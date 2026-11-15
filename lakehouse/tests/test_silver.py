"""Silver: typed, deduplicated on the business key, upserted by MERGE; late and duplicate rows; schema evolution; every bronze row accounted for."""
from __future__ import annotations

from delta.tables import DeltaTable
from pyspark.sql import functions as F

from lakehouse import config, silver
from lakehouse.tests.helpers import CONN, CONN2, day, figures, land, lead, outcome_sum, silver_daily, snap, synced

S = "silver_daily_figure"


def run(lh, rows, n):
    land(lh, rows, run_id=f"b{n}")
    counts = silver_daily(lh, f"s{n}")
    assert counts["read"] == outcome_sum(counts), counts  # no row lost
    return counts


def test_rows_are_typed_and_inserted(lh):
    counts = run(lh, [snap(0, 5), snap(1, 7, currency=None), snap(0, 900, metric="net_sales", currency="USD")], 1)
    assert counts["inserted"] == 3 and counts["quarantined"] == 0
    types = dict(lh.read(S).dtypes)
    assert types["observed_on"] == "date" and types["value_numeric"] == "bigint" and types["synced_at"] == "timestamp"
    assert figures(lh)[(CONN, "orders", day(0))] == 5


def test_a_newer_revision_updates_in_place_with_a_merge(lh):
    run(lh, [snap(0, 5, sync=synced(0))], 1)
    counts = run(lh, [snap(0, 6, sync=synced(0, hour=9))], 2)
    assert counts["updated"] == 1 and lh.count(S) == 1 and figures(lh)[(CONN, "orders", day(0))] == 6
    assert "MERGE" in [r["operation"] for r in DeltaTable.forPath(lh.spark, lh.path(S)).history().collect()]


def test_a_late_arriving_older_copy_is_stale_and_changes_nothing(lh):
    run(lh, [snap(0, 6, sync=synced(0, hour=9))], 1)
    before = lh.version(S)
    counts = run(lh, [snap(0, 5, sync=synced(0, hour=3))], 2)
    assert counts["stale"] == 1 and counts["updated"] == 0 and figures(lh)[(CONN, "orders", day(0))] == 6 and lh.version(S) == before


def test_the_same_values_again_are_unchanged_and_write_nothing(lh):
    run(lh, [snap(0, 5, sync=synced(0))], 1)
    before = lh.version(S)
    counts = run(lh, [snap(0, 5, sync=synced(0, hour=9))], 2)
    assert counts["unchanged"] == 1 and lh.version(S) == before


def test_two_revisions_of_a_key_in_one_batch_keep_the_newest_and_count_the_other(lh):
    counts = run(lh, [snap(0, 5, sync=synced(0, hour=3)), snap(0, 8, sync=synced(0, hour=9)), snap(0, 7, sync=synced(0, hour=5))], 1)
    assert counts["inserted"] == 1 and counts["duplicate_in_batch"] == 2
    assert lh.count(S) == 1 and figures(lh)[(CONN, "orders", day(0))] == 8


def test_a_day_that_arrives_after_later_days_is_inserted_and_counted_as_late(lh):
    run(lh, [snap(5, 1), snap(6, 2)], 1)
    counts = run(lh, [snap(2, 3)], 2)
    assert counts["inserted"] == 1 and counts["late_arrival"] == 1 and lh.count(S) == 3


def test_an_added_column_flows_through_as_a_new_column(lh):
    run(lh, [snap(0, 5)], 1)
    counts = run(lh, [snap(1, 7, channel="web")], 2)
    assert counts["inserted"] == 1 and counts["quarantined"] == 0
    df = lh.read(S)
    assert "channel" in df.columns
    got = {str(r["observed_on"]): r["channel"] for r in df.collect()}
    assert got[day(1)] == "web" and got[day(0)] is None


def test_a_renamed_column_is_quarantined_with_the_reason_never_a_null(lh):
    bad = snap(0, 5)
    bad["value"] = bad.pop("value_numeric")
    counts = run(lh, [bad, snap(1, 6)], 1)
    assert counts["quarantined"] == 1 and counts["inserted"] == 1
    reason = lh.read(config.SILVER_QUARANTINE).collect()[0]["reason"]
    assert reason.startswith("missing_required:value_numeric") and "unexpected columns: value" in reason
    assert lh.read(S).where(F.col("value_numeric").isNull()).count() == 0 and lh.count(S) == 1


def test_a_retyped_or_unreadable_value_is_quarantined_with_the_reason(lh):
    rows = [snap(0, "12.5"), snap(1, "abc"), snap(2, 4.5), snap(3, 5, observed_on="2026-13-45"), snap(4, 6, connection_id="not-a-uuid"),
            snap(5, 7, sync="yesterday"), snap(7, 9, sync="2026-10-08T05:0:00+00:00"), snap(8, 9, sync="2026-10-09"), snap(6, 8)]
    counts = run(lh, rows, 1)
    assert counts["quarantined"] == 8 and counts["inserted"] == 1
    reasons = sorted(r["reason"] for r in lh.read(config.SILVER_QUARANTINE).collect())
    assert all(r.startswith("retyped:") for r in reasons), reasons
    assert any("value_numeric is not a bigint, got '12.5'" in r for r in reasons)
    assert any("observed_on is not a date" in r for r in reasons) and any("connection_id is not a uuid" in r for r in reasons)
    assert list(figures(lh)) == [(CONN, "orders", day(6))]


def test_missing_and_null_required_values_are_quarantined(lh):
    gone = snap(0, 5)
    del gone["metric_key"]
    rows = [gone, snap(1, None), snap(2, 5, tenant=None), snap(3, 5, metric="  ")]
    counts = run(lh, rows, 1)
    assert counts["quarantined"] == 4 and counts["inserted"] == 0 and lh.count(S) == 0
    assert all(r["reason"].startswith("missing_required:") for r in lh.read(config.SILVER_QUARANTINE).collect())


def test_an_unsafe_added_column_name_is_quarantined(lh):
    row = snap(0, 5)
    row["bad name!"] = "x"
    counts = run(lh, [row], 1)
    assert counts["quarantined"] == 1 and "unsafe_column_name" in lh.read(config.SILVER_QUARANTINE).collect()[0]["reason"]


def test_a_bronze_run_is_taken_once_and_a_new_run_is_taken_next(lh):
    run(lh, [snap(0, 5)], 1)
    before = lh.version(S)
    again = silver_daily(lh, "s-again")
    assert again["bronze_runs"] == 0 and again["read"] == 0 and lh.version(S) == before
    land(lh, [snap(1, 6)], run_id="b-new")
    assert silver_daily(lh, "s-new")["inserted"] == 1


def test_two_sources_with_the_same_day_and_metric_are_two_rows(lh):
    run(lh, [snap(0, 5, conn=CONN), snap(0, 9, conn=CONN2)], 1)
    assert lh.count(S) == 2


def test_leads_take_their_org_from_the_source_and_ignore_the_attributes_object(lh):
    recs = [lead(1, converted=True, converted_date="2026-10-02", modified="2026-10-02T12:00:00.000+0000"),
            lead(2, lead_id="short"), lead(3, created="2026-13-01T00:00:00.000+0000")]
    land(lh, recs, run_id="b1", table="bronze_sf_lead", source_table="lead", source_name="salesforce_rest")
    counts = silver.run_silver(lh, silver.SPECS["silver_lead"], "s1")
    assert counts["read"] == outcome_sum(counts) == 3 and counts["inserted"] == 1 and counts["quarantined"] == 2
    row = lh.read("silver_lead").collect()[0]
    assert row["org_id"] == "00DST0000000001AAA" and row["is_converted"] is True and str(row["converted_date"]) == "2026-10-02"
    assert "attributes" not in lh.read("silver_lead").columns
