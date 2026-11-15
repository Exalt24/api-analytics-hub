"""The warehouse helper: tables are plain Delta folders, a whole-table sync writes only when something differs, append never writes an empty frame."""
from __future__ import annotations

from lakehouse.frames import frame


def rows(spark, *values):
    return frame(spark, [(v, v * 10) for v in values], "a long, b long")


def test_append_creates_the_table_and_an_empty_append_adds_no_version(lh):
    assert lh.version("t_demo") == -1 and not lh.exists("t_demo")
    assert lh.append("t_demo", rows(lh.spark, 1, 2)) == 2
    assert lh.version("t_demo") == 0 and (lh.root / "t_demo" / "_delta_log").is_dir()
    assert lh.append("t_demo", rows(lh.spark, 1).where("a > 5")) == 0
    assert lh.version("t_demo") == 0 and lh.count("t_demo") == 2


def test_a_whole_table_sync_with_unchanged_content_writes_nothing(lh):
    assert lh.sync_whole("t_sync", rows(lh.spark, 1, 2, 3)) == 3
    v = lh.version("t_sync")
    assert lh.sync_whole("t_sync", rows(lh.spark, 3, 2, 1)) == 0      # the same rows in another order
    assert lh.version("t_sync") == v


def test_a_whole_table_sync_with_changed_content_or_schema_writes_a_new_version(lh):
    lh.sync_whole("t_sync", rows(lh.spark, 1, 2, 3))
    assert lh.sync_whole("t_sync", rows(lh.spark, 1, 2, 4)) == 3 and lh.version("t_sync") == 1
    assert sorted(r["a"] for r in lh.read("t_sync").collect()) == [1, 2, 4]
    assert lh.sync_whole("t_sync", rows(lh.spark, 1, 2, 4).withColumnRenamed("b", "c")) == 3 and "c" in lh.read("t_sync").columns


def test_the_same_rows_with_a_duplicate_count_as_a_change(lh):
    lh.sync_whole("t_sync", rows(lh.spark, 1, 2))
    assert lh.sync_whole("t_sync", rows(lh.spark, 1, 2, 2)) == 3


def test_a_registered_table_is_readable_by_name_in_sql(lh):
    lh.append("t_named", rows(lh.spark, 7))
    assert lh.spark.sql("SELECT a FROM t_named").collect()[0]["a"] == 7
    lh.register_all()
    assert lh.table_names() == ["t_named"]
