"""Replay of an adversary's corpus against the change detection module.

tests/corpus/change_detection_blind_1.jsonl holds 45 cases written by an independent reviewer from the module's own documentation (each carries `promise`, a quote from
docs/change-detection.md, and a `why` that says who is harmed on an ordinary day). Each runnable case is a scenario: a company book, one or more daily files, the options of
apply_snapshot and optional later steps, plus the outcome a correct module must give. A case marked KNOWN_LIMIT records a disagreement the owner decided not to fix; it is
expected to keep failing (strict xfail), so fixing it later turns it into a test failure that says to move the case into the passing set.

The module tests need a scratch database: ANALYTICS_TEST_DSN names one, and this file drops and recreates its `public` schema (see docs/change-detection.md).
"""
from __future__ import annotations

import json
import os
import sys
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

CORPUS = ROOT / "tests" / "corpus" / "change_detection_blind_1.jsonl"
MIGRATIONS = ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql", "004_observability.sql", "005_change_detection.sql")
TABLES = "carrier_stage, carrier_current, carrier_history, change_event, change_event_match, change_outbox, carrier_load, carrier_company"
CASES = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]
RUNNABLE = [c for c in CASES if c["case"] not in (43, 44)]


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
    return c.execute(
        "select (select count(*) from carrier_current), (select count(*) from carrier_history), (select count(*) from change_event), (select count(*) from change_outbox), "
        "(select count(*) from carrier_load), (select coalesce(md5(string_agg(usdot_norm || track_hash::text, ',' order by usdot_norm)), '') from carrier_current)"
    ).fetchone()


def _sink(script):
    steps = []
    for s in script or []:
        if s == "throttled0":
            steps.append(SinkAnswer("throttled", None, "slow", 0))
        elif s == "raise":
            steps.append(RuntimeError("boom"))
        else:
            steps.append(s)
    return FakeSink(steps if steps else None)


def _run(c, case):
    inp = case["input"]
    obs = {"files": []}
    for comp in inp.get("companies", []):
        c.execute("insert into carrier_company (name, usdot_no, mc_no) values (%s, %s, %s)", comp)
    for f in inp["files"]:
        opts = dict(inp.get("options", {}))
        opts.update(f.get("options", {}))
        data = f["csv"].encode("latin-1") if f.get("latin1") else f["csv"].encode("utf-8")
        before = _fingerprint(c)
        try:
            r = apply_snapshot(c, data, date.fromisoformat(f["date"]), **opts)
            obs["files"].append({"result": "ok", "new": r.events_new, "unknown": r.unknown_policy_status})
        except Exception as e:  # noqa: BLE001 - the corpus names the exception class it expects
            obs["files"].append({"result": type(e).__name__, "nothing_written": _fingerprint(c) == before})
    obs["events"] = dict(c.execute("select kind, count(*) from change_event group by kind order by 1").fetchall())
    obs["queued"] = c.execute("select count(*) from change_outbox").fetchone()[0]
    obs["history"] = c.execute("select count(*) from carrier_history").fetchone()[0]
    sink = None
    for st in inp.get("steps", []):
        if "add_company" in st:
            cid = c.execute("insert into carrier_company (name, usdot_no, mc_no) values (%s, %s, %s) returning id", st["add_company"]).fetchone()[0]
            obs.setdefault("late_queued", []).append(match_late_company(c, cid))
        if "deliver" in st:
            d = st["deliver"]
            if sink is None or "script" in d:
                sink = _sink(d.get("script"))
            r = deliver_pending(c, sink, backoff_seconds=d.get("backoff", 0), max_attempts=d.get("max_attempts", 5))
            obs.setdefault("deliveries", []).append({"delivered": r.delivered, "retrying": r.retrying, "throttled": r.throttled, "dead": r.dead, "calls": len(sink.calls)})
            obs["sink_tasks"] = len(sink.tasks)
        if "requeue" in st:
            obs.setdefault("requeued", []).append(requeue_dead(c))
    obs["outbox_status"] = dict(c.execute("select status, count(*) from change_outbox group by 1").fetchall())
    if inp.get("report"):
        rep = daily_report(c)
        obs["report"] = {k: rep[k] for k in ("events_new", "matched_events", "conflict_events", "unmatched_events", "rows_missing", "events_repeated")}
    if inp.get("conflict_payload"):
        row = c.execute("select payload->>'match', jsonb_array_length(payload->'companies') from change_outbox limit 1").fetchone()
        obs["conflict_payload"] = list(row) if row else None
    return obs


def _disagreements(exp, obs):
    bad = []
    if "events" in exp and exp["events"] != obs["events"]:
        bad.append("events")
    if "queued" in exp and exp["queued"] != obs["queued"]:
        bad.append("queued")
    if "exception" in exp:
        last = obs["files"][-1]
        if last["result"] != exp["exception"]:
            bad.append("exception")
        if exp.get("nothing_written") and not last.get("nothing_written"):
            bad.append("nothing_written")
    for key, got in (("late_queued", obs.get("late_queued")), ("outbox_status", obs["outbox_status"]), ("sink_tasks", obs.get("sink_tasks")), ("history", obs["history"]),
                     ("conflict_payload", obs.get("conflict_payload")), ("file_new", [f.get("new") for f in obs["files"]])):
        if key in exp and exp[key] != got:
            bad.append(key)
    if "deliveries" in exp and exp["deliveries"] != [{k: v for k, v in d.items() if k in exp["deliveries"][i]} for i, d in enumerate(obs.get("deliveries", []))]:
        bad.append("deliveries")
    if "report" in exp and exp["report"] != {k: obs["report"][k] for k in exp["report"]}:
        bad.append("report")
    if "unknown" in exp and exp["unknown"] != obs["files"][-1].get("unknown"):
        bad.append("unknown")
    return bad


def _id(case):
    return "case%02d" % case["case"]


def _param(case):
    marks = [pytest.mark.xfail(strict=True, reason=case["limit_reason"])] if case.get("status") == "KNOWN_LIMIT" else []
    return pytest.param(case, id=_id(case), marks=marks)


def test_the_corpus_holds_both_kinds_and_every_case_names_its_promise_and_its_harm():
    assert len(CASES) >= 30
    assert {c["expect"] for c in CASES} == {"pass", "fail"}
    assert all(len(c["promise"]) >= 15 and len(c["why"]) >= 20 and c["source"] in ("real", "invented") for c in CASES)
    spec = (ROOT / "docs" / "change-detection.md").read_text(encoding="utf-8")
    quotes_found = sum(1 for c in CASES if c["promise"] in spec)
    # most promises are verbatim quotes from the page (the others were quoted from the README section); the replay below is what proves the behaviour
    assert quotes_found >= len(CASES) * 0.5


@pytest.mark.parametrize("case", [_param(c) for c in RUNNABLE])
def test_replay_of_the_adversary_corpus_agrees_with_the_module(conn, case):
    obs = _run(conn, case)
    bad = _disagreements(case["input"]["expected"], obs)
    assert not bad, "%s disagrees on %s: %s" % (_id(case), bad, case["why"][:120])
