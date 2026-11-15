"""Silver: typed, deduplicated on the business key, upserted with a Delta MERGE.

Every bronze row of a run that silver has not yet taken ends in exactly one of six places, and the run log records each count:

    inserted            a new business key, written
    updated             a known key whose newer revision changed a value, merged in
    unchanged           a known key, same values, nothing to write
    stale               a known key whose stored revision is newer (a late arriving older copy); ignored
    duplicate_in_batch  the same key twice in one batch; the newest revision wins, the rest are counted here
    quarantined         the row could not be read as the table's contract; it goes to silver_quarantine with the reason

so `rows read = the sum of the six`, which the tests check on seeded random messy input.

Schema evolution: a column the contract does not know, in a row that has every column it does know, flows through as a new string column (Delta
schema auto merge). A row that lacks a column the contract requires (a rename shows up as exactly that) or whose value cannot be read as the contract's
type (a retype) is quarantined with the reason. It is never turned into a null.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from delta.tables import DeltaTable
from pyspark.sql import Column, DataFrame, Window
from pyspark.sql import functions as F

from . import config
from .bronze import QUARANTINE_SCHEMA
from .warehouse import Lakehouse
from .frames import frame

CHECKPOINT_SCHEMA = "silver_table string, bronze_run_id string, silver_run_id string, processed_at timestamp"
LINEAGE = ("source_version", "bronze_run_id", "row_hash", "silver_loaded_at")
SAFE_NAME = r"^[A-Za-z][A-Za-z0-9_]{0,62}$"

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_SFID = r"^[a-zA-Z0-9]{15}([a-zA-Z0-9]{3})?$"


@dataclass(frozen=True)
class Col:
    source: str          # the key in the bronze payload
    name: str            # the silver column
    type: str            # string | bigint | date | timestamp | boolean | uuid | sfid
    required: bool = False


@dataclass(frozen=True)
class SilverSpec:
    table: str
    bronze: str
    source_table: str
    key: tuple[str, ...]            # silver column names
    columns: tuple[Col, ...]
    version: str | None = None      # silver column that orders revisions of one key; None means the ingest time
    account_col: str | None = None  # silver column filled from the bronze source_account
    ignore: tuple[str, ...] = ()    # payload keys that are neither contract nor drift
    date_col: str | None = None     # the day a row belongs to, for counting late arrivals


def typed_expr(raw: Column, kind: str) -> Column:
    """The typed value, or null when the text is not that type. Written as explicit patterns so that '12.5' is never quietly a bigint."""
    if kind == "string":
        return F.when(F.trim(raw) != "", raw)
    if kind == "bigint":
        return F.when(raw.rlike(r"^-?[0-9]{1,18}$"), raw.cast("bigint"))
    if kind == "date":
        return F.when(raw.rlike(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), raw.try_cast("date"))
    if kind == "timestamp":
        return F.try_to_timestamp(raw)
    if kind == "boolean":
        return F.when(F.lower(raw) == "true", F.lit(True)).when(F.lower(raw) == "false", F.lit(False))
    if kind == "uuid":
        return F.when(raw.rlike(_UUID), F.lower(raw))
    if kind == "sfid":
        return F.when(raw.rlike(_SFID), raw)
    raise ValueError("unknown type " + kind)


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


_ARROW = {"string": "string", "uuid": "string", "sfid": "string", "bigint": "bigint", "date": "date", "timestamp": "timestamp", "boolean": "boolean"}


def ensure_silver(lh: Lakehouse, spec: SilverSpec) -> None:
    """Create the silver table empty, typed from the contract, so gold SQL and the quality checks can read it before its first row arrives."""
    if lh.exists(spec.table):
        return
    fields = [f"{c.name} {_ARROW[c.type]}" for c in spec.columns]
    if spec.account_col:
        fields.append(f"{spec.account_col} string")
    fields += ["source_version timestamp", "bronze_run_id string", "row_hash string", "silver_loaded_at timestamp"]
    frame(lh.spark, [], ", ".join(fields)).write.format("delta").save(lh.path(spec.table))
    lh.register(spec.table)


def pending_runs(lh: Lakehouse, spec: SilverSpec) -> list[str]:
    if not lh.exists(spec.bronze):
        return []
    bronze = lh.read(spec.bronze).where(F.col("source_table") == spec.source_table).select("ingest_run_id").distinct()
    if lh.exists(config.OPS_CHECKPOINT):
        done = lh.read(config.OPS_CHECKPOINT).where(F.col("silver_table") == spec.table).select(F.col("bronze_run_id").alias("ingest_run_id"))
        bronze = bronze.join(done, "ingest_run_id", "left_anti")
    return sorted(r[0] for r in bronze.collect())


def parse_batch(batch: DataFrame, spec: SilverSpec) -> DataFrame:
    """Add the parsed map, one typed column per contract column, the reasons a row is refused and the keys the contract does not know."""
    m = F.from_json(F.col("raw_payload"), "map<string,string>")
    df = batch.withColumn("_m", m)
    known = [c.source for c in spec.columns] + list(spec.ignore)
    df = df.withColumn("_unexpected", F.array_except(F.map_keys(F.col("_m")), F.array(*[F.lit(k) for k in known])))
    reasons = []
    for c in spec.columns:
        raw = F.col("_m").getItem(c.source)
        df = df.withColumn(c.name, typed_expr(raw, c.type))
        present = raw.isNotNull() & (F.trim(raw) != "")
        if c.required:
            reasons.append(F.when(~present, F.lit("missing_required:" + c.source)))
        reasons.append(F.when(present & F.col(c.name).isNull(),
                              F.concat(F.lit(f"retyped:{c.source} is not a {c.type}, got '"), F.substring(raw, 1, 40), F.lit("'"))))
    has_missing = F.lit(False)
    for c in spec.columns:
        if c.required:
            raw = F.col("_m").getItem(c.source)
            has_missing = has_missing | ~(raw.isNotNull() & (F.trim(raw) != ""))
    reasons.append(F.when(has_missing & (F.size("_unexpected") > 0), F.concat(F.lit("unexpected columns: "), F.concat_ws(",", F.col("_unexpected")))))
    unsafe = F.filter(F.col("_unexpected"), lambda x: ~x.rlike(SAFE_NAME))
    reasons.append(F.when(F.size(unsafe) > 0, F.concat(F.lit("unsafe_column_name: "), F.concat_ws(",", unsafe))))
    reasons.append(F.when(F.col("_m").isNull(), F.lit("payload is not a JSON object")))
    return df.withColumn("_reason", F.concat_ws("; ", *reasons))


def run_silver(lh: Lakehouse, spec: SilverSpec, silver_run_id: str, *, runs: list[str] | None = None) -> dict:
    spark = lh.spark
    runs = runs if runs is not None else pending_runs(lh, spec)
    counts = dict.fromkeys(("read", "inserted", "updated", "unchanged", "stale", "duplicate_in_batch", "quarantined", "late_arrival"), 0)
    counts["bronze_runs"] = len(runs)
    if not runs:
        return counts
    ensure_silver(lh, spec)
    now = datetime.now(timezone.utc)
    batch = (lh.read(spec.bronze).where((F.col("source_table") == spec.source_table) & F.col("ingest_run_id").isin(runs))
             .select("ingest_run_id", "ingested_at", "source_account", "row_hash", "raw_payload"))
    parsed = parse_batch(batch, spec).cache()
    split = {bool(r[0]): int(r[1]) for r in parsed.groupBy(F.col("_reason") == "").count().collect()}
    counts["quarantined"] = split.get(False, 0)
    counts["read"] = split.get(True, 0) + counts["quarantined"]

    bad = parsed.where(F.col("_reason") != "")
    good = parsed.where(F.col("_reason") == "")

    # rows that cannot be read as the contract go to quarantine with the reason
    if counts["quarantined"]:
        q = bad.select(F.lit(spec.table).alias("table_name"), F.lit(silver_run_id).alias("run_id"), F.lit(now).cast("timestamp").alias("quarantined_at"),
                       "row_hash", F.col("_reason").alias("reason"), "raw_payload").dropDuplicates(["row_hash", "reason"])
        if lh.exists(config.SILVER_QUARANTINE):
            seen = lh.read(config.SILVER_QUARANTINE).where(F.col("table_name") == spec.table).select("row_hash", "reason")
            q = q.join(seen, ["row_hash", "reason"], "left_anti")
        lh.append(config.SILVER_QUARANTINE, q.select(*[f.name for f in QUARANTINE_SCHEMA]))

    if counts["read"] > counts["quarantined"]:
        # added columns flow through as strings
        extras = sorted({r[0] for r in good.select(F.explode("_unexpected")).distinct().collect()})
        extra_cols: dict[str, str] = {}
        taken = {c.name for c in spec.columns} | set(LINEAGE) | ({spec.account_col} if spec.account_col else set())
        for key in extras:
            name = snake(key)
            extra_cols[key] = ("x_" + name) if name in taken else name

        version_expr = F.col(spec.version) if spec.version else F.col("ingested_at")
        cols = [F.col(c.name) for c in spec.columns]
        if spec.account_col:
            cols.append(F.col("source_account").alias(spec.account_col))
        cols += [F.col("_m").getItem(k).alias(name) for k, name in extra_cols.items()]
        typed = good.select(*cols, version_expr.alias("source_version"), F.col("ingest_run_id").alias("bronze_run_id"), "row_hash", "ingested_at")

        # one row per key: the newest revision wins, the others are counted as duplicates in the batch
        w = Window.partitionBy(*spec.key).orderBy(F.col("source_version").desc(), F.col("ingested_at").desc(), F.col("row_hash").desc())
        ranked = typed.withColumn("_rn", F.row_number().over(w))

        cur = lh.read(spec.table)
        compare = [c for c in typed.columns if c in cur.columns and c not in spec.key and c not in LINEAGE and c != spec.version and c != "ingested_at"]
        e = cur.select(*[F.col(k).alias("e_" + k) for k in spec.key], F.col("source_version").alias("e_version"),
                       *[F.col(c).alias("e_" + c) for c in compare])
        joined = ranked.join(e, [F.col(k) == F.col("e_" + k) for k in spec.key], "left")
        same = F.lit(True)
        for c in compare:
            same = same & F.col(c).eqNullSafe(F.col("e_" + c))
        category = (F.when(F.col("_rn") > 1, F.lit("duplicate_in_batch"))
                    .when(F.col("e_" + spec.key[0]).isNull(), F.lit("inserted"))
                    .when(F.col("source_version") < F.col("e_version"), F.lit("stale"))
                    .when(same, F.lit("unchanged"))
                    .otherwise(F.lit("updated")))
        max_day = cur.agg(F.max(spec.date_col)).collect()[0][0] if spec.date_col else None
        late = (F.col("_category") == "inserted") & (F.col(spec.date_col) < F.lit(max_day)) if (spec.date_col and max_day is not None) else F.lit(False)
        classified = joined.withColumn("_category", category).withColumn("_late", late).cache()
        for cat, is_late, n in classified.groupBy("_category", "_late").count().collect():
            counts[cat] += int(n)
            counts["late_arrival"] += int(n) if is_late else 0

        if counts["inserted"] + counts["updated"] > 0:
            to_write = (classified.where(F.col("_category").isin("inserted", "updated"))
                        .select(*typed.columns).drop("ingested_at").withColumn("silver_loaded_at", F.lit(now).cast("timestamp")))
            cond = " AND ".join(f"t.{k} <=> s.{k}" for k in spec.key)
            (DeltaTable.forPath(lh.spark, lh.path(spec.table)).alias("t").merge(to_write.alias("s"), cond)
             .whenMatchedUpdateAll().whenNotMatchedInsertAll().execute())
            lh.register(spec.table)
        classified.unpersist()

    # the bronze runs are done once the merge has committed; a crash before this line only means they are read again, and a merge twice is a no-op
    done = frame(spark, [(spec.table, r, silver_run_id, now) for r in runs], CHECKPOINT_SCHEMA)
    lh.append(config.OPS_CHECKPOINT, done)
    parsed.unpersist()
    assert counts["read"] == sum(counts[k] for k in ("inserted", "updated", "unchanged", "stale", "duplicate_in_batch", "quarantined")), counts
    return counts


def _cols(*items: tuple) -> tuple[Col, ...]:
    return tuple(Col(*i) for i in items)


SPECS: dict[str, SilverSpec] = {s.table: s for s in (
    SilverSpec(
        "silver_tenant", "bronze_pg_tenant", "tenant", ("id",),
        _cols(("id", "id", "uuid", True), ("slug", "slug", "string", True), ("name", "name", "string", True), ("created_at", "created_at", "timestamp")),
    ),
    SilverSpec(
        "silver_platform_connection", "bronze_pg_platform_connection", "platform_connection", ("id",),
        _cols(("id", "id", "uuid", True), ("tenant_id", "tenant_id", "uuid", True), ("platform", "platform", "string", True),
              ("external_account", "external_account", "string", True), ("display_name", "display_name", "string"),
              ("status", "status", "string"), ("created_at", "created_at", "timestamp")),
    ),
    SilverSpec(
        "silver_metric", "bronze_pg_metric", "metric", ("key",),
        _cols(("key", "key", "string", True), ("label", "label", "string", True), ("unit", "unit", "string", True), ("kind", "kind", "string", True)),
    ),
    SilverSpec(
        "silver_daily_figure", "bronze_pg_metric_snapshot", "metric_snapshot", ("connection_id", "metric_key", "observed_on"),
        _cols(("tenant_id", "tenant_id", "uuid", True), ("connection_id", "connection_id", "uuid", True), ("metric_key", "metric_key", "string", True),
              ("observed_on", "observed_on", "date", True), ("value_numeric", "value_numeric", "bigint", True), ("currency", "currency", "string"),
              ("synced_at", "synced_at", "timestamp", True)),
        version="synced_at", date_col="observed_on",
    ),
    SilverSpec(
        "silver_lead", "bronze_sf_lead", "lead", ("org_id", "lead_id"),
        _cols(("Id", "lead_id", "sfid", True), ("CreatedDate", "created_at", "timestamp", True), ("LastModifiedDate", "last_modified_at", "timestamp", True),
              ("LeadSource", "lead_source", "string"), ("Status", "status", "string"), ("IsConverted", "is_converted", "boolean", True),
              ("ConvertedDate", "converted_date", "date")),
        version="last_modified_at", account_col="org_id", ignore=("attributes",),
    ),
)}
