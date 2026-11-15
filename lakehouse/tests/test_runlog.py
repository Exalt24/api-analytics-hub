"""The run log: a step is one row with its counts, clock and status; a failure is written with the error text and raised; a skipped step says why."""
from __future__ import annotations

import pytest

from lakehouse import config
from lakehouse.runlog import RunLog, StepFailed


def test_a_finished_step_is_one_row_with_counts_and_a_clock(lh):
    log = RunLog(lh, "run-x")
    with log.step("ingest_demo") as s:
        s.rows_read, s.rows_written, s.rows_quarantined = 10, 8, 2
        s.detail = {"skipped_known": 0}
    row = lh.read(config.OPS_RUN_LOG).collect()[0]
    assert (row["run_id"], row["step"], row["rows_read"], row["rows_written"], row["rows_quarantined"], row["status"]) == ("run-x", "ingest_demo", 10, 8, 2, "SUCCEEDED")
    assert row["error"] is None and row["seconds"] >= 0 and row["started_at"] <= row["ended_at"] and '"skipped_known": 0' in row["detail"]


def test_a_failed_step_is_logged_with_its_error_text_and_raised(lh):
    log = RunLog(lh, "run-y")
    with pytest.raises(StepFailed) as err:
        with log.step("silver_demo"):
            raise ValueError("a value that cannot be read")
    assert err.value.step == "silver_demo" and "a value that cannot be read" in err.value.error
    row = lh.read(config.OPS_RUN_LOG).collect()[0]
    assert row["status"] == "FAILED" and row["error"] == "ValueError: a value that cannot be read"


def test_a_skipped_step_says_why_and_counts_nothing(lh):
    log = RunLog(lh, "run-z")
    log.skip("gold_demo", "skipped: step silver_demo failed")
    row = lh.read(config.OPS_RUN_LOG).collect()[0]
    assert row["status"] == "SKIPPED" and row["error"] == "skipped: step silver_demo failed" and row["seconds"] == 0.0
