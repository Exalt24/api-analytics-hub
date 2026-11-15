"""One place that builds the local Spark session with Delta Lake (and, when asked, the PostgreSQL JDBC driver)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import config


def _windows_hadoop_home() -> None:
    """Hadoop's file system layer wants winutils.exe and hadoop.dll on Windows. `python -m lakehouse.setup_windows` puts them in lakehouse/.hadoop."""
    if os.name != "nt":
        return
    home = Path(os.environ["HADOOP_HOME"]) if os.environ.get("HADOOP_HOME") else config.ROOT / ".hadoop"
    if (home / "bin" / "winutils.exe").exists():
        os.environ["HADOOP_HOME"] = str(home)
        if str(home / "bin") not in os.environ.get("PATH", ""):
            os.environ["PATH"] = str(home / "bin") + os.pathsep + os.environ.get("PATH", "")  # hadoop.dll is found through PATH


def build_spark(app: str = "lakehouse", *, jdbc: bool = False, shuffle_partitions: int = 4, driver_memory: str | None = None,
                tmp_dir: Path | None = None):
    # imports are local so that a module that only needs the config does not start a JVM
    from delta import configure_spark_with_delta_pip
    from pyspark.sql import SparkSession

    _windows_hadoop_home()
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)
    builder = (
        SparkSession.builder.master("local[4]").appName(app)
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.shuffle.partitions", str(shuffle_partitions))
        .config("spark.ui.enabled", "false")
        .config("spark.databricks.delta.schema.autoMerge.enabled", "true")
        .config("spark.databricks.delta.snapshotPartitions", "2")
        .config("spark.sql.sources.parallelPartitionDiscovery.parallelism", "2")
    )
    if driver_memory:
        builder = builder.config("spark.driver.memory", driver_memory)
    if tmp_dir is not None:
        builder = builder.config("spark.local.dir", str(tmp_dir))
    packages = [config.PG_JDBC_PACKAGE] if jdbc else []
    spark = configure_spark_with_delta_pip(builder, extra_packages=packages).getOrCreate()
    spark.sparkContext.setLogLevel("ERROR")
    return spark
