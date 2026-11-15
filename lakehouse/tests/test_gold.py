"""Gold: the dimensional model and the two reporting tables, built from the SQL files in lakehouse/sql/, on six days of the sample world."""
from __future__ import annotations

from datetime import timedelta

import pytest
from pyspark.sql import functions as F

from lakehouse import gold
from lakehouse.tests.conftest import AS_OF, open_copy

@pytest.fixture
def gl(spark, mini, tmp_path):
    return open_copy(spark, mini.path, tmp_path / "copy")


def test_the_fact_has_one_row_per_source_day_and_metric_and_every_key_has_a_dimension_row(gl):
    fact = gl.read("gold_fact_daily_figure")
    assert fact.count() == fact.select("source_key", "date_key", "metric_key").distinct().count()
    for dim, fk in (("gold_dim_tenant", "tenant_key"), ("gold_dim_source", "source_key"), ("gold_dim_date", "date_key"), ("gold_dim_metric", "metric_key")):
        assert fact.join(gl.read(dim).select(fk).distinct(), fk, "left_anti").count() == 0, dim


def test_the_fact_holds_the_store_ad_and_site_figures_and_the_lead_counts_made_from_records(mini, gl):
    by_metric = {r["metric_key"]: r["n"] for r in gl.read("gold_fact_daily_figure").groupBy("metric_key").agg(F.count("*").alias("n")).collect()}
    assert {"orders", "net_sales", "ad_spend", "sessions", "sf_leads_created"} <= set(by_metric)
    leads = sum(len(v) for v in mini.world.leads.values())
    total_created = gl.read("gold_fact_daily_figure").where("metric_key = 'sf_leads_created'").agg(F.sum("figure_value")).collect()[0][0]
    assert total_created == leads
    money = gl.read("gold_fact_daily_figure").where("metric_key IN ('net_sales', 'ad_spend')")
    assert money.where(F.col("currency").isNull()).count() == 0


def test_gold_totals_equal_silver_totals_per_metric(gl):
    silver = {r["metric_key"]: r["t"] for r in gl.read("silver_daily_figure").groupBy("metric_key").agg(F.sum("value_numeric").alias("t")).collect()}
    gold_totals = {r["metric_key"]: r["t"] for r in gl.read("gold_fact_daily_figure").groupBy("metric_key").agg(F.sum("figure_value").alias("t")).collect()}
    for key, total in silver.items():
        assert gold_totals[key] == total, key


def test_the_date_dimension_has_no_gaps(gl):
    d = gl.read("gold_dim_date")
    lo, hi = d.agg(F.min("date_day"), F.max("date_day")).collect()[0]
    assert d.count() == (hi - lo).days + 1 and d.select("date_key").distinct().count() == d.count()
    assert hi == AS_OF


def test_the_daily_report_puts_store_and_ads_on_one_row_and_divides_in_the_same_currency_only(gl):
    r = gl.read("gold_rpt_daily_store_and_ad_spend")
    assert r.count() == r.select("tenant_key", "report_date").distinct().count()
    usd = r.where("store_currency = 'USD' AND ad_currency = 'USD' AND ad_spend_minor > 0").collect()[0]
    assert usd["net_sales_per_ad_spend"] == round(usd["net_sales_minor"] / usd["ad_spend_minor"], 2)
    assert r.where("store_currency != ad_currency AND net_sales_per_ad_spend IS NOT NULL").count() == 0


def test_the_freshness_view_has_a_status_per_source_and_the_limit_is_inclusive(gl):
    f = gl.read("gold_rpt_data_freshness")
    assert f.count() == gl.read("gold_dim_source").count()
    assert {r["freshness_status"] for r in f.collect()} == {"FRESH"}
    assert f.where("limit_days != 2").count() == 0 and f.where(F.col("as_of_date") != F.lit(AS_OF.isoformat()).cast("date")).count() == 0
    # a source whose newest day is exactly the limit behind is FRESH; one day further is STALE
    gl.as_of = AS_OF + timedelta(days=2)
    gold.build_gold_table(gl, "gold_rpt_data_freshness")
    assert {r["freshness_status"] for r in gl.read("gold_rpt_data_freshness").collect()} == {"FRESH"}
    gl.as_of = AS_OF + timedelta(days=3)
    gold.build_gold_table(gl, "gold_rpt_data_freshness")
    assert {r["freshness_status"] for r in gl.read("gold_rpt_data_freshness").collect()} == {"STALE"}


def test_a_source_with_no_figures_is_no_data_not_fresh(gl):
    keys = ", ".join("'" + r["source_key"] + "'" for r in gl.read("gold_dim_source").where("platform = 'ga4'").collect())
    gl.spark.sql(f"DELETE FROM delta.`{gl.path('gold_fact_daily_figure')}` WHERE source_key IN ({keys})")
    gold.build_gold_table(gl, "gold_rpt_data_freshness")
    statuses = {r["platform"]: r["freshness_status"] for r in gl.read("gold_rpt_data_freshness").collect()}
    assert statuses["ga4"] == "NO_DATA" and statuses["shopify"] == "FRESH"


def test_a_rebuild_on_unchanged_silver_adds_no_delta_version(gl):
    before = gl.versions(list(gold.GOLD_ORDER))
    for name in gold.GOLD_ORDER:
        assert gold.build_gold_table(gl, name)["written"] == 0
    assert gl.versions(list(gold.GOLD_ORDER)) == before
