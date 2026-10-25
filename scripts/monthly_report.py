# LIFESPAN: permanent
"""Build the monthly report deck for one tenant's connection from the stored snapshots.

    DATABASE_URL=postgresql://... python scripts/monthly_report.py --month 2026-10 --tenant <tenant uuid> [--connection <uuid>] [--out report.pptx] [--dump]

Reads stored snapshots only, never a vendor API. The narrative is written by a model only
when GROQ_API_KEY or LLM_API_KEY is set (and --no-model is not); otherwise, or when the
model's text names a number that is not among the computed figures, a fixed template writes
it. The command prints which writer ran and why a refused text was refused; no slide says so. The key is never printed.

Exit 0: the deck was written. Exit 2: the report could not be produced, with the reason.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402

from app.db import Database, UnsafeDatabaseRole  # noqa: E402
from app.report.deck import dump_deck  # noqa: E402
from app.report.insights import ReportInsightWriter  # noqa: E402
from app.report.monthly import ReportError  # noqa: E402
from app.report.service import generate  # noqa: E402

MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def parse_month(text: str) -> tuple[int, int]:
    m = MONTH_RE.match(text)
    if not m:
        raise argparse.ArgumentTypeError("use YYYY-MM, for example 2026-10")
    return int(m.group(1)), int(m.group(2))


def model_writer(disabled: bool) -> ReportInsightWriter | None:
    key = os.environ.get("GROQ_API_KEY") or os.environ.get("LLM_API_KEY")
    return ReportInsightWriter(key) if key and not disabled else None


async def run(args: argparse.Namespace) -> int:
    year, month = args.month
    out = args.out or "monthly_report_%04d-%02d.pptx" % (year, month)
    try:
        db = Database(max_size=2)
    except RuntimeError as exc:  # DATABASE_URL is not set
        print("no report: %s" % exc, file=sys.stderr)
        return 2
    try:
        await db.connect()
        path, report, insights = await generate(
            db, args.tenant, year, month, out,
            connection_id=args.connection, writer=model_writer(args.no_model),
            data_note=args.data_note, as_of=args.as_of,
        )
    except ReportError as exc:
        print("no report: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, asyncpg.PostgresError, UnsafeDatabaseRole) as exc:
        # The class only: a connection error can carry the host and user, never print it whole.
        print("no report: the database could not be read (%s)" % type(exc).__name__, file=sys.stderr)
        return 2
    finally:
        await db.close()
    print("wrote %s (%s, insights: %s)" % (path, report.current.name, insights.source))
    if insights.rejected_reason:
        print("model text not used: %s" % insights.rejected_reason)
    if args.dump:
        print(dump_deck(path))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--month", type=parse_month, required=True, help="YYYY-MM")
    p.add_argument("--tenant", required=True, help="tenant uuid")
    p.add_argument("--connection", help="connection uuid, needed when the tenant has more than one")
    p.add_argument("--out", help="deck path, default monthly_report_<month>.pptx")
    p.add_argument("--no-model", action="store_true", help="use the template even when a key is set")
    p.add_argument("--dump", action="store_true", help="print the saved deck back as text")
    p.add_argument("--data-note", default="", help="a line printed on the title slide")
    p.add_argument("--as-of", type=date.fromisoformat, default=None, help="treat this date as today (YYYY-MM-DD)")
    return asyncio.run(run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
