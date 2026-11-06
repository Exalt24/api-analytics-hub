# LIFESPAN: permanent
"""Print the combined ad spend view for one tenant: cost per conversion by platform and by week, with the shop's booked jobs beside it.

    DATABASE_URL=postgresql://... python scripts/ad_spend_report.py --tenant <tenant uuid> --start 2026-10-01 --end 2026-11-01 [--json]

The window is half open, like every sync window: it runs up to, not including, END. Reads stored snapshots only, never a vendor API. Every active
Google Ads, Meta and TikTok connection of the tenant is one line; an active Shopmonkey connection supplies the booked jobs. Money stays in each ad
account's own currency and is never converted or added across currencies.

Exit 0: the view was printed. Exit 2: it could not be produced, with the reason on stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402

from app.db import Database, UnsafeDatabaseRole  # noqa: E402
from app.report.adspend import render_text, to_dict  # noqa: E402
from app.report.adspend_service import load_view  # noqa: E402
from app.report.monthly import ReportError  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    try:
        db = Database(max_size=2)
    except RuntimeError as exc:  # DATABASE_URL is not set
        print("no view: %s" % exc, file=sys.stderr)
        return 2
    try:
        await db.connect()
        view = await load_view(db, args.tenant, args.start, args.end)
    except ReportError as exc:
        print("no view: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, asyncpg.PostgresError, UnsafeDatabaseRole) as exc:
        # The class only: a connection error can carry the host and user, never print it whole.
        print("no view: the database could not be read (%s)" % type(exc).__name__, file=sys.stderr)
        return 2
    finally:
        await db.close()
    print(json.dumps(to_dict(view), indent=2) if args.json else render_text(view))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", required=True, help="tenant uuid")
    p.add_argument("--start", type=date.fromisoformat, required=True, help="first day, YYYY-MM-DD")
    p.add_argument("--end", type=date.fromisoformat, required=True, help="first day NOT included, YYYY-MM-DD")
    p.add_argument("--json", action="store_true", help="print JSON instead of the text table")
    return asyncio.run(run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
