"""Data quality: one flag per table and check for every run, PASS or FAIL with the counts, written to `ops_quality_flags`.

What each check means, and what to do about a FAIL, is in lakehouse/RUNBOOK.md. A table that does not exist is a FAIL, not a skipped check.

    not_null            a key or required column holds a null
    unique_key          a business key appears twice
    accepted_range      a figure is outside the range configured for its metric (config.RANGES)
    known_metric        a figure's metric key is not in the metric vocabulary
    freshness           a source's newest day is more than the stated limit behind the as-of date
    reconciliation      silver and gold disagree on a metric's total
    orphan_keys         a gold fact row names a tenant, source, day or metric that has no dimension row
    lead_dates          a lead is created in the future or modified before it was created
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from pyspark.sql import functions as F
from pyspark.sql.types import LongType, StringType, StructField, StructType, TimestampType

from . import config
from .warehouse import Lakehouse
from .frames import frame

FLAG_SCHEMA = StructType([
    StructField("run_id", StringType(), False),
    StructField("checked_at", TimestampType(), False),
    StructField("table_name", StringType(), False),
    StructField("check_name", StringType(), False),
    StructField("status", StringType(), False),
    StructField("rows_checked", LongType()),
    StructField("rows_failed", LongType()),
    StructField("threshold", StringType()),
    StructField("detail", StringType()),
])


def flag(table: str, check: str, checked: int, failed: int, threshold: str | None = None, detail: str | None = None) -> dict:
    return {"table_name": table, "check_name": check, "status": "PASS" if failed == 0 else "FAIL", "rows_checked": int(checked),
            "rows_failed": int(failed), "threshold": threshold, "detail": detail}


def _missing(lh: Lakehouse, table: str, check: str) -> dict:
    return flag(table, check, 0, 1, None, "table does not exist")


def check_not_null(lh: Lakehouse, table: str, cols: list[str]) -> dict:
    if not lh.exists(table):
        return _missing(lh, table, "not_null")
    df = lh.read(table)
    bad = F.lit(False)
    for c in cols:
        bad = bad | F.col(c).isNull()
    n, failed = df.agg(F.count(F.lit(1)), F.sum(F.when(bad, 1).otherwise(0))).collect()[0]
    return flag(table, "not_null", n or 0, failed or 0, None, "columns: " + ",".join(cols))


def check_unique_key(lh: Lakehouse, table: str, cols: list[str]) -> dict:
    if not lh.exists(table):
        return _missing(lh, table, "unique_key")
    df = lh.read(table)
    n = df.count()
    dup = df.groupBy(*cols).count().where("count > 1")
    failed = dup.agg(F.sum(F.col("count") - 1)).collect()[0][0] or 0
    return flag(table, "unique_key", n, failed, None, "key: " + ",".join(cols))


def check_accepted_range(lh: Lakehouse) -> dict:
    table = "silver_daily_figure"
    if not lh.exists(table):
        return _missing(lh, table, "accepted_range")
    df = lh.read(table)
    out = F.lit(False)
    for key, (lo, hi) in config.RANGES.items():
        out = out | ((F.col("metric_key") == key) & ~F.col("value_numeric").between(lo, hi))
    lo, hi = config.DEFAULT_RANGE
    out = out | (~F.col("metric_key").isin(*config.RANGES) & ~F.col("value_numeric").between(lo, hi))
    n, failed = df.agg(F.count(F.lit(1)), F.sum(F.when(out, 1).otherwise(0))).collect()[0]
    return flag(table, "accepted_range", n or 0, failed or 0, "per metric, config.RANGES")


def check_known_metric(lh: Lakehouse) -> dict:
    table = "silver_daily_figure"
    if not lh.exists(table) or not lh.exists("silver_metric"):
        return _missing(lh, table, "known_metric")
    df = lh.read(table)
    orphans = df.join(lh.read("silver_metric").select(F.col("key").alias("metric_key")), "metric_key", "left_anti")
    return flag(table, "known_metric", df.count(), orphans.count(), None, "metric_key must exist in silver_metric")


def check_freshness(lh: Lakehouse, limit_days: int = config.FRESHNESS_LIMIT_DAYS) -> dict:
    table = "silver_daily_figure"
    if not lh.exists(table):
        return _missing(lh, table, "freshness")
    last = lh.read(table).groupBy("connection_id").agg(F.max("observed_on").alias("last_day"))
    lag = last.withColumn("lag_days", F.datediff(F.lit(lh.as_of.isoformat()).cast("date"), F.col("last_day")))
    stale = lag.where(F.col("lag_days") > limit_days)
    names = [f"{r['connection_id'][:8]} {r['lag_days']}d" for r in stale.orderBy(F.col("lag_days").desc()).limit(5).collect()]
    return flag(table, "freshness", last.count(), stale.count(), f"{limit_days} days behind {lh.as_of.isoformat()}", ("stale: " + "; ".join(names)) if names else None)


def check_freshness_report(lh: Lakehouse) -> dict:
    table = "gold_rpt_data_freshness"
    if not lh.exists(table):
        return _missing(lh, table, "freshness")
    df = lh.read(table)
    bad = df.where(F.col("freshness_status") != "FRESH")
    names = [f"{r['source_name']} {r['freshness_status']}" for r in bad.limit(5).collect()]
    return flag(table, "freshness", df.count(), bad.count(), None, ("not fresh: " + "; ".join(names)) if names else None)


def _silver_totals(lh: Lakehouse):
    figures = lh.read("silver_daily_figure").groupBy("metric_key").agg(F.sum("value_numeric").alias("silver_total"))
    leads = lh.read("silver_lead")
    created = leads.agg(F.count(F.lit(1)).alias("silver_total")).withColumn("metric_key", F.lit("sf_leads_created"))
    converted = (leads.where(F.col("is_converted") & F.col("converted_date").isNotNull())
                 .agg(F.count(F.lit(1)).alias("silver_total")).withColumn("metric_key", F.lit("sf_leads_converted")))
    return figures.unionByName(created.select("metric_key", "silver_total")).unionByName(converted.select("metric_key", "silver_total"))


def check_reconciliation(lh: Lakehouse) -> dict:
    table = "gold_fact_daily_figure"
    if not lh.exists(table) or not lh.exists("silver_daily_figure") or not lh.exists("silver_lead"):
        return _missing(lh, table, "reconciliation")
    silver = _silver_totals(lh)
    gold = lh.read(table).groupBy("metric_key").agg(F.sum("figure_value").alias("gold_total"))
    both = silver.join(gold, "metric_key", "full_outer").na.fill(0, ["silver_total", "gold_total"])
    mismatch = both.where(F.col("silver_total") != F.col("gold_total"))
    rows = [f"{r['metric_key']} silver={r['silver_total']} gold={r['gold_total']}" for r in mismatch.limit(5).collect()]
    return flag(table, "reconciliation", both.count(), mismatch.count(), "silver total equals gold total per metric", ("; ".join(rows)) if rows else None)


def check_orphan_keys(lh: Lakehouse) -> dict:
    table = "gold_fact_daily_figure"
    needed = ["gold_dim_tenant", "gold_dim_source", "gold_dim_date", "gold_dim_metric"]
    if not lh.exists(table) or not all(lh.exists(n) for n in needed):
        return _missing(lh, table, "orphan_keys")
    f = lh.read(table)
    missing: dict[str, int] = {}
    for dim, fk in (("gold_dim_tenant", "tenant_key"), ("gold_dim_source", "source_key"), ("gold_dim_date", "date_key"), ("gold_dim_metric", "metric_key")):
        n = f.join(lh.read(dim).select(fk).distinct(), fk, "left_anti").count()
        if n:
            missing[fk] = n
    detail = ("no dimension row for: " + ", ".join(f"{k} ({n} rows)" for k, n in missing.items())) if missing else "tenant, source, date and metric keys must have a dimension row"
    return flag(table, "orphan_keys", f.count(), sum(missing.values()), None, detail)


def check_lead_dates(lh: Lakehouse) -> dict:
    table = "silver_lead"
    if not lh.exists(table):
        return _missing(lh, table, "lead_dates")
    df = lh.read(table)
    horizon = F.lit(lh.as_of.isoformat()).cast("date") + F.expr("interval 1 day")
    bad = df.where((F.col("created_at") >= horizon) | (F.col("last_modified_at") < F.col("created_at")))
    return flag(table, "lead_dates", df.count(), bad.count(), None, "created before the day after the as-of date; modified at or after created")


CheckFn = Callable[[Lakehouse], dict]

CHECKS: list[CheckFn] = [
    lambda lh: check_not_null(lh, "bronze_pg_metric_snapshot", ["row_hash", "raw_payload", "ingest_run_id"]),
    lambda lh: check_unique_key(lh, "bronze_pg_metric_snapshot", ["row_hash"]),
    lambda lh: check_not_null(lh, "silver_tenant", ["id", "slug"]),
    lambda lh: check_unique_key(lh, "silver_tenant", ["id"]),
    lambda lh: check_not_null(lh, "silver_platform_connection", ["id", "tenant_id", "platform", "external_account"]),
    lambda lh: check_unique_key(lh, "silver_platform_connection", ["id"]),
    lambda lh: check_not_null(lh, "silver_metric", ["key", "unit", "kind"]),
    lambda lh: check_unique_key(lh, "silver_metric", ["key"]),
    lambda lh: check_not_null(lh, "silver_daily_figure", ["tenant_id", "connection_id", "metric_key", "observed_on", "value_numeric", "synced_at"]),
    lambda lh: check_unique_key(lh, "silver_daily_figure", ["connection_id", "metric_key", "observed_on"]),
    check_accepted_range,
    check_known_metric,
    check_freshness,
    lambda lh: check_not_null(lh, "silver_lead", ["org_id", "lead_id", "created_at", "last_modified_at", "is_converted"]),
    lambda lh: check_unique_key(lh, "silver_lead", ["org_id", "lead_id"]),
    check_lead_dates,
    lambda lh: check_not_null(lh, "gold_fact_daily_figure", ["tenant_key", "source_key", "date_key", "metric_key", "figure_value"]),
    lambda lh: check_unique_key(lh, "gold_fact_daily_figure", ["source_key", "date_key", "metric_key"]),
    check_orphan_keys,
    check_reconciliation,
    lambda lh: check_unique_key(lh, "gold_rpt_daily_store_and_ad_spend", ["tenant_key", "report_date"]),
    lambda lh: check_not_null(lh, "gold_rpt_daily_store_and_ad_spend", ["tenant_key", "report_date"]),
    check_freshness_report,
]


def run_quality(lh: Lakehouse, run_id: str) -> list[dict]:
    """Run every check, write the flags (one row per table and check), and return them."""
    lh.register_all()
    now = datetime.now(timezone.utc)
    flags = [fn(lh) for fn in CHECKS]
    rows = [(run_id, now, f["table_name"], f["check_name"], f["status"], f["rows_checked"], f["rows_failed"], f["threshold"], f["detail"]) for f in flags]
    lh.append(config.OPS_QUALITY_FLAGS, frame(lh.spark, rows, FLAG_SCHEMA))
    return flags


def key_failures(flags: list[dict]) -> list[dict]:
    return [f for f in flags if f["status"] == "FAIL" and f["table_name"] in config.KEY_TABLES]
