"""The run log: one Delta row per step of every run, with rows read, written and quarantined, the clock and the error.

It is also the compute cost record: seconds and rows per job, ranked by `lakehouse.costs`.
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pyspark.sql.types import DoubleType, LongType, StringType, StructField, StructType, TimestampType

from . import config
from .warehouse import Lakehouse
from .frames import frame

SCHEMA = StructType([
    StructField("run_id", StringType(), False),
    StructField("step", StringType(), False),
    StructField("rows_read", LongType()),
    StructField("rows_written", LongType()),
    StructField("rows_quarantined", LongType()),
    StructField("started_at", TimestampType(), False),
    StructField("ended_at", TimestampType(), False),
    StructField("seconds", DoubleType(), False),
    StructField("status", StringType(), False),
    StructField("error", StringType()),
    StructField("detail", StringType()),
])


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


@dataclass
class Step:
    """What a step reports back while it runs."""
    name: str
    rows_read: int = 0
    rows_written: int = 0
    rows_quarantined: int = 0
    detail: dict = field(default_factory=dict)


class StepFailed(Exception):
    def __init__(self, step: str, error: str):
        super().__init__(f"{step}: {error}")
        self.step = step
        self.error = error


class RunLog:
    def __init__(self, lh: Lakehouse, run_id: str | None = None):
        self.lh = lh
        self.run_id = run_id or new_run_id()
        self.lh.ensure(config.OPS_RUN_LOG, SCHEMA)

    def _write(self, step: Step, started: datetime, ended: datetime, seconds: float, status: str, error: str | None) -> None:
        row = (self.run_id, step.name, step.rows_read, step.rows_written, step.rows_quarantined, started, ended, round(seconds, 3), status, error,
               json.dumps(step.detail, sort_keys=True, default=str) if step.detail else None)
        self.lh.append(config.OPS_RUN_LOG, frame(self.lh.spark, [row], SCHEMA))

    @contextmanager
    def step(self, name: str):
        """Run a block as a logged step. A failure is written (status FAILED and the error text) and then raised as StepFailed."""
        s = Step(name)
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        try:
            yield s
        except Exception as exc:  # noqa: BLE001 - every failure must reach the log
            ended = datetime.now(timezone.utc)
            text = (type(exc).__name__ + ": " + str(exc)).strip()[:2000]
            self._write(s, started, ended, time.perf_counter() - t0, "FAILED", text)
            raise StepFailed(name, text) from exc
        ended = datetime.now(timezone.utc)
        self._write(s, started, ended, time.perf_counter() - t0, "SUCCEEDED", None)

    def skip(self, name: str, reason: str) -> None:
        now = datetime.now(timezone.utc)
        self._write(Step(name), now, now, 0.0, "SKIPPED", reason)

    def steps(self):
        return self.lh.read(config.OPS_RUN_LOG).where(f"run_id = '{self.run_id}'")
