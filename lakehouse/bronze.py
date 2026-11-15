"""Bronze: land what a source said, untouched, append only.

Every landed row keeps the raw payload as the source sent it (one JSON text), the source name and table, the ingest run id, the ingest time and a
SHA-256 row hash. Nothing is typed or cleaned here. A record that cannot even be read as a JSON object goes to `bronze_quarantine` with the
reason and the raw text; nothing is dropped.

A rerun on the same input writes nothing: a payload whose hash is already in the table is counted as `skipped_known` and not landed again. The
table is still append only (no update, no delete), the hash is how it stays free of copies.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from pyspark.sql import DataFrame
from pyspark.sql import functions as F
from pyspark.sql.types import StringType, StructField, StructType, TimestampType

from . import config
from .warehouse import Lakehouse
from .frames import frame

BRONZE_SCHEMA = StructType([
    StructField("source_name", StringType(), False),
    StructField("source_table", StringType(), False),
    StructField("source_account", StringType()),
    StructField("ingest_run_id", StringType(), False),
    StructField("ingested_at", TimestampType(), False),
    StructField("row_hash", StringType(), False),
    StructField("raw_payload", StringType(), False),
])

QUARANTINE_SCHEMA = StructType([
    StructField("table_name", StringType(), False),
    StructField("run_id", StringType(), False),
    StructField("quarantined_at", TimestampType(), False),
    StructField("row_hash", StringType(), False),
    StructField("reason", StringType(), False),
    StructField("raw_payload", StringType()),
])

CANDIDATES = StructType([StructField("raw_payload", StringType()), StructField("source_account", StringType())])
REJECTS = StructType([StructField("raw_payload", StringType()), StructField("reason", StringType())])


def classify_records(records: list) -> tuple[list[tuple[str, str | None]], list[tuple[str | None, str]]]:
    """Split raw records into (payload text, account) rows that are JSON objects and (raw text, reason) rows that are not.
    A record is a dict, or a text that holds a JSON object."""
    good: list[tuple[str, str | None]] = []
    bad: list[tuple[str | None, str]] = []
    for rec in records:
        account = None
        if isinstance(rec, tuple) and len(rec) == 2:
            rec, account = rec
        if isinstance(rec, (bytes, bytearray)):
            rec = rec.decode("utf-8", "replace")
        if isinstance(rec, str):
            try:
                parsed = json.loads(rec)
            except ValueError as exc:
                bad.append((rec, "not valid JSON: " + str(exc)[:120]))
                continue
            if not isinstance(parsed, dict):
                bad.append((rec, "JSON but not an object (" + type(parsed).__name__ + ")"))
                continue
            rec = parsed
        if not isinstance(rec, dict):
            bad.append((repr(rec)[:2000], "not a JSON object (" + type(rec).__name__ + ")"))
            continue
        good.append((json.dumps(rec, sort_keys=True, default=str, ensure_ascii=False), account))
    return good, bad


def land_records(lh: Lakehouse, table: str, *, source_name: str, source_table: str, run_id: str, records: list,
                 extra_rejects: list[tuple[str | None, str]] | None = None, ingested_at: datetime | None = None) -> dict:
    """Land Python records (dicts or JSON texts, optionally paired with a source account) in a bronze table."""
    good, bad = classify_records(records)
    bad = bad + list(extra_rejects or [])
    spark = lh.spark
    return land_frame(lh, table, source_name=source_name, source_table=source_table, run_id=run_id,
                      good=frame(spark, good, CANDIDATES), rejected=frame(spark, bad, REJECTS) if bad else None, ingested_at=ingested_at,
                      rejected_count=len(bad))


def frame_to_payloads(df: DataFrame, account_col: str | None = None) -> DataFrame:
    """A table read (JDBC, CSV) becomes raw payload texts: one JSON object per row, nulls kept so that a missing value and a missing column differ."""
    cols = [F.col(f"`{c}`") for c in df.columns]
    payload = F.to_json(F.struct(*cols), {"ignoreNullFields": "false"})
    account = F.col(account_col).cast("string") if account_col else F.lit(None).cast("string")
    return df.select(payload.alias("raw_payload"), account.alias("source_account"))


def land_frame(lh: Lakehouse, table: str, *, source_name: str, source_table: str, run_id: str, good: DataFrame, rejected: DataFrame | None,
               ingested_at: datetime | None = None, rejected_count: int | None = None) -> dict:
    """The one landing path. Returns the counts that the run log records and the tests check:
    read = landed + skipped_known + quarantined (every record read is one of the three)."""
    now = ingested_at or datetime.now(timezone.utc)
    good = good.withColumn("row_hash", F.sha2(F.col("raw_payload"), 256)).cache()
    n_good = good.count()
    unique = good.dropDuplicates(["row_hash"])
    if lh.exists(table):
        known = lh.read(table).where(F.col("source_table") == source_table).select("row_hash").distinct()
        fresh = unique.join(known, "row_hash", "left_anti")
    else:
        fresh = unique
    rows = fresh.select(
        F.lit(source_name).alias("source_name"), F.lit(source_table).alias("source_table"), F.col("source_account"),
        F.lit(run_id).alias("ingest_run_id"), F.lit(now).cast("timestamp").alias("ingested_at"), F.col("row_hash"), F.col("raw_payload"),
    )
    landed = lh.append(table, rows)
    good.unpersist()

    n_bad_in = 0
    quarantined = 0
    if rejected is not None and (rejected_count is None or rejected_count > 0):
        bad = rejected.withColumn("row_hash", F.sha2(F.coalesce(F.col("raw_payload"), F.lit("")), 256)).dropDuplicates(["row_hash", "reason"])
        n_bad_in = rejected_count if rejected_count is not None else rejected.count()
        q_rows = bad.select(F.lit(table).alias("table_name"), F.lit(run_id).alias("run_id"), F.lit(now).cast("timestamp").alias("quarantined_at"),
                            "row_hash", "reason", "raw_payload")
        if lh.exists(config.BRONZE_QUARANTINE):
            seen = lh.read(config.BRONZE_QUARANTINE).where(F.col("table_name") == table).select("row_hash", "reason")
            q_rows = q_rows.join(seen, ["row_hash", "reason"], "left_anti")
        quarantined = lh.append(config.BRONZE_QUARANTINE, q_rows.select(*[f.name for f in QUARANTINE_SCHEMA]))
    return {
        "read": n_good + n_bad_in,
        "landed": landed,
        "skipped_known": n_good - landed,
        "quarantined": n_bad_in,
        "quarantine_rows_written": quarantined,
    }
