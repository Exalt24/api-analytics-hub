# LIFESPAN: permanent
"""Pull one Shopmonkey shop's daily work orders into the stored snapshots for one connection.

    DATABASE_URL=postgresql://... SHOPMONKEY_API_KEY=<key> SHOPMONKEY_CURRENCY=<USD> python scripts/sync_shopmonkey.py --tenant <tenant uuid> --connection <connection uuid> [--days N] [--end 2026-11-01]

The window is half open: it runs up to, not including, END (default: the UTC date twelve hours ago, so no day that is still running in any time
zone is pulled) and starts DAYS before it (default: the first day of the month before END's month, so a run on the 1st holds the whole month that
has just ended). the connection's external account (the location id, or all-locations) must be the account that connection holds, or the run is refused. Running it again over the same window rewrites the same
rows. Optional: SHOPMONKEY_LOCATION_ID, SHOPMONKEY_TIME_ZONE (an IANA zone, default UTC) and SHOPMONKEY_API_BASE_URL. The credentials come from the environment only. It has never been run against a live account; the stand-in server in
app/connectors/shopmonkey_fake.py is what it was tested against.

Exit 0: the run succeeded. Exit 1: it ran and failed, or another worker holds the connection. Exit 2: it could not start, with the reason on stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.shopmonkey import ShopmonkeyConnector, settled_window_end  # noqa: E402
from app.sync.cli import add_arguments, check_days, run_connection_sync  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    try:
        connector = ShopmonkeyConnector.from_env()
    except ValueError as exc:
        print("no sync: %s" % exc, file=sys.stderr)
        return 2
    return await run_connection_sync(
        args, platform="shopmonkey", connector=connector, default_end=settled_window_end(datetime.now(timezone.utc)))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(p, "shopmonkey")
    args = p.parse_args(argv)
    check_days(p, args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
