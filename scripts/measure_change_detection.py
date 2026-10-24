# LIFESPAN: permanent
"""Measure snapshot change detection on invented data against a local Postgres, and print every number it measured.

    python scripts/measure_change_detection.py --dir <scratch folder> [--n 400000] [--json-out <file>]

It makes two scratch databases on the server named by ANALYTICS_TEST_DSN (default: the compose Postgres on port 5433), applies migration 005 to each, and drops both at the end. It never
touches the database the DSN names. The password in the DSN is never printed.

Run 1: load day 1 as the baseline, load day 2, check the events against the generator's own manifest, load day 2 again (must raise nothing), push the alerts to a fake sink.
Run 2: on a second database, crash inside the day's transaction at every stage, show the tables are unchanged each time, then load day 2 cleanly and show it raises the same events once.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.changes import apply_snapshot, daily_report, deliver_pending, format_report, load_companies  # noqa: E402
from app.changes_fake_sink import FakeSink  # noqa: E402
from make_carrier_snapshot import generate  # noqa: E402

DEFAULT_DSN = "postgresql://analytics:analytics@localhost:5433/analytics"


class Crash(RuntimeError):
    pass


def _swap_db(dsn: str, name: str) -> str:
    head, _, _ = dsn.rpartition("/")
    return head + "/" + name


def _fingerprint(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "select (select count(*) from carrier_current), (select count(*) from carrier_history), (select count(*) from change_event), (select count(*) from change_outbox), "
            "(select count(*) from carrier_load), (select coalesce(md5(string_agg(usdot_norm || track_hash::text, ',' order by usdot_norm)), '') from carrier_current)"
        )
        a, b, c, d, e, f = cur.fetchone()
    return {"carrier_current": a, "carrier_history": b, "change_event": c, "change_outbox": d, "carrier_load": e, "current_digest": f}


def _events_by_kind(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("select kind, count(*) from change_event group by kind order by kind")
        return dict(cur.fetchall())


def _new_db(admin_dsn: str, label: str) -> tuple[str, str]:
    name = "changes_lab_%s_%s" % (label, uuid.uuid4().hex[:8])
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        c.execute('create database "%s"' % name)
    dsn = _swap_db(admin_dsn, name)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute((ROOT / "migrations" / "005_change_detection.sql").read_text(encoding="utf-8"))
    return name, dsn


def _drop_db(admin_dsn: str, name: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        c.execute('drop database if exists "%s" with (force)' % name)


def _r(x: float) -> float:
    return round(x, 3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="folder for the generated files (they are left there; delete it afterwards)")
    ap.add_argument("--n", type=int, default=400_000)
    ap.add_argument("--flappers", type=int, default=100, help="carriers cancelled, reinstated and cancelled again on the same filing id (days 3 and 4)")
    ap.add_argument("--json-out")
    a = ap.parse_args()
    admin_dsn = os.environ.get("ANALYTICS_TEST_DSN", DEFAULT_DSN)
    out_dir = Path(a.dir)
    results: dict = {"machine": {"platform": platform.platform(), "python": platform.python_version(), "logical_cpus": os.cpu_count()}}

    t = time.perf_counter()
    manifest = generate(out_dir, n=a.n, flappers=a.flappers, day1=date.today() - timedelta(days=3))
    results["generate_seconds"] = _r(time.perf_counter() - t)
    results["manifest"] = manifest
    day1, day2 = out_dir / "carriers_day1.csv", out_dir / "carriers_day2.csv"
    d1, d2 = date.fromisoformat(manifest["snapshot_day1"]), date.fromisoformat(manifest["snapshot_day2"])
    results["file_mb"] = {"day1": _r(day1.stat().st_size / 1e6), "day2": _r(day2.stat().st_size / 1e6)}

    created = []
    try:
        # ---------------------------------------------------------------- run 1
        name, dsn = _new_db(admin_dsn, "run")
        created.append(name)
        with psycopg.connect(dsn, autocommit=True) as conn:
            results["server"] = conn.execute("select version()").fetchone()[0].split(" on ")[0]
            results["companies_loaded"] = load_companies(conn, out_dir / "companies.csv")

            r1 = apply_snapshot(conn, day1, d1)
            results["day1_baseline"] = {**r1.__dict__, "seconds": {k: _r(v) for k, v in r1.seconds.items()}, "snapshot_date": str(r1.snapshot_date)}

            r2 = apply_snapshot(conn, day2, d2)
            kinds = _events_by_kind(conn)
            results["day2"] = {**r2.__dict__, "seconds": {k: _r(v) for k, v in r2.seconds.items()}, "snapshot_date": str(r2.snapshot_date)}
            results["day2_events_by_kind"] = kinds
            results["day2_matches_manifest"] = kinds == manifest["expected_events_by_kind"]

            before = _fingerprint(conn)
            r3 = apply_snapshot(conn, day2, d2)
            after = _fingerprint(conn)
            results["day2_again"] = {"rows_changed": r3.rows_changed, "events_new": r3.events_new, "events_suppressed": r3.events_suppressed,
                                     "seconds": {k: _r(v) for k, v in r3.seconds.items()}, "events_total_before": before["change_event"], "events_total_after": after["change_event"],
                                     "tables_identical": before == after or {k: v for k, v in before.items() if k != "carrier_load"} == {k: v for k, v in after.items() if k != "carrier_load"}}

            report = daily_report(conn, d2)
            results["report_counts"] = {k: report[k] for k in ("matched_events", "conflict_events", "unmatched_events")}
            results["manifest_matches"] = {"matched": manifest["expected_matched"], "conflict": manifest["expected_conflict"], "unmatched": manifest["expected_unmatched"]}
            sink = FakeSink()
            t = time.perf_counter()
            delivered = 0
            while True:
                pass_result = deliver_pending(conn, sink, limit=1000)
                if not pass_result.delivered:
                    break
                delivered += pass_result.delivered
            results["outbox"] = {"delivered": delivered, "seconds": _r(time.perf_counter() - t), "sink_calls": len(sink.calls), "tasks_created": len(sink.tasks)}
            results["report_text"] = format_report(daily_report(conn, d2))
            hist = conn.execute("select count(*) from carrier_history").fetchone()[0]
            results["history_rows"] = hist

            # Days 3 and 4: cancelled on day 2, reinstated on day 3, cancelled again on day 4 on the same filing id. Each cancellation alerts once; replaying day 4 alerts nothing.
            r_d3 = apply_snapshot(conn, out_dir / "carriers_day3.csv", d2 + timedelta(days=1))
            r_d4 = apply_snapshot(conn, out_dir / "carriers_day4.csv", d2 + timedelta(days=2))
            r_d4_again = apply_snapshot(conn, out_dir / "carriers_day4.csv", d2 + timedelta(days=2))
            n_flap = conn.execute("select count(*) from change_event where kind = 'cancellation_filed' and usdot_norm = any(%s)", (manifest["flap_usdots"],)).fetchone()[0]
            results["flap"] = {"carriers": manifest["flap_carriers"], "cancellation_events_for_them": n_flap, "expected": manifest["expected_flap_cancellations"],
                               "day3": {"rows_changed": r_d3.rows_changed, "events_new": r_d3.events_new, "seconds_total": _r(r_d3.seconds["total"])},
                               "day4": {"rows_changed": r_d4.rows_changed, "events_new": r_d4.events_new, "seconds_total": _r(r_d4.seconds["total"])},
                               "day4_again_events_new": r_d4_again.events_new}

        # ---------------------------------------------------------------- run 2: crash at every stage
        name2, dsn2 = _new_db(admin_dsn, "crash")
        created.append(name2)
        with psycopg.connect(dsn2, autocommit=True) as conn:
            load_companies(conn, out_dir / "companies.csv")
            apply_snapshot(conn, day1, d1)
            base = _fingerprint(conn)
            crashes = {}
            for stage in ("setup", "copy", "clean", "compare", "history", "events", "apply"):
                def boom(s, stage=stage):
                    if s == stage:
                        raise Crash(stage)
                try:
                    apply_snapshot(conn, day2, d2, on_stage=boom)
                    crashes[stage] = "NO CRASH"
                except Crash:
                    crashes[stage] = "tables unchanged" if _fingerprint(conn) == base else "STATE CHANGED"
            results["crash_points"] = crashes
            clean = apply_snapshot(conn, day2, d2)
            kinds2 = _events_by_kind(conn)
            again = apply_snapshot(conn, day2, d2)
            results["after_crash_clean_rerun"] = {"events_new": clean.events_new, "events_by_kind": kinds2, "same_as_run_1": kinds2 == kinds,
                                                  "rerun_again_events_new": again.events_new, "event_total": sum(kinds2.values())}
    finally:
        for name in created:
            _drop_db(admin_dsn, name)

    text = json.dumps(results, indent=2, default=str)
    print(text)
    if a.json_out:
        Path(a.json_out).write_text(text, encoding="utf-8")
    ok = results["flap"]["cancellation_events_for_them"] == results["flap"]["expected"] and results["flap"]["day4_again_events_new"] == 0 and results["day2_matches_manifest"] and results["day2_again"]["events_new"] == 0 and all(v == "tables unchanged" for v in results["crash_points"].values())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
