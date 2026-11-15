"""The tuning benchmark on a small table: the four versions answer the query identically, and each fix reads fewer files. No timing is asserted: a clock on a shared machine
is not a test. The measured numbers are in docs/lakehouse.md."""
from __future__ import annotations

from lakehouse import tuning


def test_generation_is_deterministic_and_the_four_versions_agree(spark, tmp_path):
    a = tuning.generate(spark, 5000, seed=1).agg({"figure_value": "sum"}).collect()[0][0]
    b = tuning.generate(spark, 5000, seed=1).agg({"figure_value": "sum"}).collect()[0][0]
    c = tuning.generate(spark, 5000, seed=2).agg({"figure_value": "sum"}).collect()[0][0]
    assert a == b and a != c
    out = tuning.run_tuning(spark, tmp_path / "bench", rows=60000, runs=1, seed=3)
    v = out["versions"]
    assert out["same_result_in_every_version"] and set(v) == {"A baseline", "B partitioned", "C zorder", "D broadcast"}
    assert len({x["result_checksum"] for x in v.values()}) == 1 and v["A baseline"]["result_rows"] > 0
    assert v["A baseline"]["files"] == 200
    # each fix reads fewer files than the one before it, or the same number when the change is not about files
    a_read, b_read, c_read, d_read = (v[k]["files_read"] for k in ("A baseline", "B partitioned", "C zorder", "D broadcast"))
    assert a_read > b_read >= c_read >= 1 and d_read == c_read
    assert v["C zorder"]["files"] < v["B partitioned"]["files"] and v["C zorder"]["optimize_seconds"] >= 0
