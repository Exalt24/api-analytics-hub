"""A warehouse is a folder of Delta tables, one folder per table, plus the Spark session that reads and writes them.

Tables are written by path and registered as external tables in a catalog database named after the folder, so Spark SQL files can say
`FROM silver_daily_figure` and the data still sits in plain Delta folders that any other Delta reader can open.
"""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pyspark.sql import DataFrame, SparkSession

from .frames import frame


def _shape(df: DataFrame) -> list[tuple[str, str]]:
    """Column names and types; nullability is left out because a Delta table read back reports every column nullable."""
    return [(f.name, f.dataType.simpleString()) for f in df.schema.fields]


@dataclass
class Lakehouse:
    spark: SparkSession
    root: Path
    as_of: date = field(default_factory=date.today)

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = "lh_" + hashlib.sha1(str(self.root).encode()).hexdigest()[:12]
        self.spark.sql(f"CREATE DATABASE IF NOT EXISTS {self.db}")
        self.use()

    # -- names and paths
    def use(self) -> None:
        self.spark.catalog.setCurrentDatabase(self.db)

    def path(self, name: str) -> str:
        return str(self.root / name)

    def uri(self, name: str) -> str:
        return (self.root / name).as_uri()

    def exists(self, name: str) -> bool:
        return (self.root / name / "_delta_log").is_dir()

    def table_names(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if (p / "_delta_log").is_dir())

    # -- catalog
    def register(self, name: str) -> None:
        self.use()
        self.spark.sql(f"DROP TABLE IF EXISTS {name}")
        self.spark.sql(f"CREATE TABLE {name} USING DELTA LOCATION '{self.uri(name)}'")

    def register_all(self) -> None:
        for name in self.table_names():
            self.register(name)

    # -- read
    def read(self, name: str) -> DataFrame:
        return self.spark.read.format("delta").load(self.path(name))

    def count(self, name: str) -> int:
        return self.read(name).count() if self.exists(name) else 0

    def version(self, name: str) -> int:
        """The Delta table version, -1 for a table that does not exist yet."""
        if not self.exists(name):
            return -1
        from delta.tables import DeltaTable
        return int(DeltaTable.forPath(self.spark, self.path(name)).history(1).select("version").collect()[0][0])

    def versions(self, names: list[str] | None = None) -> dict[str, int]:
        return {n: self.version(n) for n in (names or self.table_names())}

    # -- write
    def append(self, name: str, df: DataFrame) -> int:
        """Append only. Creates the table on the first write. Returns the rows written; an empty frame writes nothing and adds no version."""
        n = df.count()
        if n == 0:
            return 0
        existed = self.exists(name)
        df.write.format("delta").mode("append").save(self.path(name))
        if not existed:
            self.register(name)
        return n

    def ensure(self, name: str, schema) -> None:
        """Create an empty table with the given schema when it is missing, so a first read never fails on a table with no rows yet."""
        if not self.exists(name):
            frame(self.spark, [], schema).write.format("delta").save(self.path(name))
            self.register(name)

    def sync_whole(self, name: str, df: DataFrame) -> int:
        """Make a table equal to df. Writes only when the content or the schema differs, so a rerun on unchanged input adds no Delta version.
        Returns the rows written (0 when nothing changed)."""
        if not self.exists(name):
            n = df.count()
            df.write.format("delta").save(self.path(name))
            self.register(name)
            return n
        current = self.read(name)
        if _shape(current) == _shape(df) and current.exceptAll(df).unionAll(df.exceptAll(current)).limit(1).count() == 0:
            return 0
        n = df.count()
        df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(self.path(name))
        self.register(name)
        return n

    def drop_all(self) -> None:
        self.spark.sql(f"DROP DATABASE IF EXISTS {self.db} CASCADE")
        shutil.rmtree(self.root, ignore_errors=True)
