# LIFESPAN: permanent
"""Pull one Meta ad account's daily figures into the stored snapshots for one connection.

    DATABASE_URL=postgresql://... META_AD_ACCOUNT_ID=<number> META_ACCESS_TOKEN=<token> \
        python scripts/sync_meta.py --tenant <tenant uuid> --connection <connection uuid> [--days N] [--end 2026-11-01]

The window is half open: it runs up to, not including, END (default: the UTC date twelve hours ago, so no day that is
still running in any ad account time zone is pulled) and starts DAYS before it (default: the first day of the month
before END's month, so a run on the 1st holds the whole month that has just ended). META_AD_ACCOUNT_ID must be the ad
account that connection holds, or the run is refused. Running it again over the same window rewrites the same rows. A day inside the click window can still gain purchases, so a later run
over the same days restates them. The credentials come from the environment only; META_API_BASE_URL points it
at another server, which is how it is run against the stand-in in app/connectors/meta_fake.py. It has never
been run against a live ad account.

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

from app.connectors.meta import MetaConnector, settled_window_end  # noqa: E402
from app.sync.cli import add_arguments, check_days, run_connection_sync  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    try:
        connector = MetaConnector.from_env()
    except ValueError as exc:
        print("no sync: %s" % exc, file=sys.stderr)
        return 2
    return await run_connection_sync(
        args, platform="meta", connector=connector, default_end=settled_window_end(datetime.now(timezone.utc)))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(p, "meta")
    args = p.parse_args(argv)
    check_days(p, args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
