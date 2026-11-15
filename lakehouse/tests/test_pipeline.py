"""The whole run: idempotent reruns, new data flowing through, a failed step and a bad REST page leaving earlier layers intact, the quality exit code, the cost report."""
from __future__ import annotations

import csv
import re
import shutil
from datetime import timedelta

import pytest
from pyspark.sql import functions as F

from lakehouse import config, costs, gold, run as run_cli
from lakehouse.pipeline import PgSource, RestSource, run_pipeline
from lakehouse.sample_data import SF_ORG_ID
from lakehouse.stand_in import StandInSalesforce
from lakehouse.tests.conftest import AS_OF, open_copy

LAYERS = ("bronze_", "silver_", "gold_")
NAME = re.compile(r"^(bronze_[a-z0-9]+_[a-z0-9_]+|silver_[a-z0-9_]+|gold_(dim|fact|rpt)_[a-z0-9_]+|ops_[a-z0-9_]+)$")


def state(lh) -> dict:
    """(Delta version, row count) of every bronze, silver and gold table."""
    return {t: (lh.version(t), lh.count(t)) for t in lh.table_names() if t.startswith(LAYERS)}


def steps_of(lh, run_id) -> dict:
    return {r["step"]: r for r in lh.read(config.OPS_RUN_LOG).where(F.col("run_id") == run_id).collect()}


@pytest.fixture
def gl(spark, built, tmp_path):
    return open_copy(spark, built.path, tmp_path / "copy")


@pytest.fixture
def more_data(built, tmp_path):
    """The CSV export with one more day of figures for the first Shopify source, as the Postgres read would find after the next sync."""
    export = tmp_path / "export2"
    shutil.copytree(built.export, export)
    shop = next(c for c in built.world.connections if c["platform"] == "shopify")
    new_day = AS_OF + timedelta(days=1)
    rows = [[shop["tenant_id"], shop["id"], key, new_day.isoformat(), str(value), cur, f"{new_day + timedelta(days=1)}T03:00:00+00:00"]
            for key, value, cur in (("orders", 30, ""), ("gross_sales", 126000, "USD"), ("refunds", 0, "USD"), ("net_sales", 126000, "USD"), ("customers", 24, ""))]
    with (export / "metric_snapshot.csv").open("a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    return export, len(rows), new_day


def test_a_second_run_on_the_same_input_writes_nothing_and_adds_no_version(gl, built):
    before = state(gl)
    ops_before = gl.version(config.OPS_RUN_LOG)
    code = run_cli.main(["--source", "csv", "--warehouse", str(gl.root), "--export-dir", str(built.export), "--rest-url", built.stand_in.url,
                         "--rest-token", "stand-in-token", "--org-id", SF_ORG_ID, "--as-of", AS_OF.isoformat()])
    assert code == 0
    assert state(gl) == before
    assert gl.version(config.OPS_RUN_LOG) > ops_before  # the run itself is logged
    log = gl.read(config.OPS_RUN_LOG)
    latest = log.orderBy(F.col("started_at").desc()).limit(1).collect()[0]["run_id"]
    for name, row in steps_of(gl, latest).items():
        if name.startswith(("ingest_", "silver_", "gold_")):
            assert row["rows_written"] == 0, (name, row["rows_written"])


def test_new_figures_and_a_revised_lead_flow_through_and_only_what_changed_changes(gl, built, more_data):
    export, added, new_day = more_data
    lead_id = next(r["lead_id"] for r in gl.read("silver_lead").where("is_converted = false").limit(1).collect())
    before = state(gl)
    with StandInSalesforce([dict(x) for x in built.world.leads[SF_ORG_ID]], default_batch=40) as si:
        si.revise(lead_id, "2026-11-01T22:00:00.000+0000", Status="Working - Contacted")
        res = run_pipeline(gl, PgSource("csv", export_dir=export), RestSource(si.url, "stand-in-token", SF_ORG_ID, batch_size=40, backoff=0.01))
    assert res.status in ("SUCCEEDED", "FAILED_QUALITY"), res.error
    s = steps_of(gl, res.run_id)
    assert s["ingest_pg_metric_snapshot"]["rows_written"] == added
    assert s["ingest_sf_lead"]["rows_written"] == 1
    assert s["silver_daily_figure"]["rows_written"] == added and s["silver_lead"]["rows_written"] == 1
    after = state(gl)
    assert after["silver_daily_figure"][1] == before["silver_daily_figure"][1] + added and after["silver_lead"][1] == before["silver_lead"][1]
    assert after["bronze_sf_lead"][1] == before["bronze_sf_lead"][1] + 1
    for untouched in ("bronze_pg_tenant", "bronze_pg_platform_connection", "silver_tenant", "silver_platform_connection", "silver_metric", "gold_dim_tenant", "gold_dim_metric"):
        assert after[untouched] == before[untouched], untouched
    assert gl.read("silver_lead").where(F.col("lead_id") == lead_id).collect()[0]["status"] == "Working - Contacted"
    assert gl.read("gold_dim_date").agg(F.max("date_day")).collect()[0][0] == new_day


def test_a_failing_step_is_logged_with_its_error_and_the_layers_before_it_stay_intact(gl, built, more_data):
    export, added, _ = more_data
    before = state(gl)
    res = run_pipeline(gl, PgSource("csv", export_dir=export), RestSource(built.stand_in.url, "stand-in-token", SF_ORG_ID, batch_size=40, backoff=0.01),
                       fail_at="gold_fact_daily_figure")
    assert res.status == "FAILED" and res.failed_step == "gold_fact_daily_figure" and res.exit_code == 1
    s = steps_of(gl, res.run_id)
    assert s["gold_fact_daily_figure"]["status"] == "FAILED" and "injected failure at step gold_fact_daily_figure" in s["gold_fact_daily_figure"]["error"]
    assert s["silver_daily_figure"]["status"] == "SUCCEEDED" and s["ingest_pg_metric_snapshot"]["status"] == "SUCCEEDED" and s["gold_dim_date"]["status"] == "SUCCEEDED"
    for later in ("gold_rpt_daily_store_and_ad_spend", "gold_rpt_data_freshness", "quality_checks"):
        assert s[later]["status"] == "SKIPPED" and "gold_fact_daily_figure failed" in s[later]["error"]
    mid = state(gl)
    assert mid["gold_fact_daily_figure"] == before["gold_fact_daily_figure"]            # the failed layer kept its last good content
    assert mid["silver_daily_figure"][1] == before["silver_daily_figure"][1] + added    # the layers before it landed their rows
    assert mid["bronze_pg_metric_snapshot"][1] == before["bronze_pg_metric_snapshot"][1] + added
    # the next run picks up exactly where it stopped: bronze and silver have nothing left to do, gold catches up
    again = run_pipeline(gl, PgSource("csv", export_dir=export), RestSource(built.stand_in.url, "stand-in-token", SF_ORG_ID, batch_size=40, backoff=0.01))
    assert again.status == "SUCCEEDED", again.error
    end = state(gl)
    assert end["silver_daily_figure"] == mid["silver_daily_figure"] and end["bronze_pg_metric_snapshot"] == mid["bronze_pg_metric_snapshot"]
    assert end["gold_fact_daily_figure"][1] == before["gold_fact_daily_figure"][1] + added


def test_a_corrupt_rest_page_fails_that_step_keeps_the_page_and_leaves_the_rest_alone(gl, built, more_data):
    export, added, _ = more_data
    before = state(gl)
    with StandInSalesforce([dict(x) for x in built.world.leads[SF_ORG_ID]], default_batch=40) as si:
        si.revise(built.world.leads[SF_ORG_ID][0]["Id"], "2026-11-01T23:00:00.000+0000", Status="Working - Contacted")
        si.corrupt_page = 1
        res = run_pipeline(gl, PgSource("csv", export_dir=export), RestSource(si.url, "stand-in-token", SF_ORG_ID, batch_size=40, backoff=0.01))
        assert res.status == "FAILED" and res.failed_step == "ingest_sf_lead" and "page 1 is not valid JSON" in res.error
        s = steps_of(gl, res.run_id)
        assert s["ingest_sf_lead"]["status"] == "FAILED" and s["silver_lead"]["status"] == "SKIPPED" and s["quality_checks"]["status"] == "SKIPPED"
        q = gl.read(config.BRONZE_QUARANTINE).where("table_name = 'bronze_sf_lead'").collect()
        assert len(q) == 1 and q[0]["raw_payload"].startswith('{"totalSize"') and "not valid JSON" in q[0]["reason"]
        mid = state(gl)
        assert mid["bronze_sf_lead"] == before["bronze_sf_lead"] and mid["silver_lead"] == before["silver_lead"]
        assert mid["bronze_pg_metric_snapshot"][1] == before["bronze_pg_metric_snapshot"][1] + added  # the Postgres ingest before it is kept
        si.corrupt_page = None
        again = run_pipeline(gl, PgSource("csv", export_dir=export), RestSource(si.url, "stand-in-token", SF_ORG_ID, batch_size=40, backoff=0.01))
    assert again.status == "SUCCEEDED", again.error
    assert steps_of(gl, again.run_id)["ingest_sf_lead"]["rows_written"] == 1


def test_a_failed_quality_check_ends_the_run_with_exit_code_2_and_names_the_check(gl, built):
    gl.as_of = AS_OF + timedelta(days=6)
    res = run_pipeline(gl, built.pg, built.rest)
    assert res.status == "FAILED_QUALITY" and res.exit_code == 2
    row = steps_of(gl, res.run_id)["quality_checks"]
    assert row["status"] == "FAILED" and "silver_daily_figure.freshness" in row["error"] and "gold_rpt_data_freshness.freshness" in row["error"]
    assert gl.read(config.OPS_QUALITY_FLAGS).where(F.col("run_id") == res.run_id).where("status = 'FAIL'").count() >= 2


def test_the_cost_report_ranks_jobs_by_seconds(built):
    ranked = costs.rank_jobs(built.lh).collect()
    assert ranked and len(ranked) == 5 + 5 + len(gold.GOLD_ORDER) + 1
    seconds = [r["total_seconds"] for r in ranked]
    assert seconds == sorted(seconds, reverse=True)
    top = ranked[0]
    assert top["runs"] >= 1 and top["rows_read"] is not None and {"avg_seconds", "rows_written", "rows_read_per_second"} <= set(top.asDict())
    assert len(costs.rank_jobs(built.lh, top=3).collect()) == 3


def test_the_built_run_succeeded_and_logged_every_step(built):
    assert built.result.status == "SUCCEEDED", built.result.error
    log = built.lh.read(config.OPS_RUN_LOG).where(F.col("run_id") == built.result.run_id)
    steps = {r["step"]: r for r in log.collect()}
    assert len(steps) == 5 + 5 + len(gold.GOLD_ORDER) + 1
    assert {r["status"] for r in steps.values()} == {"SUCCEEDED"}
    assert steps["ingest_pg_metric_snapshot"]["rows_read"] == len(built.world.snapshots) == steps["ingest_pg_metric_snapshot"]["rows_written"]
    assert steps["ingest_sf_lead"]["rows_written"] == sum(len(v) for v in built.world.leads.values())
    assert all(r["seconds"] >= 0 and r["started_at"] <= r["ended_at"] for r in steps.values())


def test_every_gold_table_is_a_sql_file_and_every_name_follows_the_standard(built):
    for name in gold.GOLD_ORDER:
        assert (config.SQL_DIR / f"{name}.sql").is_file()
    assert {p.stem for p in config.SQL_DIR.glob("*.sql")} == set(gold.GOLD_ORDER)
    for table in built.lh.table_names():
        assert NAME.match(table) or table in (config.BRONZE_QUARANTINE, config.SILVER_QUARANTINE), table
    layers = {t.split("_")[0] for t in built.lh.table_names()}
    assert {"bronze", "silver", "gold", "ops"} <= layers
