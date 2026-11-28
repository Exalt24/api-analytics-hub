# LIFESPAN: permanent
"""Pull one Autotask database's daily ticket and technician time figures into the stored snapshots for one connection.

    DATABASE_URL=postgresql://... AUTOTASK_USERNAME=api@example.com AUTOTASK_SECRET=... AUTOTASK_INTEGRATION_CODE=... \
    python scripts/sync_autotask.py --tenant <tenant uuid> --connection <connection uuid> [--days N] [--end 2026-11-01] [--ledger tickets.jsonl]

The window is half open: it runs up to, not including, END (default: the UTC date twelve hours ago) and starts DAYS before it (default: the first day of the month before
END's month). The API user must be the one that connection holds, or the run is refused. Running it again over the same window rewrites the same rows, and the resolution goals
missed are restated by each run. `--ledger PATH` also writes the tickets and time entries the run read to a JSON lines file, for `scripts/msp_backlog.py`.
The credentials come from the environment only; the zone is looked up from the user name (AUTOTASK_API_URL and AUTOTASK_ZONE_LOOKUP_URL point at a stand-in). It has never been
run against a live database; the stand-in server in app/connectors/autotask_fake.py is what it was tested against.

Exit 0: the run succeeded. Exit 1: it ran and failed, or another worker holds the connection. Exit 2: it could not start, with the reason on stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.autotask import AutotaskConnector  # noqa: E402
from app.connectors.ga4 import settled_window_end  # noqa: E402
from app.msp.ledger import write_ledger  # noqa: E402
from app.sync.cli import add_arguments, check_days, run_connection_sync  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    try:
        connector = AutotaskConnector.from_env()
    except ValueError as exc:
        print("no sync: %s" % exc, file=sys.stderr)
        return 2
    code = await run_connection_sync(args, platform="autotask", connector=connector, default_end=settled_window_end(datetime.now(timezone.utc)))
    if args.ledger and code == 0:
        count = write_ledger(args.ledger, [*connector.tickets_read, *connector.time_read], generated_at=datetime.now(timezone.utc))
        print("ledger: %d records written to %s" % (count, args.ledger))
    return code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(p, "autotask")
    p.add_argument("--ledger", help="also write the tickets and time entries read to this JSON lines file")
    args = p.parse_args(argv)
    check_days(p, args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
