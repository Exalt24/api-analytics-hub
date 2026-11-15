"""Gold: the dimensional model and the two reporting tables, each one a Spark SQL file in lakehouse/sql/.

A table is rebuilt from silver on every run and replaced only when its content differs, so a rerun on unchanged input adds no Delta version.
Order matters: the dimensions and the date table, then the fact (which reads silver), then the two reports (which read gold).
"""
from __future__ import annotations

from . import config
from .warehouse import Lakehouse

GOLD_ORDER = (
    "gold_dim_tenant",
    "gold_dim_source",
    "gold_dim_metric",
    "gold_dim_date",
    "gold_fact_daily_figure",
    "gold_rpt_daily_store_and_ad_spend",
    "gold_rpt_data_freshness",
)


def sql_text(name: str) -> str:
    return (config.SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")


def build_gold_table(lh: Lakehouse, name: str, *, limit_days: int = config.FRESHNESS_LIMIT_DAYS) -> dict:
    """Run one SQL file and make the Delta table equal to its result. Returns rows read (the result size) and rows written (0 when unchanged)."""
    lh.use()
    # :as_of and :limit_days are filled here as literals (a date and an integer, nothing else can reach the text); Spark's own named
    # parameter binding left them unbound inside this query's nested select
    text = sql_text(name).replace(":as_of", "'" + lh.as_of.isoformat() + "'").replace(":limit_days", str(int(limit_days)))
    df = lh.spark.sql(text)
    read = df.count()
    written = lh.sync_whole(name, df)
    return {"read": read, "written": written}
