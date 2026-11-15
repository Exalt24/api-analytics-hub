"""Query tuning with measured numbers: one deliberately slow query on a generated fact table, four versions of the table, the same method each time.

    python -m lakehouse.tuning --rows 4000000 --runs 3 --out lakehouse/results/tuning.json

The table is `rows` generated daily figures (tenant, day, metric, value and some payload columns), made from `spark.range` and a seeded hash, so two
runs build the same table. The query asks for one tenant's daily totals over one month and joins the small metric dimension for the unit:

    A  baseline      one Delta table of 200 small files in no order, the dimension join left to Spark's default for a table this size with
                     broadcast joins switched off (spark.sql.autoBroadcastJoinThreshold = -1) and adaptive execution off in every version, which is the slow starting point on purpose
    B  partitioned   the same rows partitioned by month (24 folders), so a one month filter skips 23 of them
    C  zorder        B plus OPTIMIZE ... ZORDER BY (tenant_id): files compacted and the rows of one tenant laid next to each other, so Delta's per
                     file min and max statistics skip most files inside the month
    D  broadcast     C plus a BROADCAST hint on the dimension, so the 41 row table is sent to the tasks instead of shuffling the fact table

Versions B to D add `f.month = '2026-03'` next to the day range, because Delta prunes partitions on the partition column itself.

Each version is queried once untimed (a warm up) and then `runs` times, timed with the wall clock around the whole collect. The report is the median of
the timed runs, the three raw numbers, the number of data files the scan read (a metric of the scan itself) and the result of the query, which must be
identical in every version. One machine, one day: the numbers say what happened here, not what another machine would do.
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import statistics
import time
from datetime import date
from pathlib import Path

from pyspark.sql import functions as F

from .spark_session import build_spark

METRICS = ["orders", "gross_sales", "refunds", "net_sales", "customers", "sessions", "users", "page_views", "key_events", "ad_spend", "ad_clicks", "ad_impressions"]
TENANTS = 200
DAYS = 730
START = date(2025, 1, 1)

QUERY = """
SELECT f.day, m.unit, SUM(f.figure_value) AS total, COUNT(*) AS n
FROM {fact} f JOIN {hint}bench_dim_metric m ON m.metric_key = f.metric_key
WHERE f.tenant_id = 42 AND f.day BETWEEN DATE'2026-03-01' AND DATE'2026-03-31'
GROUP BY f.day, m.unit
ORDER BY f.day, m.unit
"""


def generate(spark, rows: int, seed: int = 42):
    """A deterministic fact table: the same rows for the same (rows, seed), on any machine."""
    i = F.col("id")
    metrics = F.array(*[F.lit(m) for m in METRICS])
    return spark.range(rows).select(
        (F.pmod(F.xxhash64(i, F.lit(seed)), F.lit(TENANTS)) + 1).cast("int").alias("tenant_id"),
        F.date_add(F.lit(START.isoformat()).cast("date"), F.pmod(F.xxhash64(i, F.lit(seed + 1)), F.lit(DAYS)).cast("int")).alias("day"),
        F.element_at(metrics, (F.pmod(F.xxhash64(i, F.lit(seed + 2)), F.lit(len(METRICS))) + 1).cast("int")).alias("metric_key"),
        F.pmod(F.xxhash64(i, F.lit(seed + 3)), F.lit(100000)).cast("long").alias("figure_value"),
        F.concat(F.lit("src-"), F.pmod(F.xxhash64(i, F.lit(seed + 4)), F.lit(50)).cast("string")).alias("source_name"),
        F.sha2(i.cast("string"), 256).alias("row_checksum"),
        F.concat(F.lit("note "), F.pmod(F.xxhash64(i, F.lit(seed + 5)), F.lit(1000)).cast("string")).alias("note"),
    )


def delta_detail(spark, path: str) -> dict:
    row = spark.sql(f"DESCRIBE DETAIL delta.`{path}`").collect()[0]
    return {"files": int(row["numFiles"]), "bytes": int(row["sizeInBytes"])}


def files_read(df) -> int | None:
    """The number of data files the fact table scan read, from the scan's own metric after the query ran. None when the plan has no such metric."""
    try:
        plan = df._jdf.queryExecution().executedPlan()
        if plan.getClass().getSimpleName() == "AdaptiveSparkPlanExec":
            plan = plan.executedPlan()
        leaves = plan.collectLeaves().iterator()
        total = 0
        found = False
        while leaves.hasNext():
            leaf = leaves.next()
            if "bench_dim_metric" in leaf.toString():
                continue  # the one file of the small dimension is not part of the fact table's cost
            metric = leaf.metrics().get("numFiles")
            if metric.isDefined():
                found = True
                total += int(metric.get().value())
        return total if found else None
    except Exception:  # noqa: BLE001 - the metric is a nicety; the timing is the measurement
        return None


def measure(spark, sql: str, runs: int) -> dict:
    spark.sql(sql).collect()  # warm up: JIT, OS file cache, the first plan; not counted
    times, result, scanned = [], None, None
    for _ in range(runs):
        df = spark.sql(sql)
        t0 = time.perf_counter()
        result = df.collect()
        times.append(round(time.perf_counter() - t0, 3))
        scanned = files_read(df)
    fingerprint = [(str(r["day"]), r["unit"], int(r["total"]), int(r["n"])) for r in result]
    return {"runs": times, "median": round(statistics.median(times), 3), "files_read": scanned, "result_rows": len(fingerprint),
            "result_checksum": hash_rows(fingerprint)}


def hash_rows(rows) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]


def run_tuning(spark, work: Path, rows: int, runs: int = 3, seed: int = 42) -> dict:
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    base, part, dim = (str(work / n) for n in ("a_baseline", "b_partitioned", "bench_dim_metric"))
    for p in (base, part, dim):
        shutil.rmtree(p, ignore_errors=True)
    spark.sql("CREATE DATABASE IF NOT EXISTS bench")
    spark.catalog.setCurrentDatabase("bench")

    t0 = time.perf_counter()
    fact = generate(spark, rows, seed)
    fact.repartition(200).write.format("delta").save(base)
    build_seconds = round(time.perf_counter() - t0, 1)
    spark.sql("DROP TABLE IF EXISTS bench_dim_metric")
    dim_rows = ", ".join(f"('{m}', '{'minor_currency' if m in ('gross_sales', 'refunds', 'net_sales', 'ad_spend') else 'count'}')" for m in METRICS)
    spark.sql(f"SELECT * FROM VALUES {dim_rows} AS t(metric_key, unit)").write.format("delta").save(dim)
    spark.sql(f"CREATE TABLE bench_dim_metric USING DELTA LOCATION '{Path(dim).as_uri()}'")

    versions: dict[str, dict] = {}

    def table(name: str, path: str) -> str:
        spark.sql(f"DROP TABLE IF EXISTS {name}")
        spark.sql(f"CREATE TABLE {name} USING DELTA LOCATION '{Path(path).as_uri()}'")
        return name

    spark.conf.set("spark.sql.autoBroadcastJoinThreshold", "-1")
    # adaptive execution is off for the benchmark so the plan that ran is the plan whose scan metrics are read, and so no version gets help the others lack
    spark.conf.set("spark.sql.adaptive.enabled", "false")
    # OPTIMIZE's default target is 1 GB per file, which would fold a 20 MB month into one file and leave nothing for ZORDER to arrange; 4 MB keeps a month in a few files
    spark.conf.set("spark.databricks.delta.optimize.maxFileSize", str(4 * 1024 * 1024))
    table("bench_a", base)
    versions["A baseline"] = {"layout": "200 small files, no order", **delta_detail(spark, base)}
    versions["A baseline"].update(measure(spark, QUERY.format(fact="bench_a", hint=""), runs))

    spark.read.format("delta").load(base).withColumn("month", F.date_format("day", "yyyy-MM")).write.format("delta").partitionBy("month").save(part)
    table("bench_b", part)
    versions["B partitioned"] = {"layout": "partitioned by month", **delta_detail(spark, part)}
    versions["B partitioned"].update(measure(spark, QUERY.format(fact="bench_b", hint="").replace("f.day BETWEEN", "f.month = '2026-03' AND f.day BETWEEN"), runs))

    t1 = time.perf_counter()
    spark.sql("OPTIMIZE bench_b ZORDER BY (tenant_id)")
    optimize_seconds = round(time.perf_counter() - t1, 1)
    versions["C zorder"] = {"layout": "partitioned by month, OPTIMIZE ZORDER BY tenant_id", "optimize_seconds": optimize_seconds, **delta_detail(spark, part)}
    versions["C zorder"].update(measure(spark, QUERY.format(fact="bench_b", hint="").replace("f.day BETWEEN", "f.month = '2026-03' AND f.day BETWEEN"), runs))

    broadcast = QUERY.format(fact="bench_b", hint="").replace("SELECT f.day", "SELECT /*+ BROADCAST(m) */ f.day").replace("f.day BETWEEN", "f.month = '2026-03' AND f.day BETWEEN", 1)
    versions["D broadcast"] = {"layout": "as C, BROADCAST(m) hint", **delta_detail(spark, part)}
    versions["D broadcast"].update(measure(spark, broadcast, runs))

    checks = {v["result_checksum"] for v in versions.values()}
    assert len(checks) == 1, "the four versions did not return the same result: " + str(versions)
    return {
        "machine": f"{platform.processor() or platform.machine()}, {platform.system()} {platform.release()}",
        "rows": rows, "seed": seed, "runs_per_version": runs, "warm_up_runs_not_counted": 1, "build_seconds_baseline_table": build_seconds,
        "spark": spark.version, "versions": versions, "same_result_in_every_version": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", type=int, default=4_000_000)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--work", type=Path, default=Path("lakehouse/warehouse_tuning"))
    ap.add_argument("--out", type=Path)
    ap.add_argument("--machine", default=None, help="a name for the machine, written into the result")
    a = ap.parse_args()
    spark = build_spark("lakehouse-tuning", shuffle_partitions=8, driver_memory="6g")
    out = run_tuning(spark, a.work, a.rows, a.runs, a.seed)
    if a.machine:
        out["machine"] = a.machine
    print(json.dumps(out, indent=2))
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
