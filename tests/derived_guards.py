# LIFESPAN: permanent
"""Mutation guards derived from the code's own syntax tree, run in COPIES of the repository.

The hand matched guards in `mutation_check.py` pair a piece of old text with its replacement, and go stale the moment
a line is reworded (two of them did while the Meta and Search Console work was written). These are derived instead: the
sites come from `mutate_ast.py` (`sites` and `apply`: a flipped comparison, a negated condition, an emptied string, a
deleted call, a changed constant, a swapped operator, `return x` made `return None`), every site of the target file is
tried, one at a time, and the suite must go red for each. A mutant that stays green is a line the tests do not pin: it is
either an equivalent change (say so) or a missing test (write it).

    python tests/derived_guards.py --tool <path to mutate_ast.py> --target app/connectors/meta.py \
        --tests "tests/test_meta_connector.py tests/test_meta_fake_server.py" [--jobs 6] [--db] [--json out.json]

It never touches the working tree: the repository (without .git, the virtual environment, the dashboard and the media) is
copied once for each worker, and the mutant is written into a copy. With `--db` each worker also gets its own scratch
database on the server named by ANALYTICS_TEST_DSN, because the database tests drop and recreate the `public` schema of the
database they are pointed at; the database that DSN names is never used by a test, and the scratch ones are dropped at the end.
The result is a line for each function (sites, killed, survived) and a line for each survivor, and exit 1 if any survives.
`mutate_ast.py` is a tool of the author's, not part of this repository: the saved output in docs/test-runs/ is what you can read.
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures as cf
import importlib.util
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import defaultdict
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns(".git", ".venv", "node_modules", "web", "__pycache__", ".pytest_cache", ".shopify", "*.mp4", "*.gif", "*.png", "*.br")
DEFAULT_DSN = "postgresql://analytics:analytics@localhost:5433/analytics"


def load_tool(path: str):
    spec = importlib.util.spec_from_file_location("mutate_ast_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def python_in(root: Path) -> str:
    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidate = root.joinpath(".venv", *rel)
    return str(candidate if candidate.exists() else sys.executable)


def enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """line -> name of the innermost function that holds it ('(module)' for a line outside any)."""
    owner: dict[int, str] = {}
    spans = sorted(((n.lineno, n.end_lineno, n.name) for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
                   key=lambda s: (s[1] - s[0]), reverse=True)
    for start, end, name in spans:        # widest first, so the innermost function writes last and wins
        for line in range(start, end + 1):
            owner[line] = name
    return owner


def run_tests(copy: Path, tests: str, dsn: str | None, timeout: int, first_only: bool = True) -> tuple[bool, str]:
    """(green, the name of the first failing test or a reason). A timeout counts as red."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if dsn:
        env["ANALYTICS_TEST_DSN"] = dsn
    # a base temp folder of its own: pytest's shared one is cleaned up by every process at once, which on Windows stalls the ones that use tmp_path
    cmd = [python_in(ROOT), "-m", "pytest", *tests.split(), "-q", "-p", "no:cacheprovider", "--tb=no", "--basetemp=" + str(copy / ".pytest_tmp")] + (
        ["-x"] if first_only else [])
    proc = subprocess.Popen(cmd, cwd=copy, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # a mutant that loops forever: kill the whole tree (on Windows the venv launcher's child outlives it and spins on)
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=60)
        else:
            proc.kill()
        try:
            proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            pass
        return False, "timed out after %d s" % timeout
    failed = [line.split(" ")[1].split("::")[-1][:90] for line in out.splitlines() if line.startswith(("FAILED", "ERROR"))]
    return proc.returncode == 0, failed[0] if failed else ("passed" if proc.returncode == 0 else "exit %d" % proc.returncode)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tool", required=True, help="path to mutate_ast.py")
    p.add_argument("--target", required=True, help="the file to mutate, relative to the repository")
    p.add_argument("--tests", required=True, help="the test files that must go red, space separated")
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--timeout", type=int, default=240)
    p.add_argument("--db", action="store_true", help="give each worker its own scratch database")
    p.add_argument("--only-functions", default="", help="comma separated function names to mutate")
    p.add_argument("--json", help="write every verdict to this file")
    args = p.parse_args(argv)

    tool = load_tool(args.tool)
    target = ROOT / args.target
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    sites = tool.sites(tree, source, False, [f for f in args.only_functions.split(",") if f] or None)
    owner = enclosing_functions(tree)
    admin_dsn = os.environ.get("ANALYTICS_TEST_DSN", DEFAULT_DSN)
    tmp = Path(tempfile.mkdtemp(prefix="derived_guards_"))
    slots: "queue.Queue[tuple[Path, str | None]]" = queue.Queue()
    scratch: list[str] = []
    try:
        for i in range(args.jobs):
            copy = tmp / ("w%d" % i)
            shutil.copytree(ROOT, copy, ignore=IGNORED)
            dsn = None
            if args.db:
                name = "mut_%s_%d" % (uuid.uuid4().hex[:6], i)
                with psycopg.connect(admin_dsn, autocommit=True) as admin:
                    admin.execute('create database "%s"' % name)
                scratch.append(name)
                dsn = admin_dsn.rpartition("/")[0] + "/" + name
            slots.put((copy, dsn))
        copies = list(slots.queue)
        green = [run_tests(copy, args.tests, dsn, args.timeout, first_only=False)[0] for copy, dsn in copies[:1]]
        if not all(green):
            print("the tests are RED on the unmutated copy, so every mutant would count as killed: fix that first")
            return 3
        # the same source after ast.unparse with no mutation, which is what every mutant is written as
        copy, dsn = copies[0]
        (copy / args.target).write_text(ast.unparse(tree), encoding="utf-8")
        round_trip = run_tests(copy, args.tests, dsn, args.timeout, first_only=False)[0]
        (copy / args.target).write_text(source, encoding="utf-8")
        if not round_trip:
            print("the tests are RED on the source round-tripped through ast.unparse: a test reads a comment or a line number")
            return 3

        def one(site):
            index, line, description = site
            mutated = ast.unparse(tool.apply(tree, index))
            try:
                compile(mutated, args.target, "exec")
            except (SyntaxError, ValueError):
                return site, "invalid", ""
            copy, dsn = slots.get()
            try:
                (copy / args.target).write_text(mutated, encoding="utf-8")
                ok, first = run_tests(copy, args.tests, dsn, args.timeout)
            finally:
                (copy / args.target).write_text(source, encoding="utf-8")
                slots.put((copy, dsn))
            return site, "survived" if ok else "killed", first

        verdicts = []
        with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for result in pool.map(one, sorted(sites, key=lambda s: s[1])):
                verdicts.append(result)
        by_function: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        survivors = []
        for (index, line, description), verdict, first in verdicts:
            row = by_function[owner.get(line, "(module)")]
            row[0] += 1
            if verdict == "killed":
                row[1] += 1
            elif verdict == "survived":
                row[2] += 1
                survivors.append((line, description))
        killed = sum(r[1] for r in by_function.values())
        survived = sum(r[2] for r in by_function.values())
        invalid = sum(1 for _, v, _ in verdicts if v == "invalid")
        print("%s: %d mutants judged (%d sites, %d did not compile), %d killed, %d survived" % (args.target, killed + survived, len(sites), invalid, killed, survived))
        for name, (n, k, s) in sorted(by_function.items(), key=lambda kv: kv[0]):
            print("  %-34s %3d sites  %3d killed  %3d survived" % (name, n, k, s))
        for line, description in sorted(survivors):
            print("SURVIVED %s:%d  %s" % (Path(args.target).name, line, description))
        if args.json:
            Path(args.json).write_text(json.dumps([{"line": s[1], "what": s[2], "function": owner.get(s[1], "(module)"), "verdict": v, "first_failing_test": f}
                                                   for s, v, f in verdicts], indent=1), encoding="utf-8")
        return 1 if survived else 0
    finally:
        for name in scratch:
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                admin.execute('drop database if exists "%s" with (force)' % name)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
