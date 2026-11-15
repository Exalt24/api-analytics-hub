"""Small DataFrames from Python rows without starting a Python worker.

`spark.createDataFrame(list)` hands the rows to a Python worker process. PySpark 4.1 workers crash on Python 3.13 on Windows ("not a socket"), and a
pipeline that only needs a few rows (a run log entry, a page of REST records) should not depend on that. This builds the frame on the JVM side: the rows
go over py4j as JSON lines and are read back with an explicit schema, so a value that does not fit its column becomes null the same way it would in a file.
Nothing in this pipeline uses a Python UDF either, so the whole run is JVM work driven from Python.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Iterable

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.types import StructType


def _default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _ddl(schema: StructType) -> str:
    return ", ".join(f"`{f.name}` {f.dataType.simpleString()}" for f in schema.fields)


def frame(spark: SparkSession, rows: Iterable, schema: StructType | str) -> DataFrame:
    if isinstance(schema, str):
        schema = StructType.fromDDL(schema)
    names = schema.fieldNames()
    jvm = spark._jvm
    lines = jvm.java.util.ArrayList()
    for r in rows:
        d = r if isinstance(r, dict) else dict(zip(names, r))
        lines.add(json.dumps(d, default=_default, ensure_ascii=True))
    jds = spark._jsparkSession.createDataset(lines, jvm.org.apache.spark.sql.Encoders.STRING())
    reader = spark._jsparkSession.read().schema(_ddl(schema)).option("mode", "PERMISSIVE")
    return DataFrame(reader.json(jds), spark)
