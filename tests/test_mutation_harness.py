"""The mutation harness edits source in place, so its safety is tested.

`--help` explains that, an argument it does not know is refused before anything is edited, the
interpreter path is chosen by platform, and a run that is interrupted while a mutation is in
the file leaves the file exactly as it was, byte for byte (including its line endings).
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutation_check as mc  # noqa: E402


def digest_of_targets() -> dict[str, str]:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(mc.TARGETS.values()))}


def run_harness(*args):
    return subprocess.run([sys.executable, str(ROOT / "tests" / "mutation_check.py"), *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=ROOT)


def test_help_says_it_edits_in_place_and_exits_zero_without_touching_anything():
    before = digest_of_targets()
    done = run_harness("--help")
    assert done.returncode == 0
    assert "EDITS SOURCE FILES IN PLACE" in done.stdout and "COPY of the repository" in done.stdout
    assert "--check-targets" in done.stdout and "--only" in done.stdout
    assert digest_of_targets() == before


@pytest.mark.parametrize("args", [("--bogus",), ("everything",), ("--only",), ("--check-target",), ("--onl", "report")])
def test_an_argument_it_does_not_know_is_refused_and_nothing_is_edited(args):
    before = digest_of_targets()
    done = run_harness(*args)
    assert done.returncode == 2
    assert "usage:" in done.stderr
    assert digest_of_targets() == before


def test_check_targets_runs_nothing_and_edits_nothing():
    before = digest_of_targets()
    done = run_harness("--check-targets")
    assert done.returncode == 0 and "guards find their target" in done.stdout
    assert digest_of_targets() == before


def test_the_interpreter_is_the_venv_one_when_it_exists_for_this_platform_else_the_running_one():
    path = mc.venv_python()
    expected = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    assert path == ROOT.joinpath(".venv", *expected.split("/")) or path == Path(sys.executable)


def test_an_interrupted_run_leaves_every_file_byte_for_byte_as_it_was(tmp_path, monkeypatch):
    target = tmp_path / "guarded.py"
    original = b"def check(x):\r\n    if x < 0:\r\n        raise ValueError\r\n    return x\r\n"
    target.write_bytes(original)
    monkeypatch.setattr(mc, "TARGETS", {"t": target})
    monkeypatch.setattr(mc, "MUTATIONS", [("t", "the guard", "        raise ValueError", "        pass", "why")])
    monkeypatch.setattr(mc, "SCOPED_SUITE", {})
    calls = []

    def fake_suite(timeout=0, path="tests/"):
        calls.append(target.read_bytes())
        if len(calls) == 2:                       # the mutated run
            raise KeyboardInterrupt
        return True

    monkeypatch.setattr(mc, "run_suite", fake_suite)
    with pytest.raises(KeyboardInterrupt):
        mc.main([])
    assert b"        pass" in calls[1]            # the mutation really was in the file when it was interrupted
    assert target.read_bytes() == original


def test_a_run_that_fails_halfway_still_restores_the_file(tmp_path, monkeypatch):
    target = tmp_path / "guarded.py"
    original = b"x = 1\nif x:\n    pass\n"
    target.write_bytes(original)
    monkeypatch.setattr(mc, "TARGETS", {"t": target})
    monkeypatch.setattr(mc, "MUTATIONS", [("t", "the guard", "if x:", "if False:", "why")])
    monkeypatch.setattr(mc, "SCOPED_SUITE", {})
    monkeypatch.setattr(mc, "run_suite", lambda timeout=0, path="tests/": False if target.read_bytes() != original else True)
    assert mc.main([]) == 0
    assert target.read_bytes() == original
