"""The shared body of the command line sync scripts for one connection.

`scripts/sync_ga4.py` was the first of these and carries its own copy; the Meta and Search Console scripts
call this one, so the checks that matter live in one place: the connection is verified against the tenant and
the platform before anything is claimed, the sync runs as the `sync_worker` role, a database failure prints the
class only (a connection error can carry the host and user), and the exit code says what happened.

Exit 0: the run succeeded. Exit 1: it ran and failed, or another worker holds the connection. Exit 2: it could
not start, with the reason on stderr.
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


def add_arguments(parser: argparse.ArgumentParser, platform: str) -> None:
    parser.add_argument("--tenant", required=True, help="tenant uuid")
    parser.add_argument("--connection", required=True, help="the %s connection uuid" % platform)
    parser.add_argument("--days", type=int, default=30, help="how many days back from the end (default 30)")
    parser.add_argument("--end", type=date.fromisoformat, default=None, help="first day NOT pulled, YYYY-MM-DD")


def check_days(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not 1 <= args.days <= 3650:
        parser.error("--days must be 1 to 3650")


async def run_connection_sync(args: argparse.Namespace, *, platform: str, connector: Any, default_end: date) -> int:
    """Sync one window of one connection through the runner and print the outcome."""
    end = args.end or default_end
    start = end - timedelta(days=args.days)
    try:
        db = Database(max_size=2, role=WORKER_ROLE)
    except RuntimeError as exc:  # DATABASE_URL is not set
        print("no sync: %s" % exc, file=sys.stderr)
        return 2
    try:
        await db.connect()
        async with db.unscoped() as conn:
            found = await conn.fetchval(
                "select platform from platform_connection where id = $1::uuid and tenant_id = $2::uuid "
                "and status = 'active'", args.connection, args.tenant)
        if found != platform:
            print("no sync: that tenant has no active %s connection with that id" % platform, file=sys.stderr)
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
