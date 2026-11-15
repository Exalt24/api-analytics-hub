"""Command line for the lakehouse run.

    python -m lakehouse.run --source csv --as-of 2026-11-01                  # every step, from the CSV export
    python -m lakehouse.run --source jdbc --rest-url http://127.0.0.1:8765 --rest-token stand-in-token --org-id 00DST0000000001AAA --as-of 2026-11-01
    python -m lakehouse.run --check --as-of 2026-11-01                       # only the quality checks; exit 2 when a key table fails

Exit codes: 0 every step and every key check passed, 1 a step failed (the run log names it), 2 the steps ran and a key quality check failed.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import config, quality
from .pipeline import PgSource, QualityFailed, RestSource, run_pipeline
from .runlog import RunLog, StepFailed
from .spark_session import build_spark
from .warehouse import Lakehouse


def print_flags(flags: list[dict]) -> None:
    for f in flags:
        print(f"  {f['status']:4}  {f['table_name']:38} {f['check_name']:15} failed {f['rows_failed']} of {f['rows_checked']}" + (f"  ({f['detail']})" if f["status"] == "FAIL" and f["detail"] else ""))


def check_only(lh: Lakehouse) -> int:
    """Run the quality checks against what is in the warehouse now. Exit 0 when every key table passes, 2 when one fails, 1 when the checks could not run."""
    log = RunLog(lh)
    flags: list[dict] = []
    failed: list[dict] = []
    try:
        with log.step("quality_checks") as s:
            flags = quality.run_quality(lh, log.run_id)
            s.rows_read = s.rows_written = len(flags)
            failed = quality.key_failures(flags)
            if failed:
                raise QualityFailed("; ".join(f"{f['table_name']}.{f['check_name']}" for f in failed))
    except StepFailed as exc:
        print("quality_checks:", exc.error)
        if not failed:
            return 1
    print_flags(flags)
    print("check: " + (f"{len(failed)} key check(s) FAILED" if failed else "every key table passes"))
    return 2 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--warehouse", type=Path, default=config.DEFAULT_WAREHOUSE)
    ap.add_argument("--source", choices=["jdbc", "csv"], default="jdbc", help="Postgres by JDBC (default) or the CSV export")
    ap.add_argument("--jdbc-url", default=config.DEFAULT_JDBC_URL)
    ap.add_argument("--export-dir", type=Path, default=config.DEFAULT_EXPORT_DIR)
    ap.add_argument("--rest-url")
    ap.add_argument("--rest-token", default="stand-in-token")
    ap.add_argument("--org-id")
    ap.add_argument("--as-of", type=date.fromisoformat, default=date.today(), help="the day freshness is judged against (default today)")
    ap.add_argument("--full-refresh", action="store_true", help="read every source row again instead of only what changed")
    ap.add_argument("--check", action="store_true", help="only run the quality checks; exit non zero when a key table fails")
    ap.add_argument("--fail-at", help="raise an error at this step, to see what a failed step looks like")
    a = ap.parse_args(argv)

    lh = Lakehouse(build_spark("lakehouse", jdbc=(a.source == "jdbc" and not a.check)), a.warehouse, a.as_of)
    if a.check:
        return check_only(lh)
    pg = PgSource(a.source, a.jdbc_url, export_dir=a.export_dir)
    rest = RestSource(a.rest_url, a.rest_token, a.org_id) if a.rest_url and a.org_id else None
    res = run_pipeline(lh, pg, rest, full_refresh=a.full_refresh, fail_at=a.fail_at)
    print(f"run {res.run_id}: {res.status}" + (f" at {res.failed_step}: {res.error}" if res.failed_step else ""))
    for r in lh.read(config.OPS_RUN_LOG).where(f"run_id = '{res.run_id}'").orderBy("started_at").collect():
        print(f"  {r['status']:9} {r['step']:36} read {r['rows_read'] or 0:>8} written {r['rows_written'] or 0:>8} quarantined {r['rows_quarantined'] or 0:>5} {r['seconds']:>7.2f}s")
    print_flags(res.flags)
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())
