"""The ordering portal's mutation harness edits source in place, so its safety is tested, like tests/test_mutation_check_esign.py does for the e-signature one.

`--help` says so, an argument it does not know is refused before anything is edited, every guard finds its text exactly once, there are at least 100 of them with unique names and each names the
tests that must catch it, every named fragment matches a real test, a run that is interrupted while a mutation is in a file leaves every file exactly as it was, byte for byte, and a surviving
mutant, one killed by the wrong test and a skipped suite are each a failure.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutation_check_fulfillment as mc  # noqa: E402


def digest_of_targets() -> dict[str, str]:
    return {k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in mc.TARGETS.items()}


def run_harness(*args):
    return subprocess.run([sys.executable, str(ROOT / "tests" / "mutation_check_fulfillment.py"), *args], capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=ROOT)


def test_help_says_it_edits_in_place_and_exits_zero_without_touching_anything():
    before = digest_of_targets()
    done = run_harness("--help")
    assert done.returncode == 0 and "EDITS SOURCE FILES IN PLACE" in done.stdout and "COPY of the repository" in done.stdout
    assert digest_of_targets() == before


@pytest.mark.parametrize("args", [("--bogus",), ("everything",), ("--match",), ("--check-target",)])
def test_an_argument_it_does_not_know_is_refused_and_nothing_is_edited(args):
    before = digest_of_targets()
    done = run_harness(*args)
    assert done.returncode == 2 and "usage:" in done.stderr
    assert digest_of_targets() == before


def test_every_guard_finds_its_text_once_and_there_are_at_least_100_with_unique_names():
    assert mc.missing_targets() == []
    names = [m[0] for m in mc.MUTATIONS]
    assert len(names) >= 100 and len(set(names)) == len(names)
    assert all(m[4] for m in mc.MUTATIONS), "every guard must name the tests that must catch it"
    done = run_harness("--check-targets")
    assert done.returncode == 0 and "guards find their target" in done.stdout


def test_every_target_file_has_a_guard_and_every_guard_changes_the_text():
    used = {m[1] for m in mc.MUTATIONS}
    assert used == set(mc.TARGETS), set(mc.TARGETS) ^ used
    assert all(m[2] != m[3] for m in mc.MUTATIONS)


def test_every_named_test_fragment_matches_a_real_test():
    """A fragment that matches no test would make its guard pass as 'no test ran, nothing failed', so each one is looked up among the collected test ids."""
    collected = subprocess.run([sys.executable, "-m", "pytest", *mc.SUITES, "--collect-only", "-q", "-p", "no:cacheprovider"], capture_output=True, text=True, encoding="utf-8",
                               timeout=300, cwd=ROOT).stdout.lower()
    missing = sorted({f for m in mc.MUTATIONS for f in m[4] if f.lower() not in collected})
    assert not missing, missing


def test_an_interrupted_run_leaves_every_file_byte_for_byte_as_it_was(tmp_path, monkeypatch):
    target = tmp_path / "guarded.py"
    original = b"def check(x):\r\n    if x < 0:\r\n        raise ValueError\r\n    return x\r\n"
    target.write_bytes(original)
    monkeypatch.setattr(mc, "TARGETS", {"t": target})
    monkeypatch.setattr(mc, "MUTATIONS", [("the guard", "t", "        raise ValueError", "        pass", ["x"], "why")])
    calls = []

    def fake_suite(select=None, timeout=0):
        calls.append(target.read_bytes())
        if len(calls) == 2:                       # the mutated run
            raise KeyboardInterrupt
        return True, []

    monkeypatch.setattr(mc, "run_suite", fake_suite)
    with pytest.raises(KeyboardInterrupt):
        mc.main([])
    assert b"        pass" in calls[1]            # the mutation really was in the file when it was interrupted
    assert target.read_bytes() == original


def test_a_surviving_mutant_and_one_killed_by_the_wrong_test_are_both_failures(tmp_path, monkeypatch, capsys):
    target = tmp_path / "guarded.py"
    target.write_bytes(b"x = 1\nif x:\n    pass\n")
    monkeypatch.setattr(mc, "TARGETS", {"t": target})
    monkeypatch.setattr(mc, "MUTATIONS", [("survives", "t", "if x:", "if False:", ["a"], "why"), ("wrong test", "t", "x = 1", "x = 2", ["a"], "why")])
    answers = iter([(True, []), (True, []), (False, ["tests/x.py::test_other"]), (True, [])])
    monkeypatch.setattr(mc, "run_suite", lambda select=None, timeout=0: next(answers))
    assert mc.main([]) == 1
    out = capsys.readouterr().out
    assert "survives: the named tests stayed GREEN" in out and "wrong test" in out and "2 UNTESTED GUARD(S)" in out


def test_a_run_that_skipped_tests_is_refused_because_a_skipped_test_cannot_kill_a_mutant(monkeypatch):
    class Done:
        returncode = 0
        stdout = "SKIPPED [1] tests/test_x.py:3: Postgres not reachable\n1 passed, 1 skipped\n"

    monkeypatch.setattr(mc.subprocess, "run", lambda *a, **k: Done())
    with pytest.raises(SystemExit) as err:
        mc.run_suite("anything")
    assert "skipped tests proves nothing" in str(err.value)
