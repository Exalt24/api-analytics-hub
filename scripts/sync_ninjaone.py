# LIFESPAN: permanent
"""Pull one NinjaOne tenant's device and active alert figures into the stored snapshots for one connection.

    DATABASE_URL=postgresql://... NINJAONE_CLIENT_ID=... NINJAONE_CLIENT_SECRET=... NINJAONE_EPOCH_UNIT=seconds \
    python scripts/sync_ninjaone.py --tenant <tenant uuid> --connection <connection uuid> [--days N] [--end 2026-11-01] [--ledger devices.jsonl]

The window is half open: it runs up to, not including, END (default: the UTC date twelve hours ago) and starts DAYS before it (default: the first day of the month before END's month).
The client id must be the one that connection holds, or the run is refused. The device and alert counts are what NinjaOne listed when the run happened, and are written on the window's last day;
the alerts created are the ACTIVE alerts whose creation time falls on each day. `--ledger PATH` also writes the devices and alerts the run read to a JSON lines file, for `scripts/msp_backlog.py`.
NINJAONE_EPOCH_UNIT is required, seconds or milliseconds, because the NinjaOne pages do not say which its timestamps use: check one alert's createTime. NINJAONE_BASE_URL names another region
or a stand-in. The credentials come from the environment only. It has never been run against a live tenant; the stand-in server in app/connectors/ninjaone_fake.py is what it was tested against.

Exit 0: the run succeeded. Exit 1: it ran and failed, or another worker holds the connection. Exit 2: it could not start, with the reason on stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.ga4 import settled_window_end  # noqa: E402
from app.connectors.ninjaone import NinjaOneConnector  # noqa: E402
from app.msp.ledger import write_ledger  # noqa: E402
from app.sync.cli import add_arguments, check_days, run_connection_sync  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    try:
        connector = NinjaOneConnector.from_env()
    except ValueError as exc:
        print("no sync: %s" % exc, file=sys.stderr)
        return 2
    code = await run_connection_sync(args, platform="ninjaone", connector=connector, default_end=settled_window_end(datetime.now(timezone.utc)))
    if args.ledger and code == 0:
        count = write_ledger(args.ledger, [*connector.devices_read, *connector.alerts_read], generated_at=datetime.now(timezone.utc))
        print("ledger: %d records written to %s" % (count, args.ledger))
    return code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(p, "ninjaone")
    p.add_argument("--ledger", help="also write the devices and alerts read to this JSON lines file")
    args = p.parse_args(argv)
    check_days(p, args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
