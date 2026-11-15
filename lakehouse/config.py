"""Names, limits and pinned versions in one place, so a rule is changed once."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
SQL_DIR = ROOT / "sql"
DEFAULT_WAREHOUSE = Path(os.environ.get("LAKEHOUSE_WAREHOUSE", ROOT / "warehouse"))
DEFAULT_EXPORT_DIR = ROOT / "data" / "export"

# Versions that belong together (Delta Lake release table, docs.delta.io/latest/releases.html: Delta 4.3.x runs on Spark 4.0.x and 4.1.x;
# the delta-spark 4.3.1 wheel itself asks for pyspark <= 4.1.1). The JDBC driver is pulled through spark.jars.packages.
PYSPARK_VERSION = "4.1.1"
DELTA_SPARK_VERSION = "4.3.1"
PG_JDBC_PACKAGE = "org.postgresql:postgresql:42.7.7"
DEFAULT_JDBC_URL = os.environ.get("LAKEHOUSE_PG_URL", "jdbc:postgresql://127.0.0.1:5433/lakehouse_src")
DEFAULT_JDBC_USER = os.environ.get("LAKEHOUSE_PG_USER", "analytics")
DEFAULT_JDBC_PASSWORD = os.environ.get("LAKEHOUSE_PG_PASSWORD", "analytics")

# How late a source may be, in days behind the as-of date, before its figures are called stale.
FRESHNESS_LIMIT_DAYS = 2
# Re-read this many hours of the Postgres synced_at column on every incremental run; the row hash drops what was already landed.
INCREMENTAL_LOOKBACK_HOURS = 48

# Accepted range per metric key, in the metric's own unit (money in minor units). Anything not listed uses DEFAULT_RANGE.
DEFAULT_RANGE = (0, 10**12)
RANGES: dict[str, tuple[int, int]] = {
    "net_sales": (-10**10, 10**12),
    "refunds": (0, 10**10),
    "orders": (0, 10**6),
    "ad_spend": (0, 10**10),
    "ad_clicks": (0, 10**9),
    "ad_impressions": (0, 10**10),
}

# Table naming standard: <layer>_<what>, snake_case. bronze_<source>_<entity>, silver_<entity>, gold_dim_/gold_fact_/gold_rpt_<what>,
# and ops_<what> for the run log, the quality flags and the checkpoint (they belong to no layer).
OPS_RUN_LOG = "ops_run_log"
OPS_QUALITY_FLAGS = "ops_quality_flags"
OPS_CHECKPOINT = "ops_checkpoint"
BRONZE_QUARANTINE = "bronze_quarantine"
SILVER_QUARANTINE = "silver_quarantine"

# The quality flags that make `python -m lakehouse.run --check` exit non zero.
KEY_TABLES = (
    "silver_daily_figure", "silver_lead", "silver_tenant", "silver_platform_connection", "silver_metric",
    "gold_fact_daily_figure", "gold_rpt_data_freshness", "gold_rpt_daily_store_and_ad_spend",
)
