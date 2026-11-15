"""Quality flags: every check passes on the clean warehouse and each one turns FAIL when its defect is planted; --check exits non zero."""
from __future__ import annotations

import shutil
from datetime import timedelta

import pytest
from pyspark.sql import functions as F

from lakehouse import config, gold, quality, run as run_cli
from lakehouse.tests.conftest import AS_OF, open_copy


@pytest.fixture
def gl(spark, mini, tmp_path):
    return open_copy(spark, mini.path, tmp_path / "copy")


def flags_of(lh) -> dict:
    return {(f["table_name"], f["check_name"]): f for f in quality.run_quality(lh, "q-run")}


def one(lh, fn, *args) -> dict:
    """One check on its own."""
    return fn(lh, *args)


def sql(lh, text: str) -> None:
    lh.spark.sql(text.replace("{silver}", f"delta.`{lh.path('silver_daily_figure')}`").replace("{fact}", f"delta.`{lh.path('gold_fact_daily_figure')}`"))
    lh.register_all()


def test_a_clean_warehouse_passes_every_check_and_the_flags_are_written(gl):
    flags = flags_of(gl)
    assert flags and {f["status"] for f in flags.values()} == {"PASS"}, [k for k, f in flags.items() if f["status"] != "PASS"]
    stored = gl.read(config.OPS_QUALITY_FLAGS).where(F.col("run_id") == "q-run")
    assert stored.count() == len(flags) == len(quality.CHECKS)
    assert {"table_name", "check_name", "status", "rows_checked", "rows_failed", "threshold", "detail"} <= set(stored.columns)
    assert all(f["rows_checked"] > 0 for f in flags.values())


def test_every_planted_defect_turns_its_own_flag_to_fail(gl):
    """Seven defects in one copy, each in rows no other defect touches, one run of every check."""
    sql(gl, "INSERT INTO {silver} SELECT * FROM {silver} WHERE metric_key = 'sessions' AND observed_on = DATE'2026-10-31'")                       # a duplicate business key
    sql(gl, "UPDATE {silver} SET value_numeric = NULL WHERE metric_key = 'orders' AND observed_on = DATE'2026-10-30'")                           # a null
    sql(gl, "UPDATE {silver} SET value_numeric = -7 WHERE metric_key = 'orders' AND observed_on = DATE'2026-10-28'")                             # below its range
    sql(gl, "UPDATE {silver} SET value_numeric = 99999999999999 WHERE metric_key = 'ad_clicks' AND observed_on = DATE'2026-10-29'")              # above its range
    sql(gl, "UPDATE {silver} SET metric_key = 'made_up_metric' WHERE metric_key = 'users' AND observed_on = DATE'2026-10-27'")                   # not in the vocabulary
    sql(gl, "DELETE FROM {fact} WHERE metric_key = 'sf_leads_created' AND date_key = 20261030")                                                  # gold lost a lead total
    gl.spark.sql(f"DELETE FROM delta.`{gl.path('gold_dim_metric')}` WHERE metric_key = 'customers'")                                            # a dimension row gone
    lead_id = gl.read("silver_lead").limit(1).collect()[0]["lead_id"]
    gl.spark.sql(f"UPDATE delta.`{gl.path('silver_lead')}` SET created_at = TIMESTAMP'2026-12-01 00:00:00', last_modified_at = TIMESTAMP'2026-12-01 00:00:00' WHERE lead_id = '{lead_id}'")
    gl.as_of = AS_OF + timedelta(days=3)                                                                                                        # every source three days behind a limit of two
    gl.register_all()
    gold.build_gold_table(gl, "gold_rpt_data_freshness")
    flags = flags_of(gl)

    f = flags[("silver_daily_figure", "unique_key")]
    assert f["status"] == "FAIL" and f["rows_failed"] == 3
    f = flags[("silver_daily_figure", "not_null")]
    assert f["status"] == "FAIL" and f["rows_failed"] == 3
    f = flags[("silver_daily_figure", "accepted_range")]
    assert f["status"] == "FAIL" and f["rows_failed"] == 6  # three tenants, two bad days each
    f = flags[("silver_daily_figure", "known_metric")]
    assert f["status"] == "FAIL" and f["rows_failed"] == 3
    f = flags[("silver_daily_figure", "freshness")]
    assert f["status"] == "FAIL" and f["rows_failed"] == f["rows_checked"] and "stale:" in f["detail"] and f["threshold"].startswith("2 days")
    f = flags[("gold_rpt_data_freshness", "freshness")]
    assert f["status"] == "FAIL" and f["rows_failed"] == f["rows_checked"]
    f = flags[("gold_fact_daily_figure", "reconciliation")]
    assert f["status"] == "FAIL" and f["rows_failed"] >= 2
    f = flags[("gold_fact_daily_figure", "orphan_keys")]
    assert f["status"] == "FAIL" and f["rows_failed"] > 0 and "metric_key" in f["detail"]
    f = flags[("silver_lead", "lead_dates")]
    assert f["status"] == "FAIL" and f["rows_failed"] == 1
    assert {k for k, v in flags.items() if v["status"] == "FAIL"} >= {("silver_daily_figure", c) for c in ("unique_key", "not_null", "accepted_range", "known_metric", "freshness")}


def test_net_sales_may_be_negative_but_orders_may_not(gl):
    sql(gl, "UPDATE {silver} SET value_numeric = -500 WHERE metric_key = 'net_sales' AND observed_on = DATE'2026-10-30'")
    assert one(gl, quality.check_accepted_range)["status"] == "PASS"


def test_a_source_exactly_at_the_limit_is_still_fresh(gl):
    gl.as_of = AS_OF + timedelta(days=2)
    assert one(gl, quality.check_freshness)["status"] == "PASS"
    gl.as_of = AS_OF + timedelta(days=3)
    assert one(gl, quality.check_freshness)["status"] == "FAIL"


def test_a_missing_table_is_a_fail_not_a_skipped_check(gl):
    shutil.rmtree(gl.root / "silver_lead")
    f = one(gl, quality.check_unique_key, "silver_lead", ["org_id", "lead_id"])
    assert f["status"] == "FAIL" and f["detail"] == "table does not exist"


def test_check_exits_zero_on_a_clean_warehouse_and_non_zero_when_a_key_table_fails(gl):
    argv = ["--check", "--warehouse", str(gl.root), "--as-of", AS_OF.isoformat()]
    assert run_cli.main(argv) == 0
    sql(gl, "INSERT INTO {silver} SELECT * FROM {silver} LIMIT 1")
    assert run_cli.main(argv) == 2
    log = gl.read(config.OPS_RUN_LOG).where("step = 'quality_checks' AND status = 'FAILED'").collect()
    assert log and "silver_daily_figure.unique_key" in log[0]["error"]


def test_a_failed_check_on_a_table_that_is_not_a_key_table_does_not_fail_check(gl):
    gl.spark.sql(f"INSERT INTO delta.`{gl.path('bronze_pg_metric_snapshot')}` SELECT * FROM delta.`{gl.path('bronze_pg_metric_snapshot')}` LIMIT 1")
    flag = one(gl, quality.check_unique_key, "bronze_pg_metric_snapshot", ["row_hash"])
    assert flag["status"] == "FAIL"
    assert quality.key_failures([flag]) == []
