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


def _why(exc: Exception, dsn: str) -> str:
    """The reason a load could not run, in words an operator can act on: the database's own message and where it happened (a bad cell, the file line) when the server gave one,
    else the exception text. The connection string and its password are cut out first, because the string is a secret and a driver message can carry it."""
    diag = getattr(exc, "diag", None)
    text = (diag.message_primary if diag is not None and getattr(diag, "message_primary", None) else str(exc)) or "no message"
    where = getattr(diag, "context", None) if diag is not None else None
    if where:
        text = "%s (%s)" % (text, " ".join(str(where).split()))
    secrets = {dsn}
    try:
        secrets.add(psycopg.conninfo.conninfo_to_dict(dsn).get("password") or "")
    except Exception:  # noqa: BLE001 - a malformed string has no password to find, and its parse error quotes the offending text, so that message is never printed
        return "the connection string in DATABASE_URL could not be parsed (its text is not printed)"
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<hidden>")
    return " ".join(text.split())[:300]


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
        print("could not load: %s: %s" % (type(exc).__name__, _why(exc, dsn)), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
