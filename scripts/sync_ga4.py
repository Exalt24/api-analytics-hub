# LIFESPAN: permanent
"""Pull one GA4 property's daily figures into the stored snapshots for one connection.

    DATABASE_URL=postgresql://... GA4_PROPERTY_ID=<number> GA4_ACCESS_TOKEN=<token> \
        python scripts/sync_ga4.py --tenant <tenant uuid> --connection <connection uuid> [--days N] [--end 2026-11-01]

The window is half open: it runs up to, not including, END (default: the UTC date twelve hours ago, so no day that is
still running in any time zone is pulled) and starts DAYS before it (default: the first day of the month before END's
month, so a run on the 1st holds the whole month that has just ended). Running it again over the same window rewrites
the same rows, it never adds a second set. The credentials come from the environment only. GA4_API_BASE_URL points it
at another server, which is how it is run against the stand-in in app/connectors/ga4_fake.py.

It runs as the `sync_worker` role, which the sync tables allow to cross tenants, so the connection is
checked against the tenant before anything is claimed, and GA4_PROPERTY_ID must be the property that connection
holds (app/sync/cli.py).

Exit 0: the run succeeded. Exit 1: it ran and failed, or another worker holds the connection. Exit 2: it
could not start, with the reason on stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.ga4 import GA4Connector, settled_window_end  # noqa: E402
from app.sync.cli import add_arguments, check_days, run_connection_sync  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    try:
        connector = GA4Connector.from_env()
    except ValueError as exc:
        print("no sync: %s" % exc, file=sys.stderr)
        return 2
    return await run_connection_sync(
        args, platform="ga4", connector=connector, default_end=settled_window_end(datetime.now(timezone.utc)))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(p, "ga4")
    args = p.parse_args(argv)
    check_days(p, args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
