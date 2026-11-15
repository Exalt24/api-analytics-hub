"""One scripted run that prints the proofs the docs quote: a full run, the same input again, messy rows, a failed step, the cost ranking.

    python -m lakehouse.proof --source jdbc --jdbc-url jdbc:postgresql://127.0.0.1:5543/lakehouse_src > docs/test-runs/<date>-lakehouse-proof.txt
    python -m lakehouse.proof --source csv                        # no Postgres needed

It seeds nothing itself: the sample figures come from `python -m lakehouse.sample_data` (the Postgres or the CSV export). It starts the stand-in REST server in this process and
builds a fresh warehouse in a temp folder. Every number it prints is read back from the Delta tables and the run log, not counted by the script.
"""
from __future__ import annotations

import argparse
import csv
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

from pyspark.sql import functions as F

from . import config, costs, gold
from .bronze import land_records
from .pipeline import PgSource, RestSource, run_pipeline
from .sample_data import SF_ORG_ID, build_world
from .spark_session import build_spark
from .stand_in import StandInSalesforce
from .warehouse import Lakehouse

LAYERS = ("bronze_", "silver_", "gold_")


def state(lh: Lakehouse) -> dict:
    return {t: (lh.version(t), lh.count(t)) for t in lh.table_names() if t.startswith(LAYERS)}


def show(title: str) -> None:
    print("\n== " + title)


def log_rows(lh: Lakehouse, run_id: str) -> None:
    for r in lh.read(config.OPS_RUN_LOG).where(F.col("run_id") == run_id).orderBy("started_at").collect():
        err = f"  error: {r['error'][:110]}" if r["error"] else ""
        print(f"  {r['status']:9} {r['step']:34} read {r['rows_read'] or 0:>6} written {r['rows_written'] or 0:>6} quarantined {r['rows_quarantined'] or 0:>3} {r['seconds']:>6.2f}s{err}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["jdbc", "csv"], default="jdbc")
    ap.add_argument("--jdbc-url", default=config.DEFAULT_JDBC_URL)
    ap.add_argument("--export-dir", type=Path, default=config.DEFAULT_EXPORT_DIR)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 1))
    a = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="lh-proof-"))
    try:
        spark = build_spark("lakehouse-proof", jdbc=(a.source == "jdbc"))
        lh = Lakehouse(spark, work / "warehouse", a.end)
        world = build_world(a.seed, a.days, a.end)
        pg = PgSource(a.source, a.jdbc_url, export_dir=a.export_dir)
        print(f"sample: seed {a.seed}, {a.days} days to {a.end}, {len(world.tenants)} tenants, {len(world.snapshots)} daily figures, "
              f"{sum(len(v) for v in world.leads.values())} leads; source {a.source}; Spark {spark.version}")
        with StandInSalesforce([dict(x) for x in world.leads[SF_ORG_ID]], default_batch=50) as si:
            rest = RestSource(si.url, "stand-in-token", SF_ORG_ID, batch_size=50, backoff=0.05)

            show("1. first run")
            r1 = run_pipeline(lh, pg, rest)
            print(f"status {r1.status}, exit code {r1.exit_code}")
            log_rows(lh, r1.run_id)
            print(f"quality flags: {sum(f['status'] == 'PASS' for f in r1.flags)} PASS, {sum(f['status'] == 'FAIL' for f in r1.flags)} FAIL")
            s1 = state(lh)

            show("2. the same input again")
            r2 = run_pipeline(lh, pg, rest)
            s2 = state(lh)
            print(f"status {r2.status}")
            print("table                                 delta version  rows   before -> after")
            for t, (v, n) in s1.items():
                print(f"  {t:36} v{v:<3} {n:>7}   v{s2[t][0]} {s2[t][1]}   {'unchanged' if s2[t] == (v, n) else 'CHANGED'}")
            print("steps that wrote rows on the second run:",
                  [r["step"] for r in lh.read(config.OPS_RUN_LOG).where(F.col("run_id") == r2.run_id).where("rows_written > 0 AND step != 'quality_checks'").collect()] or "none")

            show("3. messy rows into bronze, then silver")
            shop = next(s for s in world.snapshots if s["metric_key"] == "orders")
            def row(**kw):
                return {**shop, **kw}
            renamed = row(observed_on="2026-10-02")
            renamed["value"] = renamed.pop("value_numeric")
            messy = [
                row(), row(),                                                                        # the same bytes twice in one batch
                row(value_numeric=shop["value_numeric"] + 5, synced_at="2026-11-02T01:00:00+00:00"),   # a newer revision
                row(observed_on="2026-10-06", value_numeric=1, synced_at="2026-10-05T00:00:00+00:00"),  # an older copy of another day arriving late
                row(observed_on="2026-10-03", value_numeric="12.5"),                                  # retyped
                row(observed_on="2026-13-45"), row(observed_on="2026-10-04", metric_key=None), renamed, # bad date, missing key, renamed column
                row(observed_on="2026-10-05", channel="web", synced_at="2026-11-02T02:00:00+00:00"),  # an added column
                "not json {", "[1, 2, 3]",                                                           # unreadable
            ]
            b = land_records(lh, "bronze_pg_metric_snapshot", source_name="postgres", source_table="metric_snapshot", run_id="messy-1", records=messy)
            print("bronze:", b, " (read = landed + skipped_known + quarantined:", b["read"] == b["landed"] + b["skipped_known"] + b["quarantined"], ")")
            from . import silver
            c = silver.run_silver(lh, silver.SPECS["silver_daily_figure"], "messy-s1")
            print("silver:", c, " (read = sum of the six outcomes:", c["read"] == sum(c[k] for k in ("inserted", "updated", "unchanged", "stale", "duplicate_in_batch", "quarantined")), ")")
            for q in lh.read(config.BRONZE_QUARANTINE).orderBy("reason").collect():
                print("  bronze quarantine:", q["reason"][:100])
            for q in lh.read(config.SILVER_QUARANTINE).orderBy("reason").collect():
                print("  silver quarantine:", q["reason"][:120])
            print("silver_daily_figure now has the column 'channel':", "channel" in lh.read("silver_daily_figure").columns)

            show("4. a failed step, then the repair")
            export = work / "export2"
            new_day = a.end + timedelta(days=1)
            if a.source == "csv":
                shutil.copytree(a.export_dir, export)
                pg_after = PgSource("csv", export_dir=export)
                with (export / "metric_snapshot.csv").open("a", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerows([[shop["tenant_id"], shop["connection_id"], "orders", new_day.isoformat(), "31", "", f"{new_day + timedelta(days=1)}T03:00:00+00:00"]])
            else:
                import psycopg
                with psycopg.connect("postgresql://analytics:analytics@" + a.jdbc_url.split("//", 1)[1].replace("jdbc:", "")) as conn:
                    conn.execute("insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric, synced_at) values (%s, %s, 'orders', %s, 31, %s) on conflict (connection_id, metric_key, observed_on) do update set value_numeric = 31, synced_at = excluded.synced_at",
                                 (shop["tenant_id"], shop["connection_id"], new_day, f"{new_day + timedelta(days=1)}T03:00:00+00:00"))
                    conn.commit()
                pg_after = pg
            before = state(lh)
            r4 = run_pipeline(lh, pg_after, rest, fail_at="gold_fact_daily_figure")
            print(f"status {r4.status}, failed step {r4.failed_step}, exit code {r4.exit_code}")
            log_rows(lh, r4.run_id)
            mid = state(lh)
            for t in ("bronze_pg_metric_snapshot", "silver_daily_figure", "gold_dim_date", "gold_fact_daily_figure"):
                print(f"  {t:30} rows {before[t][1]} -> {mid[t][1]}   version v{before[t][0]} -> v{mid[t][0]}")
            r5 = run_pipeline(lh, pg_after, rest)
            end = state(lh)
            print(f"re-run: status {r5.status}")
            for t in ("bronze_pg_metric_snapshot", "silver_daily_figure", "gold_fact_daily_figure"):
                print(f"  {t:30} rows {mid[t][1]} -> {end[t][1]}   version v{mid[t][0]} -> v{end[t][0]}")

            show("5. the cost ranking (all runs in this log)")
            costs.rank_jobs(lh, top=8).show(8, truncate=False)

            show("6. python -m lakehouse.run --check on this warehouse")
            from .run import check_only
            print("exit code", check_only(lh))
            print("gold tables:", [t for t in gold.GOLD_ORDER])
        spark.stop()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
