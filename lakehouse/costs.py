"""Compute cost tracking: seconds and rows per job, from the run log, ranked by seconds.

    python -m lakehouse.costs [--warehouse DIR] [--run RUN_ID] [--top 10]

A job is a step of the run (an ingest, a silver merge, a gold table, the quality checks). The ranking sums every successful run in the log unless a
run id is given. Seconds are wall clock on the one machine the run happened on; they are the cost this pipeline can see, not a cloud bill.
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from pyspark.sql import functions as F

from . import config
from .spark_session import build_spark
from .warehouse import Lakehouse


def rank_jobs(lh: Lakehouse, run_id: str | None = None, top: int | None = None):
    df = lh.read(config.OPS_RUN_LOG).where(F.col("status") == "SUCCEEDED")
    if run_id:
        df = df.where(F.col("run_id") == run_id)
    out = (df.groupBy("step")
           .agg(F.count(F.lit(1)).alias("runs"), F.round(F.sum("seconds"), 2).alias("total_seconds"), F.round(F.avg("seconds"), 2).alias("avg_seconds"),
                F.sum("rows_read").alias("rows_read"), F.sum("rows_written").alias("rows_written"))
           .withColumn("rows_read_per_second", F.when(F.col("total_seconds") > 0, F.round(F.col("rows_read") / F.col("total_seconds"), 1)))
           .orderBy(F.col("total_seconds").desc(), "step"))
    return out.limit(top) if top else out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--warehouse", type=Path, default=config.DEFAULT_WAREHOUSE)
    ap.add_argument("--run")
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args()
    lh = Lakehouse(build_spark("lakehouse-costs"), a.warehouse, date.today())
    if not lh.exists(config.OPS_RUN_LOG):
        print("no run log in", a.warehouse)
        return 1
    rank_jobs(lh, a.run, a.top).show(a.top, truncate=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
