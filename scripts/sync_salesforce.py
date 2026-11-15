# LIFESPAN: permanent
"""Pull one Salesforce org's daily lead and opportunity counts into the stored snapshots for one connection.

    DATABASE_URL=postgresql://... SALESFORCE_INSTANCE_URL=https://yourname.my.salesforce.com SALESFORCE_CURRENCY=USD \
    SALESFORCE_CLIENT_ID=... SALESFORCE_CLIENT_SECRET=... SALESFORCE_REFRESH_TOKEN=... \
    python scripts/sync_salesforce.py --tenant <tenant uuid> --connection <connection uuid> [--days N] [--end 2026-11-01]

The window is half open: it runs up to, not including, END (default: the UTC date twelve hours ago, so no day that is still running in any time
zone is pulled) and starts DAYS before it (default: the first day of the month before END's month, so a run on the 1st holds the whole month that
has just ended). The org address must be the one that connection holds, or the run is refused. Running it again over the same window rewrites the
same rows. Optional: SALESFORCE_LOGIN_URL (a My Domain login address, or a stand-in). The credentials come from the environment only. It has never been
run against a live org; the stand-in server in app/connectors/salesforce_fake.py is what it was tested against.

If the connected app rotates refresh tokens, the first refresh kills the one in the environment: the run says so on stderr (never the token) and the new one
must be handed to the secret store by the caller.

Exit 0: the run succeeded. Exit 1: it ran and failed, or another worker holds the connection. Exit 2: it could not start, with the reason on stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.salesforce import SalesforceConnector, settled_window_end  # noqa: E402
from app.sync.cli import add_arguments, check_days, run_connection_sync  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    try:
        connector = SalesforceConnector.from_env()
    except ValueError as exc:
        print("no sync: %s" % exc, file=sys.stderr)
        return 2
    code = await run_connection_sync(
        args, platform="salesforce", connector=connector, default_end=settled_window_end(datetime.now(timezone.utc)))
    if connector.refresh_rotated:
        print("the connected app rotated its refresh token: the value in SALESFORCE_REFRESH_TOKEN no longer works, store the new one", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(p, "salesforce")
    args = p.parse_args(argv)
    check_days(p, args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
