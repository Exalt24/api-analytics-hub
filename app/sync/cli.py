"""The shared body of the command line sync scripts for one connection.

`scripts/sync_ga4.py` was the first of these and carries its own copy; the Meta and Search Console scripts
call this one, so the checks that matter live in one place: the connection is verified against the tenant and
the platform before anything is claimed, the sync runs as the `sync_worker` role, a database failure prints the
class only (a connection error can carry the host and user), and the exit code says what happened.

Exit 0: the run succeeded. Exit 1: it ran and failed, or another worker holds the connection. Exit 2: it could
not start, with the reason on stderr.

THE DEFAULT WINDOW starts on the first day of the calendar month before the one the end falls in, so a sync run on the
1st (the day a monthly deck is built) holds the whole month that has just ended whatever its length, and a rerun on any
later day of the month still does. `--days N` overrides it with N days back from the end.

THE ACCOUNT MUST BE THE CONNECTION'S. The account or property comes from the environment (META_AD_ACCOUNT_ID,
GSC_SITE_URL, GA4_PROPERTY_ID) and the connection from `--connection`; with a variable left over from another client the
two name different accounts, and one client's figures would be written into the other's history under a run that says it
succeeded. A sync whose two do not name the same account is refused before anything is claimed.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from typing import Any

import asyncpg

from app.db import Database, UnsafeDatabaseRole
from app.sync.runner import SyncRunner

WORKER_ROLE = "sync_worker"

#: The prefix a platform's account id may carry in one spelling and not in another: Meta's `act_`, a Search Console Domain property's
#: `sc-domain:` and GA4's `properties/`.
_ACCOUNT_PREFIX = {"meta": "act_", "gsc": "sc-domain:", "ga4": "properties/"}


def account_key(platform: str, account: str) -> str:
    """One account in one spelling: no space, no case, no trailing slash and no platform prefix, so every spelling of it is equal."""
    text = str(account).strip().lower().rstrip("/")
    prefix = _ACCOUNT_PREFIX.get(platform, "")
    return text[len(prefix):] if prefix and text.startswith(prefix) else text


def previous_month_start(end: date) -> date:
    """The first day of the calendar month before the one `end` falls in."""
    return (end.replace(day=1) - timedelta(days=1)).replace(day=1)


def window_start(args: argparse.Namespace, end: date) -> date:
    return end - timedelta(days=args.days) if args.days is not None else previous_month_start(end)


def add_arguments(parser: argparse.ArgumentParser, platform: str) -> None:
    parser.add_argument("--tenant", required=True, help="tenant uuid")
    parser.add_argument("--connection", required=True, help="the %s connection uuid" % platform)
    parser.add_argument("--days", type=int, default=None,
                        help="how many days back from the end (default: from the first day of the month before the end's month)")
    parser.add_argument("--end", type=date.fromisoformat, default=None, help="first day NOT pulled, YYYY-MM-DD")


def check_days(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.days is not None and not 1 <= args.days <= 3650:
        parser.error("--days must be 1 to 3650")


async def run_connection_sync(args: argparse.Namespace, *, platform: str, connector: Any, default_end: date) -> int:
    """Sync one window of one connection through the runner and print the outcome."""
    end = args.end or default_end
    start = window_start(args, end)
    try:
        db = Database(max_size=2, role=WORKER_ROLE)
    except RuntimeError as exc:  # DATABASE_URL is not set
        print("no sync: %s" % exc, file=sys.stderr)
        return 2
    try:
        await db.connect()
        async with db.unscoped() as conn:
            found = await conn.fetchrow(
                "select platform, external_account from platform_connection where id = $1::uuid and tenant_id = $2::uuid "
                "and status = 'active'", args.connection, args.tenant)
        if found is None or found["platform"] != platform:
            print("no sync: that tenant has no active %s connection with that id" % platform, file=sys.stderr)
            return 2
        if account_key(platform, found["external_account"]) != account_key(platform, connector.external_account):
            print("no sync: the environment names %s but that connection holds %s" % (connector.external_account, found["external_account"].strip()),
                  file=sys.stderr)
            return 2
        outcome = await SyncRunner(db).run_once(
            tenant_id=args.tenant, connection_id=args.connection, connector=connector,
            window_start=start, window_end=end, trigger="manual")
    except (OSError, asyncpg.PostgresError, UnsafeDatabaseRole) as exc:
        print("no sync: the database could not be used (%s)" % type(exc).__name__, file=sys.stderr)
        return 2
    finally:
        await db.close()
        await connector.aclose()
    if not outcome.claimed:
        print("another sync holds this connection, nothing was run")
        return 1
    print("sync %s: window %s to %s, %d rows written, %d api calls, %d waits"
          % (outcome.status, start, end, outcome.rows_written, outcome.api_calls, outcome.throttle_waits))
    if outcome.error_code:
        print("error %s: %s" % (outcome.error_code, outcome.error_detail))
    return 0 if outcome.status in ("succeeded", "partial") else 1
