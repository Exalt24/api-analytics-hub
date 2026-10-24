# LIFESPAN: permanent
"""Load one day's carrier file and print the daily report. The cron entry point for app/changes.py.

    DATABASE_URL=postgresql://... python scripts/load_carrier_snapshot.py carriers_day2.csv --date 2026-10-24 [--force]

Exit 0: loaded. Exit 3: the file was refused before anything committed (empty, too small against what is held, too much changed, dated ahead, or older than a load already applied);
nothing changed and the reason is printed. `--force` skips the size, change-rate and date checks, never the empty-file check and never the stale-file check. Exit 2: the load could not run.
The connection string is a secret: it is read from the environment and never printed.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.changes import LoadBusy, LoadRefused, apply_snapshot, daily_report, format_report  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file")
    ap.add_argument("--date", default=date.today().isoformat(), help="the snapshot date of the file, YYYY-MM-DD (default today)")
    ap.add_argument("--force", action="store_true", help="skip the size, change-rate and date refusals")
    ap.add_argument("--absent-is-event", action="store_true", help="raise one dropped_from_file event for a carrier that vanishes from an active-only file (off by default)")
    ap.add_argument("--min-staged-ratio", type=float, default=0.5)
    ap.add_argument("--max-changed-ratio", type=float, default=0.25)
    a = ap.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            result = apply_snapshot(conn, a.file, date.fromisoformat(a.date), force=a.force, absent_is_event=a.absent_is_event, min_staged_ratio=a.min_staged_ratio, max_changed_ratio=a.max_changed_ratio)
            print(format_report(daily_report(conn, result.snapshot_date)))
    except LoadRefused as exc:
        print("refused, nothing changed: %s" % exc, file=sys.stderr)
        return 3
    except (LoadBusy, psycopg.Error, OSError, ValueError) as exc:
        print("could not load: %s" % type(exc).__name__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
