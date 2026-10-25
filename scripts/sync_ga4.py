# LIFESPAN: permanent
"""Pull one GA4 property's daily figures into the stored snapshots for one connection.

    DATABASE_URL=postgresql://... GA4_PROPERTY_ID=<number> GA4_ACCESS_TOKEN=<token> \
        python scripts/sync_ga4.py --tenant <tenant uuid> --connection <connection uuid> [--days 30] [--end 2026-11-01]

The window is half open: it runs from END minus DAYS up to, not including, END (default: the UTC date twelve hours
ago, so no day that is still running in any time zone is pulled). Running it again over the same window rewrites the same rows, it
never adds a second set. The credentials come from the environment only. GA4_API_BASE_URL points it at
another server, which is how it is run against the stand-in in app/connectors/ga4_fake.py.

It runs as the `sync_worker` role, which the sync tables allow to cross tenants, so the connection is
checked against the tenant here before anything is claimed.

Exit 0: the run succeeded. Exit 1: it ran and failed, or another worker holds the connection. Exit 2: it
could not start, with the reason on stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402

from app.connectors.ga4 import GA4Connector, settled_window_end  # noqa: E402
from app.db import Database, UnsafeDatabaseRole  # noqa: E402
from app.sync.runner import SyncRunner  # noqa: E402

WORKER_ROLE = "sync_worker"


async def run(args: argparse.Namespace) -> int:
    try:
        connector = GA4Connector.from_env()
    except ValueError as exc:
        print("no sync: %s" % exc, file=sys.stderr)
        return 2
    end = args.end or settled_window_end(datetime.now(timezone.utc))
    start = end - timedelta(days=args.days)
    try:
        db = Database(max_size=2, role=WORKER_ROLE)
    except RuntimeError as exc:  # DATABASE_URL is not set
        print("no sync: %s" % exc, file=sys.stderr)
        return 2
    try:
        await db.connect()
        async with db.unscoped() as conn:
            platform = await conn.fetchval(
                "select platform from platform_connection where id = $1::uuid and tenant_id = $2::uuid "
                "and status = 'active'", args.connection, args.tenant)
        if platform != "ga4":
            print("no sync: that tenant has no active ga4 connection with that id", file=sys.stderr)
            return 2
        outcome = await SyncRunner(db).run_once(
            tenant_id=args.tenant, connection_id=args.connection, connector=connector,
            window_start=start, window_end=end, trigger="manual")
    except (OSError, asyncpg.PostgresError, UnsafeDatabaseRole) as exc:
        # The class only: a connection error can carry the host and user, never print it whole.
        print("no sync: the database could not be used (%s)" % type(exc).__name__, file=sys.stderr)
        return 2
    finally:
        await db.close()
    if not outcome.claimed:
        print("another sync holds this connection, nothing was run")
        return 1
    print("sync %s: window %s to %s, %d rows written, %d api calls, %d waits"
          % (outcome.status, start, end, outcome.rows_written, outcome.api_calls, outcome.throttle_waits))
    if outcome.error_code:
        print("error %s: %s" % (outcome.error_code, outcome.error_detail))
    return 0 if outcome.status in ("succeeded", "partial") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", required=True, help="tenant uuid")
    p.add_argument("--connection", required=True, help="the ga4 connection uuid")
    p.add_argument("--days", type=int, default=30, help="how many days back from the end (default 30)")
    p.add_argument("--end", type=date.fromisoformat, default=None, help="first day NOT pulled, YYYY-MM-DD")
    args = p.parse_args(argv)
    if not 1 <= args.days <= 3650:
        p.error("--days must be 1 to 3650")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
