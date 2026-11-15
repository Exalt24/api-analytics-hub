"""Mutation control for the lakehouse: break each load-bearing rule one at a time and prove a test turns red.

It follows tests/mutation_check_salesforce.py in the hub (a table of guards, each naming the test that must be among the failures, `--check-targets`, `--match`), with one
difference that makes it safe to run anywhere: it never edits the working tree. Each mutant is written into a private copy of lakehouse/ (and the migrations the JDBC test
reads), the selected tests run there, and the copy is thrown away.

    python -m lakehouse.tests.mutation_check --check-targets       only confirm every guard finds its text exactly once
    python -m lakehouse.tests.mutation_check --list                the guards and the rule each one breaks
    python -m lakehouse.tests.mutation_check --match dedupe,merge  only the guards whose name holds a word
    python -m lakehouse.tests.mutation_check --jobs 2              two copies at a time (each starts its own Spark)
    python -m lakehouse.tests.mutation_check                       every guard

A mutant counts as caught only when the tests that fail include one of the fragments named for it, so a guard cannot be "covered" by an unrelated test that happens to break.
Exit 0 when every mutant is caught, 1 otherwise, 2 for a bad argument.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAKE = REPO / "lakehouse"
PER_MUTANT_TIMEOUT_SECONDS = 600

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


def run_one(mutation: tuple, python: str) -> tuple[str, str, list[str], float]:
    rel, name, edits, selection, env_extra, fragments, why = mutation
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="lh-mut-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        make_copy(root)
        target = root / rel
        text = target.read_text(encoding="utf-8").replace("\r\n", "\n")
        mutated = text
        for old, new in edits:
            mutated = mutated.replace(old, new, 1)
        assert mutated != text, f"{name}: the mutation changed nothing"
        target.write_text(mutated, encoding="utf-8", newline="\n")
        env = {**os.environ, "PYTHONPATH": str(root), **env_extra, "PYTHONDONTWRITEBYTECODE": "1"}
        if (LAKE / ".hadoop" / "bin").is_dir() and "HADOOP_HOME" not in env:
            env["HADOOP_HOME"] = str(LAKE / ".hadoop")  # Windows: winutils lives once, beside the real tree
        try:
            # only the tests named for the guard run (-k takes substrings): the mutant must turn one of exactly those red, and a Spark start is the cost of each
            r = subprocess.run([python, "-m", "pytest", *selection, "-k", " or ".join(fragments), "-q", "-p", "no:cacheprovider", "-rf", "--tb=no"], cwd=root, env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=PER_MUTANT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return name, "hung", [], time.perf_counter() - t0
    out = r.stdout + r.stderr
    failed = re.findall(r"^FAILED (\S+)", out, re.M)
    if r.returncode == 0:
        return name, "survived", failed, time.perf_counter() - t0
    if not failed:
        return name, "error: " + out[-300:].replace("\n", " "), failed, time.perf_counter() - t0
    if any(frag in test for test in failed for frag in fragments):
        return name, "caught", failed, time.perf_counter() - t0
    return name, "wrong test", failed, time.perf_counter() - t0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    ap.add_argument("--check-targets", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--match")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args(argv)
    problems = missing_targets()
    if problems:
        print("update this file before mutating:\n  " + "\n  ".join(problems))
        return 1
    if a.list:
        for _rel, name, _edits, _sel, _env, _frag, why in MUTATIONS:
            print(f"{name}: {why}")
        return 0
    if a.check_targets:
        print(f"all {len(MUTATIONS)} guards find their target")
        return 0
    words = a.match.split(",") if a.match else None
    todo = [m for m in MUTATIONS if not words or any(w in m[1] for w in words)]
    print(f"{len(todo)} mutants, {a.jobs} at a time; each runs only the tests that should see it", flush=True)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        futures = {pool.submit(run_one, m, a.python): m for m in todo}
        for fut in concurrent.futures.as_completed(futures):
            name, verdict, failed, secs = fut.result()
            results.append((name, verdict))
            mark = "[ok]  " if verdict == "caught" else "[FAIL]"
            print(f"{mark} {name}: {verdict} ({secs:.0f}s, {len(failed)} failing test(s))" + ("" if verdict == "caught" else f" {failed[:3]}"), flush=True)
    caught = sum(v == "caught" for _, v in results)
    print(f"\nmutants run {len(results)}: caught {caught}, not caught {len(results) - caught}")
    print("mutation control: " + ("PASS" if caught == len(results) else f"{len(results) - caught} UNTESTED GUARD(S)"))
    return 0 if caught == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
