# LIFESPAN: permanent
"""Pull one Harvest account's daily time tracked, billable time and billable value into the stored snapshots for one connection.

    DATABASE_URL=postgresql://... HARVEST_ACCOUNT_ID=1234567 HARVEST_ACCESS_TOKEN=... HARVEST_CURRENCY=USD HARVEST_CONTACT=ops@example.com \
    python scripts/sync_harvest.py --tenant <tenant uuid> --connection <connection uuid> [--days N] [--end 2026-11-01]

The window is half open: it runs up to, not including, END (default: the UTC date twelve hours ago, so no day that is still running in any time
zone is pulled) and starts DAYS before it (default: the first day of the month before END's month, so a run on the 1st holds the whole month that
has just ended). The account id must be the one that connection holds, or the run is refused. Running it again over the same window rewrites the
same rows. Optional: HARVEST_BASE_URL (a stand-in). The credentials come from the environment only. It has never been run against a live account;
the stand-in server in app/connectors/harvest_fake.py is what it was tested against.

A key that may not read projects still writes the three hour counts: the run record lists harvest_billable_amount as degraded.

Exit 0: the run succeeded. Exit 1: it ran and failed, or another worker holds the connection. Exit 2: it could not start, with the reason on stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.harvest import HarvestConnector, settled_window_end  # noqa: E402
from app.sync.cli import add_arguments, check_days, run_connection_sync  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    try:
        connector = HarvestConnector.from_env()
    except ValueError as exc:
        print("no sync: %s" % exc, file=sys.stderr)
        return 2
    return await run_connection_sync(
        args, platform="harvest", connector=connector, default_end=settled_window_end(datetime.now(timezone.utc)))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(p, "harvest")
    args = p.parse_args(argv)
    check_days(p, args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
