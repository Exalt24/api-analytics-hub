"""The run: every step in order, each one logged, and what happens when one fails.

    ingest_pg_tenant, ingest_pg_platform_connection, ingest_pg_metric, ingest_pg_metric_snapshot, ingest_sf_lead     (bronze)
    silver_tenant, silver_platform_connection, silver_metric, silver_daily_figure, silver_lead                       (silver)
    gold_dim_tenant ... gold_rpt_data_freshness                                                                      (gold, one step per table)
    quality_checks

A step that fails is written to the run log as FAILED with the error text, every later step is written as SKIPPED naming the step that stopped the
run, and nothing already written is touched: bronze and silver are append and merge, gold is replaced only by a later successful step. Run it again
and the failed step runs again; the steps before it find nothing new to do.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from . import bronze, config, gold, quality, silver
from .bronze import frame_to_payloads, land_frame, land_records
from .rest_client import RestError, fetch_query, lead_query
from .runlog import RunLog, Step, StepFailed, new_run_id
from .sample_data import TABLE_COLUMNS
from .warehouse import Lakehouse

PG_TABLES = ("tenant", "platform_connection", "metric", "metric_snapshot")


class InjectedFailure(RuntimeError):
    """Raised on purpose by --fail-at, to show what a failed step looks like in the run log."""


@dataclass
class PgSource:
    kind: str = "jdbc"  # jdbc or csv
    url: str = config.DEFAULT_JDBC_URL
    user: str = config.DEFAULT_JDBC_USER
    password: str = config.DEFAULT_JDBC_PASSWORD
    export_dir: Path = config.DEFAULT_EXPORT_DIR


@dataclass
class RestSource:
    base_url: str
    token: str
    org_id: str
    batch_size: int = 200
    retries: int = 2
    backoff: float = 0.2


@dataclass
class RunResult:
    run_id: str
    status: str = "SUCCEEDED"            # SUCCEEDED, FAILED, FAILED_QUALITY
    failed_step: str | None = None
    error: str | None = None
    steps: list[str] = field(default_factory=list)
    flags: list[dict] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return {"SUCCEEDED": 0, "FAILED": 1, "FAILED_QUALITY": 2}[self.status]


# ---------------------------------------------------------------- reading the sources


def read_pg_table(lh: Lakehouse, src: PgSource, table: str, since: str | None) -> DataFrame:
    """The hub's Postgres tables, by JDBC (the main route) or from the CSV export (the fallback for a machine with no Postgres).
    The column list is explicit: credentials_enc and the raw jsonb are never selected."""
    cols = TABLE_COLUMNS[table]
    spark = lh.spark
    if src.kind == "csv":
        df = spark.read.option("header", "true").option("inferSchema", "false").csv(str(Path(src.export_dir) / f"{table}.csv")).select(*cols)
        if since and table == "metric_snapshot":
            df = df.where(F.col("synced_at") >= since)
        return df
    where = f" WHERE synced_at >= TIMESTAMPTZ '{since}'" if since and table == "metric_snapshot" else ""
    query = f"(SELECT {', '.join(cols)} FROM {table}{where}) AS src"
    return (spark.read.format("jdbc").option("url", src.url).option("dbtable", query).option("user", src.user).option("password", src.password)
            .option("driver", "org.postgresql.Driver").load())


def watermark(lh: Lakehouse, table: str, source_table: str, json_key: str) -> str | None:
    if not lh.exists(table):
        return None
    row = (lh.read(table).where(F.col("source_table") == source_table)
           .agg(F.max(F.get_json_object("raw_payload", "$." + json_key))).collect()[0][0])
    return row


def _minus_lookback(iso_text: str, hours: int) -> str:
    t = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (t - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def ingest_pg_table(lh: Lakehouse, run_id: str, src: PgSource, table: str, *, full_refresh: bool = False) -> dict:
    since = None
    if table == "metric_snapshot" and not full_refresh:
        mark = watermark(lh, "bronze_pg_metric_snapshot", "metric_snapshot", "synced_at")
        since = _minus_lookback(mark, config.INCREMENTAL_LOOKBACK_HOURS) if mark else None
    df = read_pg_table(lh, src, table, since)
    counts = land_frame(lh, f"bronze_pg_{table}", source_name="postgres", source_table=table, run_id=run_id,
                        good=frame_to_payloads(df), rejected=None)
    counts["since"] = since
    return counts


def ingest_sf_lead(lh: Lakehouse, run_id: str, rest: RestSource, *, full_refresh: bool = False) -> dict:
    since = None
    if not full_refresh:
        mark = watermark(lh, "bronze_sf_lead", "lead", "LastModifiedDate")
        since = _soql_minus(mark, config.INCREMENTAL_LOOKBACK_HOURS) if mark else None
    try:
        fetched = fetch_query(rest.base_url, rest.token, lead_query(since), batch_size=rest.batch_size, retries=rest.retries, backoff=rest.backoff)
    except RestError as exc:
        # what arrived is not landed (a half read is not trusted), but the page that could not be read is kept as evidence
        if exc.rejects:
            land_records(lh, "bronze_sf_lead", source_name="salesforce_rest", source_table="lead", run_id=run_id, records=[], extra_rejects=exc.rejects)
        raise
    records = [(r, rest.org_id) for r in fetched.records]
    counts = land_records(lh, "bronze_sf_lead", source_name="salesforce_rest", source_table="lead", run_id=run_id, records=records)
    counts.update({"since": since, "pages": fetched.pages, "api_calls": fetched.api_calls})
    return counts


def _soql_minus(sf_text: str, hours: int) -> str:
    moment = datetime.strptime(sf_text, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(timezone.utc) - timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- the run


def plan(lh: Lakehouse, pg: PgSource, rest: RestSource | None, flags_sink: list[dict], *, full_refresh: bool) -> list[tuple[str, Callable[[Step, str], None]]]:
    steps: list[tuple[str, Callable[[Step, str], None]]] = []

    def ingest_pg(table: str):
        def fn(s: Step, run_id: str) -> None:
            c = ingest_pg_table(lh, run_id, pg, table, full_refresh=full_refresh)
            s.rows_read, s.rows_written, s.rows_quarantined = c["read"], c["landed"], c["quarantined"]
            s.detail = {k: v for k, v in c.items() if k not in ("read", "landed", "quarantined")}
        return fn

    for t in PG_TABLES:
        steps.append((f"ingest_pg_{t}", ingest_pg(t)))
    if rest is not None:
        def ingest_leads(s: Step, run_id: str) -> None:
            c = ingest_sf_lead(lh, run_id, rest, full_refresh=full_refresh)
            s.rows_read, s.rows_written, s.rows_quarantined = c["read"], c["landed"], c["quarantined"]
            s.detail = {k: v for k, v in c.items() if k not in ("read", "landed", "quarantined")}
        steps.append(("ingest_sf_lead", ingest_leads))

    def silver_step(spec: silver.SilverSpec):
        def fn(s: Step, run_id: str) -> None:
            c = silver.run_silver(lh, spec, run_id)
            s.rows_read, s.rows_written, s.rows_quarantined = c["read"], c["inserted"] + c["updated"], c["quarantined"]
            s.detail = {k: v for k, v in c.items() if k not in ("read", "quarantined")}
        return fn

    for name, spec in silver.SPECS.items():
        steps.append((name, silver_step(spec)))

    def gold_step(name: str):
        def fn(s: Step, run_id: str) -> None:
            c = gold.build_gold_table(lh, name)
            s.rows_read, s.rows_written = c["read"], c["written"]
        return fn

    for name in gold.GOLD_ORDER:
        steps.append((name, gold_step(name)))

    def quality_step(s: Step, run_id: str) -> None:
        flags = quality.run_quality(lh, run_id)
        flags_sink.extend(flags)
        failed = quality.key_failures(flags)
        s.rows_read = len(flags)
        s.rows_written = len(flags)
        s.detail = {"pass": sum(f["status"] == "PASS" for f in flags), "fail": sum(f["status"] == "FAIL" for f in flags)}
        if failed:
            raise QualityFailed("; ".join(f"{f['table_name']}.{f['check_name']} ({f['rows_failed']} of {f['rows_checked']})" for f in failed))

    steps.append(("quality_checks", quality_step))
    return steps


class QualityFailed(Exception):
    pass


def run_pipeline(lh: Lakehouse, pg: PgSource, rest: RestSource | None = None, *, full_refresh: bool = False, fail_at: str | None = None,
                 run_id: str | None = None) -> RunResult:
    """Run every step. `fail_at` names a step that raises InjectedFailure (the environment variable LAKEHOUSE_FAIL_AT does the same)."""
    fail_at = fail_at or os.environ.get("LAKEHOUSE_FAIL_AT") or None
    log = RunLog(lh, run_id or new_run_id(), buffered=True)
    result = RunResult(log.run_id)
    lh.register_all()  # a warehouse opened in a new session (or copied) has its tables on disk but not yet in the catalog the SQL files read
    for name in silver.SPECS:
        silver.ensure_silver(lh, silver.SPECS[name])
    flags_sink: list[dict] = []
    steps = plan(lh, pg, rest, flags_sink, full_refresh=full_refresh)
    try:
        for i, (name, fn) in enumerate(steps):
            try:
                with log.step(name) as s:
                    if fail_at == name:
                        raise InjectedFailure(f"injected failure at step {name}")
                    fn(s, log.run_id)
                result.steps.append(name)
            except StepFailed as exc:
                result.failed_step, result.error = exc.step, exc.error
                result.status = "FAILED_QUALITY" if name == "quality_checks" and "QualityFailed" in exc.error else "FAILED"
                for later, _ in steps[i + 1:]:
                    log.skip(later, f"skipped: step {name} failed")
                break
    finally:
        log.flush()  # every step of the run, in one commit, whatever happened
    result.flags = list(flags_sink)
    return result
