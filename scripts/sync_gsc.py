# LIFESPAN: permanent
"""Pull one Search Console property's daily figures and top queries and pages into the stored snapshots.

    DATABASE_URL=postgresql://... GSC_SITE_URL=<sc-domain:example.com or https://www.example.com/> GSC_ACCESS_TOKEN=<token> \
        python scripts/sync_gsc.py --tenant <tenant uuid> --connection <connection uuid> [--days N] [--end 2026-11-01] [--top 10]

The window is half open: it runs up to, not including, END (default: the UTC date twelve hours ago) and starts DAYS
before it (default: the first day of the month before END's month, so a run on the 1st holds the whole month that has
just ended). GSC_SITE_URL must be the property that connection holds, or the run is refused. The newest days are still being collected by Google: they are stored marked provisional and a later
run over the same days rewrites them, so run it again a few days later for a month that has just ended. Dates are
Pacific time. `--top` is the number of top queries and top pages kept for each day (0 keeps none). The
credentials come from the environment only; GSC_API_BASE_URL points it at another server, which is how it is run
against the stand-in in app/connectors/gsc_fake.py. It has never been run against a live property.

Exit 0: the run succeeded. Exit 1: it ran and failed, or another worker holds the connection. Exit 2: it could
not start, with the reason on stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.gsc import DEFAULT_TOP_N, GSCConnector, settled_window_end  # noqa: E402
from app.sync.cli import add_arguments, check_days, run_connection_sync  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    try:
        connector = GSCConnector.from_env(top_n=args.top)
    except ValueError as exc:
        print("no sync: %s" % exc, file=sys.stderr)
        return 2
    return await run_connection_sync(
        args, platform="gsc", connector=connector, default_end=settled_window_end(datetime.now(timezone.utc)))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(p, "gsc")
    p.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="top queries and pages kept per day (default %d)" % DEFAULT_TOP_N)
    args = p.parse_args(argv)
    check_days(p, args)
    if not 0 <= args.top <= 25000:
        p.error("--top must be 0 to 25000")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
