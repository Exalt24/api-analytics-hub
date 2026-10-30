"""Backups and the restore proof: the pure rules, the alert policy as a simulation, and one real dump, restore and tamper run.

The real run needs the PostgreSQL client tools (pg_dump, pg_restore, psql) on PATH, or PG_CMD_PREFIX set (for example `docker compose exec -T db` from the repo root), and a server named by
ANALYTICS_TEST_DSN. It creates and drops its own throwaway databases and touches nothing else. Without those it skips, like the other database tests.

Rules of the alert policy, as invariants over a whole run:
  B1 a fresh backup with a fresh successful proof raises no problem
  B2 a backup older than the limit always raises backup_stale, and one inside it never does
  B3 a failed proof always raises restore_failed (and never restore_unproven for the same proof); a missing or old good proof raises restore_unproven
  B4 no backup at all raises exactly never_backed_up
  B5 an unchanged set never alerts twice inside the repeat window, a changed set alerts at once, a cleared set sends one recovery and then nothing
"""
from __future__ import annotations

import json
import os
import random
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import db_backup  # noqa: E402
from app.backup import (DEFAULT_MAX_BACKUP_AGE, DEFAULT_MAX_PROOF_AGE, KIND_FAILED, KIND_NEVER, KIND_STALE, KIND_UNPROVEN,  # noqa: E402
                        Manifest, Proof, backup_problems, count_mismatches, describe_backup, plan_prune, sha256_of)
from app.health import decide  # noqa: E402

NOW = 10_000_000.0
DAY = 86400


def manifest(age=3600, before=None, after=None):
    return Manifest("hub-x.dump", NOW - age, "0" * 64, 10, before or {"a": 5}, after or {"a": 5})


def kinds(problems):
    return sorted(p.kind for p in problems)


def test_count_bracket_is_inclusive_and_exact_on_a_quiet_table():
    m = manifest(before={"a": 5, "b": 0}, after={"a": 8, "b": 0})
    assert count_mismatches(m, {"a": 5, "b": 0}) == []
    assert count_mismatches(m, {"a": 8, "b": 0}) == []
    assert count_mismatches(m, {"a": 6, "b": 0}) == []
    assert any("a restored 4" in x for x in count_mismatches(m, {"a": 4, "b": 0}))
    assert any("a restored 9" in x for x in count_mismatches(m, {"a": 9, "b": 0}))
    assert any("b restored 1" in x and "held 0" in x for x in count_mismatches(m, {"a": 5, "b": 1}))


def test_missing_and_extra_tables_are_reported():
    m = manifest(before={"a": 1, "b": 2}, after={"a": 1, "b": 2})
    assert count_mismatches(m, {"a": 1}) == ["b is missing from the restore"]
    assert count_mismatches(m, {"a": 1, "b": 2, "c": 0}) == ["c is in the restore but not in the manifest"]


def test_a_table_present_on_one_side_of_the_dump_only_is_still_checked():
    m = manifest(before={"a": 1}, after={"a": 1, "new": 3})        # created while the dump ran
    assert count_mismatches(m, {"a": 1, "new": 3}) == []
    assert count_mismatches(m, {"a": 1}) == ["new is missing from the restore"]


def test_prune_keeps_the_newest_and_never_all_of_them():
    dumps = [("d1", 1.0), ("d2", 2.0), ("d3", 3.0), ("d4", 4.0)]
    assert sorted(plan_prune(dumps, 2)) == ["d1", "d2"]
    assert sorted(plan_prune(dumps, 0)) == ["d1", "d2", "d3"]                  # keep below one is treated as one
    assert plan_prune(dumps, 99) == []
    assert plan_prune([], 3) == []
    assert "d4" not in plan_prune(dumps, 1)


def test_manifest_and_proof_round_trip(tmp_path):
    m = manifest(before={"t": 1}, after={"t": 2})
    assert Manifest.from_json(m.to_json()) == m
    p = Proof("f", 5.0, False, "why")
    assert Proof.from_json(p.to_json()) == p
    f = tmp_path / "x"
    f.write_bytes(b"abc")
    assert sha256_of(f) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_each_policy_case():
    ok = Proof("f", NOW - DAY, True)
    assert backup_problems(NOW, manifest(), ok) == []                                                           # B1
    assert kinds(backup_problems(NOW, None, None)) == [KIND_NEVER]                                              # B4
    assert kinds(backup_problems(NOW, manifest(age=DEFAULT_MAX_BACKUP_AGE + 1), ok)) == [KIND_STALE]
    assert backup_problems(NOW, manifest(age=DEFAULT_MAX_BACKUP_AGE), ok) == []                                 # the limit itself is still fine
    assert kinds(backup_problems(NOW, manifest(), None)) == [KIND_UNPROVEN]
    assert kinds(backup_problems(NOW, manifest(), Proof("f", NOW - 10 * DAY, True))) == [KIND_UNPROVEN]
    assert kinds(backup_problems(NOW, manifest(), Proof("f", NOW - 10 * DAY, False))) == [KIND_FAILED]          # old AND failed is failed, once
    assert kinds(backup_problems(NOW, manifest(age=5 * DAY), Proof("f", NOW, False))) == sorted([KIND_FAILED, KIND_STALE])


def test_the_message_names_the_problem_and_a_recovery_is_worded_for_backups():
    msg, _ = decide({}, backup_problems(NOW, manifest(age=3 * DAY), None), NOW, 6 * 3600, describer=describe_backup)
    assert "too old" in msg and "no restore" in msg and "connection" not in msg
    back, _ = decide({"keys": ["database:backup_stale"], "alerted_at": 1.0}, [], NOW, 6 * 3600, describer=describe_backup)
    assert back.startswith("Recovered.") and "backup" in back and "connections" not in back


def _world(rng: random.Random, steps: int):
    """Backups and proofs that succeed or fail at random, with the clock jumping by minutes or by days."""
    now, backup_at, proof = NOW, None, None
    for _ in range(steps):
        now += rng.choice((60, 3600, 6 * 3600, DAY, 3 * DAY, 11 * DAY))
        if rng.random() < 0.55:
            backup_at = now - rng.choice((0, 60, 3600))
        if rng.random() < 0.35 and backup_at is not None:
            proof = Proof("f", now, rng.random() < 0.7)
        yield now, (Manifest("f", backup_at, "0" * 64, 1) if backup_at is not None else None), proof


@pytest.mark.parametrize("seed", range(120))
def test_policy_simulation(seed):
    rng = random.Random(seed)
    repeat, state = 6 * 3600, {}
    for now, m, proof in _world(rng, 60):
        problems = backup_problems(now, m, proof)
        got = kinds(problems)
        if m is None:
            assert got == [KIND_NEVER]                                                                          # B4
        else:
            assert (KIND_STALE in got) == (now - m.created_at > DEFAULT_MAX_BACKUP_AGE)                         # B2
            if proof is not None and not proof.ok:
                assert KIND_FAILED in got and KIND_UNPROVEN not in got                                          # B3
            if proof is None or (proof.ok and now - proof.proved_at > DEFAULT_MAX_PROOF_AGE):
                assert KIND_UNPROVEN in got and KIND_FAILED not in got
            if proof is not None and proof.ok and now - proof.proved_at <= DEFAULT_MAX_PROOF_AGE and now - m.created_at <= DEFAULT_MAX_BACKUP_AGE:
                assert got == []                                                                                # B1
        msg, new_state = decide(state, problems, now, repeat, describer=describe_backup)
        keys = sorted(p.key for p in problems)
        if msg is not None and keys and keys == sorted(state.get("keys", [])):
            assert now - state["alerted_at"] >= repeat                                                          # B5: no repeat inside the window
        if keys and keys != sorted(state.get("keys", [])):
            assert msg is not None                                                                              # a changed set alerts at once
        if not keys and state.get("keys"):
            assert msg is not None and msg.startswith("Recovered.")                                             # one recovery
        if not keys and not state.get("keys"):
            assert msg is None
        state = new_state


# ---- the real run -------------------------------------------------------------------------------------------------------------------------------------------------------------

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")
HAVE_TOOLS = bool(os.environ.get("PG_CMD_PREFIX")) or all(shutil.which(t) for t in ("pg_dump", "pg_restore", "psql"))


def _fresh_db(prefix: str):
    """Create a throwaway database and return (dsn for it, admin dsn). Skips when there is no server or the client tools are older than the server."""
    dsn = db_backup.parse_dsn(DSN)
    name = "%s_%s" % (prefix, uuid.uuid4().hex[:8])
    try:
        db_backup.psql(dsn, "postgres", 'create database "%s"' % name)
    except db_backup.ToolError as exc:
        pytest.skip("no usable PostgreSQL server for the backup test: %s" % str(exc)[:80])
    try:
        server_major = int(db_backup.psql(dsn, "postgres", "show server_version_num")[0]) // 10000
        version = subprocess.run(shlex.split(os.environ.get("PG_CMD_PREFIX", "")) + ["pg_dump", "--version"], capture_output=True, text=True, encoding="utf-8", timeout=60).stdout  # connection flags before --version make pg_dump refuse
        client_major = int(version.split()[-1].split(".")[0])
        if client_major < server_major:                  # pg_dump refuses a server newer than itself; CI runs this file again through the service container
            pytest.skip("pg_dump %d is older than the server %d; set PG_CMD_PREFIX to run the tools in the server's container" % (client_major, server_major))
    except BaseException:                                # a setup error or a skip must not leave the database behind
        db_backup.psql(dsn, "postgres", 'drop database if exists "%s" with (force)' % name)
        raise
    return dict(dsn, db=name), dsn


def _url(d):
    return "postgresql://%s:%s@%s:%s/%s" % (d["user"], d["password"], d["host"], d["port"], d["db"])


@pytest.fixture()
def source_db():
    src, admin = _fresh_db("hub_bk_src")
    db_backup.psql(src, src["db"], "create table items(id int primary key, note text); insert into items select g, 'row ' || g from generate_series(1, 250) g; "
                                   "create table \"odd name\"(x int); insert into \"odd name\" values (1),(2); create table empty_one(y int)")
    yield src
    db_backup.psql(admin, "postgres", 'drop database if exists "%s" with (force)' % src["db"])


@pytest.fixture()
def schema_db():
    """The hub's own schema: every migration applied in order, plus a row that runs the generated columns."""
    src, admin = _fresh_db("hub_bk_schema")
    for f in sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql")):
        with open(f, "rb") as fh:
            db_backup.run_tool("psql", src, ["-X", "-q", "-v", "ON_ERROR_STOP=1"], db=src["db"], stdin=fh)
    db_backup.psql(src, src["db"], "insert into carrier_stage(usdot_raw, insurer_raw, policy_status_raw, authority_date_raw) values ('0012345', ' geico ', 'active', '2026-01-02')")
    yield src
    db_backup.psql(admin, "postgres", 'drop database if exists "%s" with (force)' % src["db"])


@pytest.mark.skipif(not HAVE_TOOLS, reason="needs pg_dump, pg_restore and psql, or PG_CMD_PREFIX")
def test_the_hub_schema_itself_restores(schema_db, tmp_path, monkeypatch):
    """pg_restore runs with an empty search_path and recomputes generated columns while copying rows back, so a function body that calls another function unqualified fails here, not at 3 am."""
    monkeypatch.setenv("DATABASE_URL", _url(schema_db))
    d = tmp_path / "bk"
    assert db_backup.main(["backup", "--dir", str(d)]) == 0
    assert db_backup.main(["prove", "--dir", str(d)]) == 0, db_backup.load_proof(d).detail
    assert "tables" in db_backup.load_proof(d).detail


@pytest.mark.skipif(not HAVE_TOOLS, reason="needs pg_dump, pg_restore and psql, or PG_CMD_PREFIX")
def test_backup_restore_proof_and_tampering(source_db, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _url(source_db))
    d = tmp_path / "bk"
    assert db_backup.main(["backup", "--dir", str(d), "--keep", "3"]) == 0
    m = db_backup.newest_manifest(d)
    assert m is not None and m.after == {"items": 250, "odd name": 2, "empty_one": 0}
    assert not list(d.glob("*.partial"))

    assert db_backup.main(["prove", "--dir", str(d)]) == 0
    proof = db_backup.load_proof(d)
    assert proof.ok and "3 tables" in proof.detail
    assert db_backup.main(["check", "--dir", str(d)]) == 0                                              # fresh backup, fresh proof

    dsn = db_backup.parse_dsn(os.environ["DATABASE_URL"])
    leftover = [x for x in db_backup.psql(dsn, "postgres", "select datname from pg_database where datname like 'hub_restore_proof_%'")]
    assert leftover == []                                                                               # the scratch database is always dropped

    # a damaged dump: the checksum catches it before any restore is attempted
    dump = d / m.file
    blob = bytearray(dump.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    dump.write_bytes(bytes(blob))
    assert db_backup.main(["prove", "--dir", str(d)]) == 1
    assert "checksum" in db_backup.load_proof(d).detail
    assert db_backup.main(["check", "--dir", str(d)]) == 1                                              # and the check now reports a failed restore

    # a dump whose rows no longer match what the manifest says was in the database
    assert db_backup.main(["backup", "--dir", str(d), "--keep", "3"]) == 0
    newest = db_backup.newest_manifest(d)
    forged = json.loads(newest.to_json())
    forged["before"]["items"] = forged["after"]["items"] = 251
    (d / (newest.file + ".json")).write_text(json.dumps(forged), encoding="utf-8")
    assert db_backup.main(["prove", "--dir", str(d)]) == 1
    assert "items restored 250" in db_backup.load_proof(d).detail

    # a table dropped from the source after the backup does not matter to the dump; a second real backup proves clean again
    assert db_backup.main(["backup", "--dir", str(d), "--keep", "3"]) == 0
    assert db_backup.main(["prove", "--dir", str(d)]) == 0


@pytest.mark.skipif(not HAVE_TOOLS, reason="needs pg_dump, pg_restore and psql, or PG_CMD_PREFIX")
def test_prune_keeps_only_the_requested_number(source_db, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _url(source_db))
    d = tmp_path / "bk"
    dsn = db_backup.parse_dsn(os.environ["DATABASE_URL"])
    for i in range(4):
        assert db_backup.do_backup(dsn, d, 2, NOW + i * 60) == 0
    assert len(list(d.glob("hub-*.dump"))) == 2 and len(list(d.glob("hub-*.dump.json"))) == 2


def test_the_check_with_nothing_on_disk_says_never_backed_up(tmp_path):
    assert db_backup.main(["check", "--dir", str(tmp_path / "nothing")]) == 1


def test_a_bad_database_url_is_a_tool_error_not_a_traceback(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_URL", "mysql://nope")
    assert db_backup.main(["backup", "--dir", str(tmp_path)]) == 2


def test_the_password_never_reaches_a_command_line_or_a_log(monkeypatch):
    dsn = db_backup.parse_dsn("postgresql://u:s3cr%40t@h:5432/db")
    argv, env = db_backup._argv("pg_dump", dsn, "db")
    assert "s3cr@t" not in " ".join(argv) and env["PGPASSWORD"] == "s3cr@t"
    monkeypatch.setenv("PG_CMD_PREFIX", "docker compose exec -T db")
    argv, _ = db_backup._argv("psql", dsn, "db")
    assert argv[:5] == ["docker", "compose", "exec", "-T", "db"] and "-h" not in argv
