"""Bronze: append only, raw payload kept, bad records quarantined with a reason, a rerun on the same input writes nothing."""
from __future__ import annotations

import hashlib
import json

from delta.tables import DeltaTable
from pyspark.sql import functions as F

from lakehouse import config
from lakehouse.tests.helpers import land, snap

T = "bronze_pg_metric_snapshot"


def test_landed_row_keeps_raw_payload_source_run_time_and_hash(lh):
    row = snap(0, 5)
    counts = land(lh, [row], run_id="run-A")
    assert counts == {"read": 1, "landed": 1, "skipped_known": 0, "quarantined": 0, "quarantine_rows_written": 0}
    got = lh.read(T).collect()[0]
    assert got["source_name"] == "postgres" and got["source_table"] == "metric_snapshot"
    assert got["ingest_run_id"] == "run-A" and got["ingested_at"] is not None
    assert json.loads(got["raw_payload"]) == row
    assert got["row_hash"] == hashlib.sha256(got["raw_payload"].encode()).hexdigest()


def test_unreadable_records_go_to_quarantine_with_a_reason_and_the_raw_text(lh):
    records = [snap(0, 5), "not json {", "[1, 2]", 42, None, '"just a string"']
    counts = land(lh, records)
    assert counts["read"] == 6 and counts["landed"] == 1 and counts["quarantined"] == 5
    q = {r["raw_payload"]: r["reason"] for r in lh.read(config.BRONZE_QUARANTINE).collect()}
    assert len(q) == 5
    assert q["not json {"].startswith("not valid JSON")
    assert q["[1, 2]"].startswith("JSON but not an object")
    assert "not a JSON object (int)" in q["42"]
    assert "not a JSON object (NoneType)" in q["None"]
    assert q['"just a string"'].startswith("JSON but not an object")
    assert lh.count(T) == 1


def test_every_record_read_is_landed_skipped_or_quarantined(lh):
    land(lh, [snap(0, 1), snap(1, 2)], run_id="r1")
    counts = land(lh, [snap(0, 1), snap(1, 2), snap(2, 3), snap(2, 3), "bad", snap(3, 4)], run_id="r2")
    assert counts["read"] == 6
    assert counts["landed"] + counts["skipped_known"] + counts["quarantined"] == counts["read"]
    assert (counts["landed"], counts["skipped_known"], counts["quarantined"]) == (2, 3, 1)


def test_the_same_input_again_lands_nothing_and_adds_no_delta_version(lh):
    rows = [snap(i, i + 1) for i in range(5)] + ["bad record"]
    land(lh, rows, run_id="r1")
    v_bronze, v_quarantine, n = lh.version(T), lh.version(config.BRONZE_QUARANTINE), lh.count(T)
    again = land(lh, rows, run_id="r2")
    assert again["landed"] == 0 and again["skipped_known"] == 5 and again["quarantine_rows_written"] == 0
    assert lh.version(T) == v_bronze and lh.version(config.BRONZE_QUARANTINE) == v_quarantine and lh.count(T) == n


def test_a_changed_row_is_appended_and_the_old_one_stays(lh):
    land(lh, [snap(0, 5)], run_id="r1")
    land(lh, [snap(0, 6)], run_id="r2")
    rows = lh.read(T).orderBy("ingest_run_id").collect()
    assert [json.loads(r["raw_payload"])["value_numeric"] for r in rows] == [5, 6]
    assert [r["ingest_run_id"] for r in rows] == ["r1", "r2"]


def test_identical_records_in_one_batch_land_once(lh):
    counts = land(lh, [snap(0, 5), snap(0, 5), snap(0, 5)])
    assert counts["landed"] == 1 and counts["skipped_known"] == 2 and counts["read"] == 3
    assert lh.count(T) == 1


def test_bronze_is_never_updated_or_deleted(lh):
    land(lh, [snap(0, 1)], run_id="r1")
    land(lh, [snap(0, 2), snap(1, 3)], run_id="r2")
    ops = {r["operation"] for r in DeltaTable.forPath(lh.spark, lh.path(T)).history().collect()}
    assert ops <= {"WRITE", "CREATE TABLE", "CREATE TABLE AS SELECT"}, ops
    assert lh.read(T).where(F.col("ingest_run_id") == "r1").count() == 1
