"""Replay of a second, blind corpus against the change detection module.

tests/corpus/change_detection_blind_2.jsonl holds 44 cases (34 must-pass and 10 must-fail) written by a reviewer who had seen neither the first corpus nor any earlier finding, against
the finished build. Each carries `promise`, a quote from docs/change-detection.md, and a `why` that says who is harmed on an ordinary day. The format differs a little from the first
corpus (the expected outcome sits beside `input`, and `steps` may deliver, add a company later, requeue or ask for a report), so it has its own replay. All 44 agreed with the code
when they were written; this file keeps them agreeing.

The tests drop and recreate the `public` schema of the database ANALYTICS_TEST_DSN names (see docs/change-detection.md): aim it at a scratch database.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@localhost:5433/analytics")


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="Postgres not reachable, run docker compose up -d")

from app.changes import SinkAnswer, apply_snapshot, daily_report, deliver_pending, match_late_company, requeue_dead  # noqa: E402
from app.changes_fake_sink import FakeSink  # noqa: E402

CORPUS = ROOT / "tests" / "corpus" / "change_detection_blind_2.jsonl"
MIGRATIONS = ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql", "004_observability.sql", "005_change_detection.sql")
TABLES = "carrier_stage, carrier_current, carrier_history, change_event, change_event_match, change_outbox, carrier_load, carrier_company"
CASES = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(scope="module")
def _schema():
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("drop schema if exists public cascade; create schema public")
        for name in MIGRATIONS:
            c.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))


@pytest.fixture()
def conn(_schema):
    c = psycopg.connect(DSN, autocommit=True)
    c.execute("set synchronous_commit = off; set session_replication_role = replica; truncate %s restart identity cascade; reset session_replication_role" % TABLES)
    yield c
    c.close()


def _fingerprint(c):
    return list(c.execute(
        "select (select count(*) from carrier_current), (select count(*) from carrier_history), (select count(*) from change_event), (select count(*) from change_outbox), "
        "(select count(*) from carrier_load), (select coalesce(md5(string_agg(usdot_norm || track_hash::text, ',' order by usdot_norm)), '') from carrier_current)").fetchone())


def _answer(step):
    if step == "raise":
        return RuntimeError("down")
    if step == "weird":
        return "ok-ish"
    if isinstance(step, dict) and "throttled" in step:
        return SinkAnswer("throttled", None, "429", step["throttled"])
    return step


def _run(c, case):
    inp = case["input"]
    obs = {}
    for name, dot, mc in inp["companies"]:
        c.execute("insert into carrier_company (name, usdot_no, mc_no) values (%s, %s, %s)", (name, dot, mc))
    files = inp["files"]
    last = None
    for i, f in enumerate(files):
        opts = dict(inp["options"])
        opts.update(f.get("options", {}))
        src = f["csv"].encode("utf-8")
        if f.get("as_path"):
            fd, path = tempfile.mkstemp(suffix=".csv")
            os.write(fd, src)
            os.close(fd)
            src = path
        before = _fingerprint(c)
        try:
            last = apply_snapshot(c, src, date.fromisoformat(f["date"]), **opts)
        except Exception as exc:  # noqa: BLE001 - the corpus names the exception class it expects
            if i == len(files) - 1 and case["expect"] == "fail":
                obs["exception"] = type(exc).__name__
                obs["unchanged"] = _fingerprint(c) == before
                break
            obs["exception_at_file"] = [i, type(exc).__name__, str(exc)[:160]]
            return obs
    else:
        if case["expect"] == "fail":
            obs["exception"] = None
    if last is not None:
        obs["rows_missing_last"] = last.rows_missing
    cid = None
    for st in inp["steps"]:
        if "add_company" in st:
            n, d, m = st["add_company"]
            cid = c.execute("insert into carrier_company (name, usdot_no, mc_no) values (%s, %s, %s) returning id", (n, d, m)).fetchone()[0]
            obs.setdefault("late_queued", []).append(match_late_company(c, cid))
        if st.get("late_match_again"):
            obs["late_queued"].append(match_late_company(c, cid))
        if "deliver" in st:
            dv = st["deliver"]
            sink = FakeSink([_answer(s) for s in dv["script"]], acts_before_failing=dv.get("acts_before_failing", False))
            for _ in range(dv.get("passes", 1)):
                deliver_pending(c, sink, max_attempts=dv.get("max_attempts", 5), backoff_seconds=dv.get("backoff_seconds", 60.0))
            obs["tasks"] = len(sink.tasks)
        if st.get("requeue"):
            obs["requeued"] = requeue_dead(c)
        if "report" in st:
            r = daily_report(c, date.fromisoformat(st["report"]))
            obs["report"] = {k: r[k] for k in ("matched_events", "unmatched_events", "conflict_events")}
    obs["events"] = dict(c.execute("select kind, count(*) from change_event group by 1").fetchall())
    obs["queued"] = c.execute("select count(*) from change_outbox").fetchone()[0]
    obs["outbox"] = [list(r) for r in c.execute("select status, attempts from change_outbox order by event_id").fetchall()]
    payload = c.execute("select payload from change_outbox order by event_id limit 1").fetchone()
    if payload:
        obs["payload_match"] = payload[0].get("match")
        obs["payload_companies"] = len(payload[0].get("companies", []))
    if files:
        obs["loads_on_date"] = c.execute("select count(*) from carrier_load where snapshot_date = %s", (date.fromisoformat(files[-1]["date"]),)).fetchone()[0]
    return obs


def _agrees(case, obs):
    exp = case["expected"]
    if case["expect"] == "fail":
        got = obs.get("exception")
        return got is not None and (exp["exception"] == "*" or got == exp["exception"]) and obs.get("unchanged") is True
    if "exception_at_file" in obs:
        return False
    return all(obs.get(k) == v for k, v in exp.items())


def test_the_second_corpus_holds_both_kinds_and_every_case_names_its_promise_and_its_harm():
    assert len(CASES) >= 30
    assert {c["expect"] for c in CASES} == {"pass", "fail"}
    assert all(len(c["promise"]) >= 15 and len(c["why"]) >= 20 and c["source"] in ("real", "invented") for c in CASES)
    spec = (ROOT / "docs" / "change-detection.md").read_text(encoding="utf-8")
    assert sum(1 for c in CASES if c["promise"] in spec) >= len(CASES) * 0.8


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_replay_of_the_blind_corpus_agrees_with_the_module(conn, case):
    obs = _run(conn, case)
    assert _agrees(case, obs), "%s disagrees: %s | expected %s | %s" % (case["id"], json.dumps(obs)[:300], json.dumps(case["expected"])[:200], case["why"][:100])
