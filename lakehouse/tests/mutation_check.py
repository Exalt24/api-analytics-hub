# LIFESPAN: permanent (the repo's own mutation check for the lakehouse, run by hand in a private copy)
"""Mutation control for the lakehouse: break each load-bearing rule one at a time and prove a test turns red.

It follows tests/mutation_check_salesforce.py in the hub (a table of guards, each naming the test that must be among the failures, `--check-targets`, `--match`), with one
difference that makes it safe to run anywhere: it never edits the working tree. Each mutant is written into a private copy of lakehouse/ (and the migrations the JDBC test
reads), the selected tests run there, and the copy is thrown away.

    python -m lakehouse.tests.mutation_check --check-targets       only confirm every guard finds its text exactly once
    python -m lakehouse.tests.mutation_check --list                the guards and the rule each one breaks
    python -m lakehouse.tests.mutation_check --match dedupe,merge  only the guards whose name holds a word
    python -m lakehouse.tests.mutation_check --jobs 2              two worker processes (each starts one Spark session and runs its share of the guards in it)
    python -m lakehouse.tests.mutation_check                       every guard

A mutant counts as caught only when the tests that fail include one of the fragments named for it, so a guard cannot be "covered" by an unrelated test that happens to break.
Exit 0 when every mutant is caught, 1 otherwise, 2 for a bad argument.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAKE = REPO / "lakehouse"

SILVER = "lakehouse/silver.py"
BRONZE = "lakehouse/bronze.py"
QUALITY = "lakehouse/quality.py"
WAREHOUSE = "lakehouse/warehouse.py"
RUNLOG = "lakehouse/runlog.py"
REST = "lakehouse/rest_client.py"
RUN = "lakehouse/run.py"
SESSION = "lakehouse/spark_session.py"
FACT_SQL = "lakehouse/sql/gold_fact_daily_figure.sql"
FRESH_SQL = "lakehouse/sql/gold_rpt_data_freshness.sql"

T_SILVER = "lakehouse/tests/test_silver.py"
T_BRONZE = "lakehouse/tests/test_bronze.py"
T_QUALITY = "lakehouse/tests/test_quality.py"
T_GOLD = "lakehouse/tests/test_gold.py"
T_REST = "lakehouse/tests/test_rest.py"
T_PROP = "lakehouse/tests/test_property.py"
T_PIPE = "lakehouse/tests/test_pipeline.py"
T_WH = "lakehouse/tests/test_warehouse.py"

MERGE_OLD = '''            (DeltaTable.forPath(lh.spark, lh.path(spec.table)).alias("t").merge(to_write.alias("s"), cond)
             .whenMatchedUpdateAll().whenNotMatchedInsertAll().execute())'''
MERGE_NEW = '''            to_write.write.format("delta").mode("append").save(lh.path(spec.table))'''

# (file, name, [(old, new)], pytest selection, env, fragments of which one must be among the failing tests, the rule it breaks)
MUTATIONS: list[tuple] = [
    (SILVER, "dedupe removed", [('F.when(F.col("_rn") > 1, F.lit("duplicate_in_batch"))', 'F.when(F.lit(False), F.lit("duplicate_in_batch"))')],
     [T_SILVER, T_PROP], {"SEEDS": "101"}, ["two_revisions_of_a_key", "random_messy"], "one row per business key: the newest revision wins and the rest are counted"),
    (SILVER, "quarantine removed", [("        lh.append(config.SILVER_QUARANTINE, q.select(*[f.name for f in QUARANTINE_SCHEMA]))", "        pass")],
     [T_SILVER], {}, ["renamed_column", "retyped", "missing_and_null", "unsafe_added"], "a row that cannot be read goes to quarantine with its reason, never dropped"),
    (SILVER, "merge made an append", [(MERGE_OLD, MERGE_NEW)],
     [T_SILVER, T_PROP], {"SEEDS": "101"}, ["newer_revision_updates", "random_messy"], "a newer revision updates the row in place (a MERGE upsert), it does not add another"),
    (SILVER, "stale guard removed", [('.when(F.col("source_version") < F.col("e_version"), F.lit("stale"))', '.when(F.lit(False), F.lit("stale"))')],
     [T_SILVER, T_PROP], {"SEEDS": "101"}, ["late_arriving_older_copy", "random_messy"], "an older copy arriving late must not overwrite a newer value"),
    (SILVER, "unchanged check removed", [('.when(same, F.lit("unchanged"))', '.when(F.lit(False), F.lit("unchanged"))')],
     [T_SILVER, T_PROP], {"SEEDS": "101"}, ["same_values_again", "random_messy"], "the same values again write nothing"),
    (SILVER, "retype turned into a null", [("reasons.append(F.when(present & F.col(c.name).isNull(),", "reasons.append(F.when(F.lit(False) & F.col(c.name).isNull(),")],
     [T_SILVER], {}, ["retyped_or_unreadable"], "a retyped value is quarantined with a reason, not stored as a null"),
    (SILVER, "renamed column accepted", [('reasons.append(F.when(~present, F.lit("missing_required:" + c.source)))', 'reasons.append(F.when(F.lit(False), F.lit("missing_required:" + c.source)))')],
     [T_SILVER], {}, ["renamed_column", "missing_and_null"], "a column renamed away is quarantined, the row is not stored with a null in its place"),
    (SILVER, "checkpoint not written", [("    lh.append(config.OPS_CHECKPOINT, done)", "    pass")],
     [T_SILVER], {}, ["taken_once"], "a bronze run is processed once; the next run starts after it"),
    (BRONZE, "known hashes not skipped", [('fresh = unique.join(known, "row_hash", "left_anti")', "fresh = unique")],
     [T_BRONZE, T_PROP], {"SEEDS": "101"}, ["same_input_again", "every_record_read", "random_messy"], "a rerun on the same input lands nothing"),
    (BRONZE, "bronze quarantine removed", [("        quarantined = lh.append(config.BRONZE_QUARANTINE, q_rows.select(*[f.name for f in QUARANTINE_SCHEMA]))", "        quarantined = 0")],
     [T_BRONZE], {}, ["unreadable_records"], "a record that cannot be read goes to quarantine with its raw text"),
    (BRONZE, "identical rows in a batch landed twice", [('unique = good.dropDuplicates(["row_hash"])', "unique = good")],
     [T_BRONZE], {}, ["identical_records_in_one_batch"], "the same bytes twice in one batch land once"),
    (QUALITY, "uniqueness check skipped", [('failed = dup.agg(F.sum(F.col("count") - 1)).collect()[0][0] or 0', "failed = 0")],
     [T_QUALITY], {}, ["every_planted_defect", "non_key_table"], "a repeated business key must be flagged"),
    (QUALITY, "freshness check dropped", [('stale = lag.where(F.col("lag_days") > limit_days)', "stale = lag.where(F.lit(False))")],
     [T_QUALITY], {}, ["every_planted_defect", "exactly_at_the_limit"], "a source behind the stated limit must be flagged"),
    (QUALITY, "reconciliation broken", [('mismatch = both.where(F.col("silver_total") != F.col("gold_total"))', "mismatch = both.where(F.lit(False))")],
     [T_QUALITY], {}, ["every_planted_defect"], "silver and gold totals must be compared"),
    (QUALITY, "orphan key check dropped", [("        if n:\n            missing[fk] = n", "        if False:\n            missing[fk] = n")],
     [T_QUALITY], {}, ["every_planted_defect"], "a fact row without a dimension row must be flagged"),
    (QUALITY, "range check dropped", [("n, failed = df.agg(F.count(F.lit(1)), F.sum(F.when(out, 1).otherwise(0))).collect()[0]\n    return flag(table, \"accepted_range\"",
                                       "n, failed = df.agg(F.count(F.lit(1)), F.sum(F.when(F.lit(False), 1).otherwise(0))).collect()[0]\n    return flag(table, \"accepted_range\"")],
     [T_QUALITY], {}, ["every_planted_defect"], "a figure outside its accepted range must be flagged"),
    (RUN, "check always exits zero", [("    return 2 if failed else 0", "    return 0")],
     [T_QUALITY], {}, ["check_exits_zero"], "--check exits non zero when a key table fails"),
    (WAREHOUSE, "gold rewritten on every run", [("if _shape(current) == _shape(df) and current.exceptAll", "if False and current.exceptAll")],
     [T_WH], {}, ["unchanged_content"], "a table whose content did not change is not rewritten, so a rerun adds no version"),
    (RUNLOG, "failure logged as success", [('self._write(s, started, ended, time.perf_counter() - t0, "FAILED", text)', 'self._write(s, started, ended, time.perf_counter() - t0, "SUCCEEDED", None)')],
     ["lakehouse/tests/test_runlog.py"], {}, ["failed_step_is_logged"], "the run log says which step failed and why"),
    (REST, "total not checked", [("    if total != len(out.records):", "    if False:")],
     [T_REST], {}, ["total_that_does_not_match"], "a page lost on the way must not pass as a smaller set"),
    (REST, "next address not checked", [('if not path.startswith("/services/data/") or ".." in path:', "if False:")],
     [T_REST], {}, ["off_the_data_api"], "the token must not be sent to an address a response names"),
    (SESSION, "schema auto merge off", [('"spark.databricks.delta.schema.autoMerge.enabled", "true"', '"spark.databricks.delta.schema.autoMerge.enabled", "false"')],
     [T_SILVER], {}, ["added_column_flows"], "an added column flows through to silver"),
    (FACT_SQL, "lead counts off by one", [("'sf_leads_created',\n        COUNT(*),", "'sf_leads_created',\n        COUNT(*) + 1,")],
     [T_GOLD], {}, ["lead_counts_made_from_records"], "the fact's lead counts equal the records in silver"),
    (FRESH_SQL, "freshness limit made exclusive", [("<= CAST(:limit_days AS INT) THEN 'FRESH'", "< CAST(:limit_days AS INT) THEN 'FRESH'")],
     [T_GOLD], {}, ["limit_is_inclusive"], "a source exactly at the limit is still fresh"),
]


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def missing_targets() -> list[str]:
    problems = []
    for rel, name, edits, selection, _env, fragments, _why in MUTATIONS:
        text = read(rel)
        for old, new in edits:
            n = text.count(old)
            if n != 1:
                problems.append(f"{name}: its target text appears {n} times in {rel}, it must appear once")
            elif old == new:
                problems.append(f"{name}: the mutation changes nothing")
        for t in selection:
            if not (REPO / t).is_file():
                problems.append(f"{name}: test file {t} does not exist")
        if not fragments:
            problems.append(f"{name}: names no test that must fail")
    names = [m[1] for m in MUTATIONS]
    problems += [f"duplicate guard name {n}" for n in set(names) if names.count(n) > 1]
    return problems


def make_copy(dest: Path) -> None:
    def ignore(folder: str, names: list[str]) -> set[str]:
        skip = {".venv", ".hadoop", "__pycache__", "data", ".pytest_cache", "results", "spark-warehouse"}
        return {n for n in names if n in skip or n.endswith(".pyc") or (n.startswith("warehouse") and (Path(folder) / n).is_dir())}

    shutil.copytree(LAKE, dest / "lakehouse", ignore=ignore, symlinks=False)
    shutil.copytree(REPO / "migrations", dest / "migrations")


# A guard that only changes one of these files cannot change how the shared `mini` warehouse is built, so such guards reuse one built from the real code.
KEEPS_MINI = {QUALITY, RUN}
# What a guard costs, to spread them over the workers: the guards that rebuild the `mini` warehouse are the slow ones.
WEIGHT = {FACT_SQL: 4, FRESH_SQL: 4}


class Collector:
    """A pytest plugin: the node ids of the tests whose call failed. An error in a fixture is not a caught mutant, it is a broken harness."""

    def __init__(self) -> None:
        self.failed: list[str] = []
        self.errors: list[str] = []

    def pytest_runtest_logreport(self, report) -> None:
        if report.failed and report.when == "call":
            self.failed.append(report.nodeid)
        elif report.failed:
            self.errors.append(report.nodeid)


def run_in_process(mutation: tuple, mini_dir: str | None) -> tuple[str, str, list[str], float]:
    """Write the mutant into a private copy, point this process at it, run only the tests named for the guard, and put the process back.
    One JVM serves every mutant of a worker: the Spark session is kept between them (LAKEHOUSE_KEEP_SPARK)."""
    import pytest

    rel, name, edits, selection, env_extra, fragments, _why = mutation
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="lh-mut-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        make_copy(root)
        target = root / rel
        text = target.read_text(encoding="utf-8").replace("\r\n", "\n")
        mutated = text
        for old, new in edits:
            mutated = mutated.replace(old, new, 1)
        assert mutated != text, f"{name}: the mutation changes nothing"
        target.write_text(mutated, encoding="utf-8", newline="\n")
        saved_env = {k: os.environ.get(k) for k in (*env_extra, "LAKEHOUSE_MINI_DIR")}
        os.environ.update(env_extra)
        if mini_dir and rel in KEEPS_MINI:
            os.environ["LAKEHOUSE_MINI_DIR"] = mini_dir
        else:
            os.environ.pop("LAKEHOUSE_MINI_DIR", None)
        saved_modules = {m: sys.modules.pop(m) for m in [m for m in sys.modules if m == "lakehouse" or m.startswith("lakehouse.")]}
        sys.path.insert(0, str(root))
        collector = Collector()
        here = os.getcwd()
        os.chdir(root)  # as a pytest started inside the copy would be; the copy cannot be deleted while this process stands in it
        try:
            code = pytest.main([*selection, "-k", " or ".join(fragments), "-q", "-p", "no:cacheprovider", "--tb=no"], plugins=[collector])
        finally:
            os.chdir(here)
            sys.path.remove(str(root))
            for m in [m for m in sys.modules if m == "lakehouse" or m.startswith("lakehouse.")]:
                del sys.modules[m]
            sys.modules.update(saved_modules)
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    failed = collector.failed
    secs = time.perf_counter() - t0
    if int(code) == 0:
        return name, "survived", failed, secs
    if not failed:
        return name, "error: pytest exit " + str(int(code)) + " " + "; ".join(collector.errors[:2]), failed, secs
    if any(frag in test for test in failed for frag in fragments):
        return name, "caught", failed, secs
    return name, "wrong test", failed, secs


def worker(names: list[str]) -> int:
    """Run the named guards one after the other in this process and print one RESULT line each."""
    os.environ["LAKEHOUSE_KEEP_SPARK"] = "1"
    todo = [m for m in MUTATIONS if m[1] in names]
    from lakehouse.spark_session import build_spark

    spark = build_spark("lakehouse-mutation", jdbc=True, shuffle_partitions=2)
    mini_dir = None
    keep = tempfile.mkdtemp(prefix="lh-mini-")
    try:
        if any(m[0] in KEEPS_MINI for m in todo):
            from datetime import date

            from lakehouse.tests.helpers import build_mini

            build_mini(spark, Path(keep), date(2026, 11, 1))
            mini_dir = keep
        for m in todo:
            name, verdict, failed, secs = run_in_process(m, mini_dir)
            print("RESULT\t" + "\t".join([name, verdict, f"{secs:.0f}", str(len(failed)), ",".join(f.rsplit("::", 1)[-1] for f in failed[:3])]), flush=True)
    finally:
        shutil.rmtree(keep, ignore_errors=True)
        spark.stop()
    return 0


def spread(todo: list[tuple], jobs: int) -> list[list[str]]:
    """The guards over `jobs` workers, heaviest first onto the lightest worker."""
    bins: list[tuple[int, list[str]]] = [(0, []) for _ in range(max(1, jobs))]
    for m in sorted(todo, key=lambda m: -WEIGHT.get(m[0], 1)):
        bins.sort(key=lambda b: b[0])
        load, names = bins[0]
        bins[0] = (load + WEIGHT.get(m[0], 1), names + [m[1]])
    return [names for _, names in bins if names]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    ap.add_argument("--check-targets", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--match")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2), help="worker processes, each with its own Spark (default: half the cores)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    problems = missing_targets()
    if problems:
        print("update this file before mutating:\n  " + "\n  ".join(problems))
        return 1
    if a.worker:
        return worker(a.worker.split("|"))
    if a.list:
        for _rel, name, _edits, _sel, _env, _frag, why in MUTATIONS:
            print(f"{name}: {why}")
        return 0
    if a.check_targets:
        print(f"all {len(MUTATIONS)} guards find their target")
        return 0
    words = a.match.split(",") if a.match else None
    todo = [m for m in MUTATIONS if not words or any(w in m[1] for w in words)]
    groups = spread(todo, a.jobs)
    print(f"{len(todo)} mutants over {len(groups)} worker process(es); each runs only the tests named for it, in one Spark session per worker", flush=True)
    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    results: list[tuple[str, str]] = []

    def run_group(names: list[str]) -> None:
        proc = subprocess.Popen([a.python, "-m", "lakehouse.tests.mutation_check", "--worker", "|".join(names)], cwd=REPO, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
        assert proc.stdout is not None
        seen = set()
        for line in proc.stdout:
            if line.startswith("RESULT\t"):
                name, verdict, secs, n, failed = (line.rstrip("\n").split("\t") + ["", "", "", "", ""])[1:6]
                seen.add(name)
                results.append((name, verdict))
                mark = "[ok]  " if verdict == "caught" else "[FAIL]"
                print(f"{mark} {name}: {verdict} ({secs}s, {n} failing test(s))" + ("" if verdict == "caught" else f" {failed}"), flush=True)
        proc.wait()
        for name in names:
            if name not in seen:
                results.append((name, "worker died"))
                print(f"[FAIL] {name}: worker died (exit {proc.returncode})", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(groups)) as pool:
        list(pool.map(run_group, groups))
    caught = sum(v == "caught" for _, v in results)
    print(f"\nmutants run {len(results)}: caught {caught}, not caught {len(results) - caught}")
    print("mutation control: " + ("PASS" if caught == len(results) else f"{len(results) - caught} UNTESTED GUARD(S)"))
    return 0 if caught == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
