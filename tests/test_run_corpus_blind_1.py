"""Replay of the blind corpus for the monthly run: the endpoint, the n8n workflow's request, the model text check and the deck snapshots (output round 1).

tests/corpus/hub_run_corpus_blind_1.jsonl holds 40 cases (22 must-pass and 18 must-fail) written by a reviewer who read docs/MONTHLY_REPORT.md,
n8n/README.md and the code. Each carries `promise` (a quote from the spec), `why` (who is harmed on an ordinary day), `source` (invented: every tenant, key and row is
made up) and `expected`. The adversary also recorded `actual`, what the code did at 0e30795 when it wrote the case.

Every case is replayed here against the real code: the endpoint in process against Postgres with real row level security (the scratch database the DSN names), the
workflow's own key and body expressions evaluated in node from the committed JSON, the model text check and the deck builds offline. NOT_REPLAYABLE is empty on purpose.
The endpoint cases need Postgres and the workflow cases need node; they are skipped, never failed, when either is missing, and run in CI.

Cases the code now answers differently from the recorded `actual` are the fixed findings: F1 (cases 17 and 18, the scheduled deck was a day short for ads and search;
the replay syncs ads and search one hour before the committed schedule fires, the relation the case describes, and builds the deck at the schedule's time) and F4
(case 22, the workflow's body could not carry the ad account; the replay evaluates the committed body expression for the tenant entry and posts what it prints, which
is also how case 13 is built). KNOWN_LIMITS are the rejected findings, pinned to the recorded `actual`: F7 (cases 7 and 8, a pasted display name or an id with a
trailing space as a connection id gives a 500 where a 422 reason would help, needs a hand typed wrong value in a field no schedule fills) and F8 (case 19, a delivery
mark picks the run by month, not by key, needs a refresh deck built under a second key and then marked delivered).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import AsyncExitStack
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from report_fixtures import day_rows  # noqa: E402
from sources_fixtures import paid_day, search_day, top  # noqa: E402

CORPUS = ROOT / "tests" / "corpus" / "hub_run_corpus_blind_1.jsonl"
CASES = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]

#: case number -> why it is not replayed offline. Empty: all 40 are replayed.
NOT_REPLAYABLE: dict[int, str] = {}

#: case number -> why the code is deliberately left as it is (the lead's decision, scored 2x1 or less). The live outcome is pinned to the recorded `actual`.
KNOWN_LIMITS = {
    7: "F7 KNOWN_LIMIT 2x1: a display name pasted as paid_connection_id gives a 500, needs a hand typed wrong value in a field no schedule fills",
    8: "F7 KNOWN_LIMIT 2x1: a uuid with a trailing space gives the same 500, the same hand typed wrong value",
    19: "F8 KNOWN_LIMIT 1x2: mark_delivered picks the run by month, not by key, needs a refresh run built under a second key and then a delivery mark",
}

#: the cases the old code failed and this change fixed; each is checked by its own predicate below
FIXED = {17, 18, 22}

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")
NODE = shutil.which("node")

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


needs_postgres = pytest.mark.skipif(psycopg is None or not _reachable(), reason="Postgres not reachable, run docker compose up -d")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed: the workflow's expressions are evaluated by node")

# ---------------------------------------------------------------- the invented rows (the adversary's run_endpoint_cases.py, reproduced)


def days(y, m, upto=None):
    d = date(y, m, 1)
    while d.month == m and (upto is None or d <= upto):
        yield d
        d += timedelta(days=1)


def store_rows():
    out = []
    for m in (9, 10):
        for d in days(2026, m):
            o = 10 + d.day % 7
            out += day_rows(d, o, o * 3100 + d.day, 0, o - 2)
    return out


def ads_rows(upto: date, currency="EUR"):
    out = []
    for m in (9, 10):
        for d in days(2026, m):
            if d > upto:
                break
            out += paid_day(d, 20000 + d.day * 10, 5000, 90 + d.day % 5, 4, 30000, currency=currency)
    return out


def search_rows(upto: date):
    out = []
    for m in (9, 10):
        for d in days(2026, m):
            if d > upto:
                break
            out += search_day(d, 120 + d.day % 9, 3000, 8400)
    return out


def _schedule_instant() -> datetime:
    from test_n8n_class_tables import _schedule_time
    return _schedule_time(2026, 11)


# ---------------------------------------------------------------- the workflow's own expressions, evaluated in node

_NODE_SCRIPT = r"""
const fs = require('fs');
const wf = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const build = wf.nodes.find(n => n.name === 'Build the report');
const keyExpr = build.parameters.headerParameters.parameters.find(p => p.name === 'Idempotency-Key').value;
const bodyExpr = build.parameters.jsonBody;
const strip = s => s.replace(/^=\{\{\s*/, '').replace(/\s*\}\}$/, '');
const items = JSON.parse(process.argv[2]);
console.log(JSON.stringify(items.map($json => ({ key: eval(strip(keyExpr)), body: eval(strip(bodyExpr)) }))));
"""


def workflow_requests(items: list[dict]) -> list[dict]:
    """For each `$json` item, the idempotency key and the body text the committed workflow's Build the report node would send."""
    done = subprocess.run([NODE, "-e", _NODE_SCRIPT, str(ROOT / "n8n" / "monthly_report.workflow.json"), json.dumps(items)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _body_of(item: dict) -> str:
    """The body text for one item: from the workflow's expression when node is there, else the same rule written in Python."""
    if NODE is not None:
        return workflow_requests([item])[0]["body"]
    body = {"month": item["month"], **({"as_of": item["as_of"]} if item["as_of"] else {})}
    body.update({k: v for k, v in item["tenant"].items() if k.endswith("connection_id") and v})
    return json.dumps(body, separators=(",", ":"))


# ---------------------------------------------------------------- the endpoint scenario (run_endpoint_cases.py, reproduced), in a scratch database

MIGRATIONS = sorted(p.name for p in (ROOT / "migrations").glob("*.sql"))


def _sql(query, params=()):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


def _seed(schedule: datetime):
    from app.api.auth import generate_key, hash_key
    from app.connectors.ga4 import settled_window_end

    keys, ids = {}, {}
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade; create schema public")
        for name in MIGRATIONS:
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))
        cur.execute("grant usage on schema public to sync_worker")

        def tenant(slug):
            t = uuid.uuid4()
            cur.execute("select set_config('app.tenant_id', %s, false)", (str(t),))
            cur.execute("insert into tenant (id, slug, name) values (%s, %s, %s)", (t, slug, slug))
            return t

        def conn_(t, platform, account, name):
            cur.execute("select set_config('app.tenant_id', %s, false)", (str(t),))
            cur.execute("insert into platform_connection (tenant_id, platform, external_account, display_name, credentials_enc) "
                        "values (%s, %s, %s, %s, %s) returning id", (t, platform, account, name, b"ciphertext"))
            return cur.fetchone()[0]

        def load(t, c, rows, windows):
            cur.execute("select set_config('app.tenant_id', %s, false)", (str(t),))
            payload = [{"tenant_id": str(t), "connection_id": str(c), "metric_key": r["metric_key"], "observed_on": r["observed_on"].isoformat(),
                        "value_numeric": r["value_numeric"], "currency": r["currency"], "raw": {}} for r in rows]
            if payload:
                cur.execute("select upsert_snapshots(%s::jsonb)", (json.dumps(payload),))
            for a, b in windows:
                cur.execute("insert into sync_run (tenant_id, connection_id, trigger, window_start, window_end, status, finished_at) "
                            "values (%s, %s, 'schedule', %s, %s, 'succeeded', now())", (t, c, a, b))

        a = tenant("example-outfitters")
        ids["a_store"] = conn_(a, "shopify", "ex.myshopify.com", "Example Outfitters")
        ids["a_ads"] = conn_(a, "meta", "act_100", "Example ads")
        ids["a_search"] = conn_(a, "gsc", "sc-domain:ex.example", "Example search")
        full = [(date(2026, 9, 1), date(2026, 11, 2))]
        load(a, ids["a_store"], store_rows(), full)
        load(a, ids["a_ads"], ads_rows(date(2026, 10, 31)), full)
        load(a, ids["a_search"], search_rows(date(2026, 10, 31)), full)
        # S: the scheduled run. The store was synced by the runner's default end (the date today, Nov 1); ads and search by their scripts'
        # default end, settled_window_end at a sync one hour before the committed schedule fires.
        s = tenant("harbor-coffee")
        ids["s_store"] = conn_(s, "shopify", "hc.myshopify.com", "Harbor Coffee")
        ids["s_ads"] = conn_(s, "meta", "act_200", "Harbor ads")
        ids["s_search"] = conn_(s, "gsc", "sc-domain:hc.example", "Harbor search")
        settled = settled_window_end(schedule - timedelta(hours=1))
        ids["settled_end"] = settled.isoformat()
        load(s, ids["s_store"], store_rows(), [(date(2026, 9, 1), date(2026, 11, 1))])
        load(s, ids["s_ads"], ads_rows(settled - timedelta(days=1)), [(date(2026, 9, 1), settled)])
        load(s, ids["s_search"], search_rows(settled - timedelta(days=1)), [(date(2026, 9, 1), settled)])
        d = tenant("dune-surf")
        ids["d_store"] = conn_(d, "shopify", "ds.myshopify.com", "Dune Surf")
        ids["d_ads1"] = conn_(d, "meta", "act_301", "Dune main ads")
        ids["d_ads2"] = conn_(d, "meta", "act_302", "Dune old ads")
        load(d, ids["d_store"], store_rows(), full)
        load(d, ids["d_ads1"], ads_rows(date(2026, 10, 31)), full)
        n = tenant("north-bakery")
        b = tenant("bay-books")
        ids["b_store"] = conn_(b, "shopify", "bb.myshopify.com", "Bay Books")
        load(b, ids["b_store"], store_rows(), full)
        e = tenant("ember-tea")
        ids["e_store"] = conn_(e, "shopify", "et.myshopify.com", "Ember Tea")
        ids["e_ads"] = conn_(e, "meta", "act_500", "Ember ads")
        load(e, ids["e_store"], store_rows(), full)
        mixed = ads_rows(date(2026, 10, 15)) + [r for d_ in days(2026, 10) if d_.day > 15 for r in paid_day(d_, 1000, 100, 5, 1, 2000, currency="USD")]
        load(e, ids["e_ads"], mixed, full)
        cur.execute("select set_config('app.tenant_id', '', false)")
        for label, t, role in (("a_op", a, "operator"), ("a_viewer", a, "viewer"), ("s_op", s, "operator"), ("d_op", d, "operator"),
                               ("n_op", n, "operator"), ("b_op", b, "operator"), ("e_op", e, "operator")):
            keys[label] = generate_key()
            cur.execute("insert into api_key (tenant_id, key_hash, label, role) values (%s, %s, %s, %s)", (t, hash_key(keys[label]), label, role))
    ids["tenants"] = {k: str(v) for k, v in (("a", a), ("s", s), ("d", d), ("n", n), ("b", b), ("e", e))}
    return keys, ids


def _health_and_notes(deck_bytes: bytes) -> str:
    from pptx import Presentation
    from app.report.deck import dump_deck
    text = dump_deck(Presentation(io.BytesIO(deck_bytes)))
    return "\n".join(ln for ln in text.splitlines() if any(w in ln for w in ("missing", "Data health", "Days with no data", "provisional", "May still change")))


async def _scenario(keys: dict, ids: dict, schedule: datetime) -> dict:
    import httpx
    from app.api.main import app as fastapi_app

    results: dict[str, dict] = {}
    stack = AsyncExitStack()
    await stack.enter_async_context(fastapi_app.router.lifespan_context(fastapi_app))
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test", timeout=120)

    async def post(who, key, body):
        headers = {"Authorization": "Bearer " + keys[who]}
        if key is not None:
            headers["Idempotency-Key"] = key
        if isinstance(body, str):
            return await c.post("/api/reports/monthly", headers={**headers, "Content-Type": "application/json"}, content=body)
        return await c.post("/api/reports/monthly", headers=headers, json=body)

    def rec(label, r, **extra):
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = {"bytes": len(r.content), "sha256": hashlib.sha256(r.content).hexdigest()}
        results[label] = {"status": r.status_code, "body": body, **extra}
        return r

    A = "monthly-report:example-outfitters:2026-10"
    r1 = rec("A1", await post("a_op", A, {"month": "2026-10", "as_of": "2026-11-02"}))
    rec("A2", await post("a_op", A, {"month": "2026-10", "as_of": "2026-11-02"}))
    g = await c.get("/api/reports/monthly/2026-10/deck", headers={"Authorization": "Bearer " + keys["a_viewer"]})
    rec("A3", g, sha_matches_build=hashlib.sha256(g.content).hexdigest() == r1.json().get("sha256"))
    rec("A4", await post("a_viewer", "viewer-try-000001", {"month": "2026-10"}))
    rec("A5", await post("a_op", A, {"month": "2026-09", "as_of": "2026-11-02"}))
    rec("A6", await post("a_op", "monthly report acme 2026-10", {"month": "2026-10"}))
    rec("A8", await post("a_op", None, {"month": "2026-10"}))
    rec("A9", await post("a_op", "monthly-report:ex-badid:2026-10", {"month": "2026-10", "as_of": "2026-11-02", "paid_connection_id": "Example ads"}))
    rec("A9b", await post("a_op", "monthly-report:ex-badid2:2026-10",
                          {"month": "2026-10", "as_of": "2026-11-02", "paid_connection_id": str(ids["a_ads"]) + " "}))
    rec("A10", await post("a_op", "monthly-report:ex-other:2026-10", {"month": "2026-10", "as_of": "2026-11-02", "paid_connection_id": str(ids["d_ads1"])}))
    rec("B1", await c.get("/api/reports/monthly/2026-10/deck", headers={"Authorization": "Bearer " + keys["b_op"]}))
    rec("B2", await post("b_op", A, {"month": "2026-10", "as_of": "2026-11-02"}))

    # D: two ad accounts, none named; then the request the committed workflow builds for a tenant entry that names the main one
    rec("D1", await post("d_op", "monthly-report:dune-surf:2026-10", {"month": "2026-10", "as_of": "2026-11-02"}))
    entry = {"name": "Dune Surf", "api_key": "<operator key>", "paid_connection_id": str(ids["d_ads1"])}
    sent = _body_of({"month": "2026-10", "as_of": "2026-11-02", "tenant": entry})
    rec("D2", await post("d_op", "monthly-report:dune-surf:2026-10", sent), body_sent=sent)

    rec("N1", await post("n_op", "monthly-report:north-bakery:2026-10", {"month": "2026-10", "as_of": "2026-11-02"}))
    t = ids["tenants"]["n"]
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select set_config('app.tenant_id', %s, false)", (t,))
        cur.execute("insert into platform_connection (tenant_id, platform, external_account, display_name, credentials_enc) "
                    "values (%s,'shopify','nb','North Bakery',%s) returning id", (t, b"x"))
        cid = cur.fetchone()[0]
        payload = [{"tenant_id": t, "connection_id": str(cid), "metric_key": r["metric_key"], "observed_on": r["observed_on"].isoformat(),
                    "value_numeric": r["value_numeric"], "currency": r["currency"], "raw": {}} for r in store_rows()]
        cur.execute("select upsert_snapshots(%s::jsonb)", (json.dumps(payload),))
        cur.execute("insert into sync_run (tenant_id, connection_id, trigger, window_start, window_end, status, finished_at) "
                    "values (%s,%s,'schedule','2026-09-01','2026-11-02','succeeded',now())", (t, cid))
    rec("N2", await post("n_op", "monthly-report:north-bakery:2026-10", {"month": "2026-10", "as_of": "2026-11-02"}))
    rec("E1", await post("e_op", "monthly-report:ember-tea:2026-10", {"month": "2026-10", "as_of": "2026-11-02"}))

    # S: the scheduled run, built at the committed schedule's time with no as_of (the date today is what the hub uses), after a sync an hour earlier
    S = "monthly-report:harbor-coffee:2026-10"
    s_as_of = schedule.date().isoformat()
    s1 = rec("S1", await post("s_op", S, {"month": "2026-10", "as_of": s_as_of}))
    gs = await c.get("/api/reports/monthly/2026-10/deck", headers={"Authorization": "Bearer " + keys["s_op"]})
    results["S1"]["deck_lines"] = _health_and_notes(gs.content)
    s2 = rec("S2", await post("s_op", S, {"month": "2026-10", "as_of": s_as_of}))
    results["S2"]["same_sha_as_S1"] = s2.json().get("sha256") == s1.json().get("sha256")
    gs2 = await c.get("/api/reports/monthly/2026-10/deck", headers={"Authorization": "Bearer " + keys["s_op"]})
    results["S2"]["deck_lines"] = _health_and_notes(gs2.content)

    rec("A11", await post("a_op", "monthly-report:example-outfitters:2026-10:refresh", {"month": "2026-10", "as_of": "2026-11-20"}))
    rd = await c.post("/api/reports/monthly/2026-10/delivered", headers={"Authorization": "Bearer " + keys["a_op"]})
    rec("A12", rd, marked_rows=_sql("select idempotency_key, delivered_at is not null from report_run where month='2026-10' and tenant_id=%s order by created_at",
                                    (ids["tenants"]["a"],)))
    rec("A13", await post("a_op", A, {"month": "2026-10", "as_of": "2026-11-02"}))
    await c.aclose()
    await stack.aclose()
    return results


@pytest.fixture(scope="module")
def run():
    """The endpoint scenario once for the whole module: {label: {status, body, ...}} and the ids it used."""
    os.environ["DATABASE_URL"] = DSN
    os.environ.setdefault("DASHBOARD_ORIGIN", "http://localhost:3000")
    for k in ("GROQ_API_KEY", "LLM_API_KEY"):
        os.environ.pop(k, None)
    schedule = _schedule_instant()
    keys, ids = _seed(schedule)
    return {"results": asyncio.run(_scenario(keys, ids, schedule)), "ids": ids, "schedule": schedule}


# ---------------------------------------------------------------- one `actual` per case, in the adversary's own words

_SHA = re.compile(r"sha256 [0-9a-f]{12}")


def mask(text: str) -> str:
    """A deck holds the time it was made, so its hash differs between runs; the rest of the line is compared."""
    return _SHA.sub("sha256 <hash>", text)


def short(r: dict) -> str:
    b = r["body"]
    if "status" in b and isinstance(b.get("slides"), int):
        return "%d %s, %d slides, sources %s, sha256 %s, delivered %s" % (r["status"], b["status"], b["slides"], ",".join(b["sources"]), b["sha256"][:12], b["delivered"])
    return "%d %s" % (r["status"], b.get("detail", b))


LABELS = {0: "A1", 1: "A2", 3: "A4", 4: "A5", 5: "A6", 6: "A8", 7: "A9", 8: "A9b", 9: "A10", 10: "B1", 11: "B2", 12: "D1", 13: "D2", 14: "N1", 15: "N2", 16: "E1"}


def endpoint_actual(index: int, R: dict) -> str:
    if index == 2:
        return "%d, sha256 matches build: %s" % (R["A3"]["status"], R["A3"]["sha_matches_build"])
    if index == 19:
        return "%s; delivered marked on %s; workflow key replay: %s" % (short(R["A11"]), [k for k, v in R["A12"]["marked_rows"] if v], short(R["A13"]))
    return short(R[LABELS[index]])


def test_the_corpus_is_the_one_that_was_written():
    assert len(CASES) == 40
    assert sum(1 for c in CASES if c["expect"] == "pass") == 22 and sum(1 for c in CASES if c["expect"] == "fail") == 18
    for n, c in enumerate(CASES):
        assert c["source"] in ("real", "invented"), n
        assert len(c["why"]) >= 10 and c["expected"] and c["promise"], n


def test_no_case_is_skipped_silently():
    assert NOT_REPLAYABLE == {}
    kinds = [c["input"]["kind"] for c in CASES]
    assert kinds[:20] == ["endpoint"] * 20 and kinds[20:24] == ["workflow_node"] * 4 and kinds[24:37] == ["model_text"] * 13 and kinds[37:] == ["snapshots"] * 3
    assert set(KNOWN_LIMITS) <= set(range(len(CASES))) and FIXED <= set(range(len(CASES)))


# ---------------------------------------------------------------- the endpoint cases 0 to 19

AGREE = [n for n in range(20) if n not in KNOWN_LIMITS and n not in FIXED]


@needs_postgres
@needs_node
@pytest.mark.parametrize("index", AGREE)
def test_the_endpoint_gives_the_outcome_the_corpus_expects(run, index):
    R, case = run["results"], CASES[index]
    got = endpoint_actual(index, R)
    why = "case %d (%s): %s | got %s" % (index, case["expect"], case["why"], got)
    if index == 0:
        assert got.startswith(case["expected"]), why
    elif index == 1:
        assert R["A2"]["status"] == 200 and R["A2"]["body"]["status"] == "replayed" and R["A2"]["body"]["sha256"] == R["A1"]["body"]["sha256"], why
        assert R["A2"]["body"]["delivered"] is False
    elif index == 2:
        assert R["A3"]["status"] == 200 and R["A3"]["sha_matches_build"] is True, why
    elif index == 3:
        assert R["A4"]["status"] == 403, why
    elif index == 4:
        assert R["A5"]["status"] == 422, why
    elif index == 5:
        assert R["A6"]["status"] == 400 and "8 to 128 letters, digits and : _ . @ -" in R["A6"]["body"]["detail"], why
    elif index == 6:
        assert R["A8"]["status"] == 400, why
    elif index == 9:
        assert R["A10"]["status"] == 422 and "no active paid media connection found with that id" in R["A10"]["body"]["detail"], why
    elif index == 10:
        assert R["B1"]["status"] == 404, why
    elif index == 11:
        b = R["B2"]["body"]
        assert R["B2"]["status"] == 200 and b["status"] == "created" and b["slides"] == 7 and b["sources"] == ["store"], why
        assert b["sha256"] != R["A1"]["body"]["sha256"]
    elif index == 12:
        assert R["D1"]["status"] == 422 and "name one" in R["D1"]["body"]["detail"], why
    elif index == 13:
        b = R["D2"]["body"]
        assert R["D2"]["status"] == 200 and b["status"] == "created" and b["slides"] == 8 and b["sources"] == ["store", "ads"], why
    elif index == 14:
        assert R["N1"]["status"] == 422 and "no active connection found for this tenant" in R["N1"]["body"]["detail"], why
    elif index == 15:
        b = R["N2"]["body"]
        assert R["N2"]["status"] == 200 and b["status"] == "created" and b["slides"] == 7, why
    elif index == 16:
        assert R["E1"]["status"] == 422 and "more than one currency in October 2026" in R["E1"]["body"]["detail"], why
    else:
        raise AssertionError("case %d has no check" % index)


@needs_postgres
@needs_node
@pytest.mark.parametrize("index", AGREE)
def test_the_recorded_actual_still_holds(run, index):
    assert mask(endpoint_actual(index, run["results"])) == mask(CASES[index]["actual"])


#: what each known limit would look like if it had moved: a 422 reason where the code gives a 500, and a delivered replay of the workflow's key
MOVED = {
    7: lambda R: R["A9"]["status"] != 500,
    8: lambda R: R["A9b"]["status"] != 500,
    19: lambda R: R["A13"]["body"].get("delivered") is True,
}


@needs_postgres
@needs_node
@pytest.mark.parametrize("index", sorted(n for n in KNOWN_LIMITS if n < 20))
def test_a_known_limit_is_pinned_to_what_the_code_does_today(run, index):
    assert mask(endpoint_actual(index, run["results"])) == mask(CASES[index]["actual"]), KNOWN_LIMITS[index]
    assert not MOVED[index](run["results"]), "the limit moved: remove case %d from KNOWN_LIMITS" % index


@needs_postgres
@needs_node
def test_case_17_the_deck_the_schedule_builds_counts_the_month_in_full_for_ads_and_search(run):
    """F1 fixed: ads and search were synced at the edge of the settled window, one hour before the committed schedule, and the deck holds 31 October."""
    s1 = run["results"]["S1"]
    assert run["ids"]["settled_end"] == "2026-11-01"
    assert s1["status"] == 200 and s1["body"]["status"] == "created" and s1["body"]["sources"] == ["store", "ads", "search"] and s1["body"]["slides"] == 9
    lines = s1["deck_lines"]
    # 31 October is stored, so no source lists it as a day with no data; the newest days are still marked as ones that may change, which is true
    assert "Data health: every day of October is covered in all 3 sources" in lines and "missing" not in lines, lines
    for source in ("store", "ads", "search"):
        assert "Days with no data in October, %s: none." % source in lines
    assert "Days with no data in October, ads: Oct 31." in CASES[17]["actual"]


@needs_postgres
@needs_node
def test_case_18_asking_again_replays_the_same_deck_and_that_deck_has_no_missing_day(run):
    s2 = run["results"]["S2"]
    assert s2["status"] == 200 and s2["body"]["status"] == "replayed" and s2["same_sha_as_S1"] is True
    assert "missing" not in s2["deck_lines"] and s2["deck_lines"] == run["results"]["S1"]["deck_lines"]


# ---------------------------------------------------------------- the workflow cases 20 to 23

TENANTS_20_TO_23 = [CASES[n]["input"]["item"] for n in range(20, 24)]


@needs_node
@pytest.mark.parametrize("index", [20, 21, 23])
def test_the_workflow_builds_a_key_the_hub_accepts_for_any_tenant_name(index):
    from app.report.runs import KEY_RE
    item = CASES[index]["input"]["item"]
    (got,) = workflow_requests([item])
    text = "key %s (%d chars, valid %s), body %s" % (got["key"], len(got["key"]), bool(KEY_RE.match(got["key"])), got["body"])
    assert KEY_RE.match(got["key"]), CASES[index]["why"]
    assert text == CASES[index]["actual"]


@needs_node
def test_case_22_the_body_names_the_ad_account_the_tenant_entry_carries():
    """F4 fixed: the entry's paid_connection_id reaches the request body."""
    item = CASES[22]["input"]["item"]
    (got,) = workflow_requests([item])
    assert json.loads(got["body"]) == {"month": "2026-10", "paid_connection_id": "<main ad connection uuid>"}
    assert 'body {"month":"2026-10"}, so the hub answers as D1' in CASES[22]["actual"]


@needs_postgres
@needs_node
def test_case_22_the_hub_builds_the_deck_from_that_body(run):
    d2 = run["results"]["D2"]
    assert json.loads(d2["body_sent"])["paid_connection_id"] == str(run["ids"]["d_ads1"])
    assert d2["status"] == 200 and d2["body"]["sources"] == ["store", "ads"], d2["body"]


# ---------------------------------------------------------------- the model text cases 24 to 36

def _facts():
    from app.report.insights import report_facts
    from app.report.monthly import build_report
    from app.report.paid import attach_paid
    from app.report.search import attach_search

    full = [(date(2026, 9, 1), date(2026, 11, 2))]
    r = build_report(store_rows(), 2026, 10, label="Example Outfitters", covered=full, as_of=date(2026, 11, 2))
    attach_paid(r, ads_rows(date(2026, 10, 31)), label="ads", covered=full, as_of=date(2026, 11, 2))
    attach_search(r, search_rows(date(2026, 10, 31)), [], label="search", covered=full, as_of=date(2026, 11, 2))
    return report_facts(r)


@pytest.mark.parametrize("index", range(24, 37))
def test_the_figure_check_keeps_what_the_corpus_says_to_keep_and_refuses_the_rest(index):
    from app.report.insights import check_model_text
    case = CASES[index]
    lines, reason = check_model_text(case["input"]["text"], _facts())
    got = "kept" if reason is None else "template fallback"
    assert got == case["expected"] == case["actual"], "case %d (%s): %s" % (index, case["expect"], case["why"])


# ---------------------------------------------------------------- the snapshot cases 37 to 39


def _snapshot_rows(name: str) -> list[str]:
    from app.report.deck import dump_deck, save_deck
    from app.report.insights import template_insights
    from app.report.monthly import build_report
    from app.report.paid import attach_paid
    from app.report.search import attach_search

    full = [(date(2026, 9, 1), date(2026, 11, 2))]
    rows, cov = {
        "paused_all_october": ([r for r in ads_rows(date(2026, 10, 31)) if r["observed_on"].month == 9], full),
        "connected_oct_15": ([r for r in ads_rows(date(2026, 10, 31)) if r["observed_on"] >= date(2026, 10, 15)], [(date(2026, 10, 15), date(2026, 11, 2))]),
    }[name]
    rep = build_report(store_rows(), 2026, 10, label="Store", covered=full, as_of=date(2026, 11, 2))
    attach_paid(rep, rows, label="ads", covered=cov, as_of=date(2026, 11, 2))
    attach_search(rep, search_rows(date(2026, 10, 31)), [], label="search", covered=full, as_of=date(2026, 11, 2))
    ins = template_insights(rep)
    with tempfile.TemporaryDirectory() as t:
        text = dump_deck(save_deck(rep, ins, Path(t) / "d.pptx"))
    return [ln for ln in text.splitlines() if ln.startswith(("Ad spend |", "Return on ad spend |", "--- slide 6"))]


@pytest.mark.parametrize("index, name", [(37, "paused_all_october"), (38, "connected_oct_15")])
def test_an_ad_account_paused_or_connected_mid_month_is_stated_as_it_is(index, name):
    got = "; ".join(_snapshot_rows(name)[:2])
    case = CASES[index]
    assert got == case["actual"]
    if index == 37:
        assert "EUR 0.00" in got and "-100.0%" in got and "not reported" in got
    else:
        assert "no earlier month stored; October: 14 days missing" in got


# ---------------------------------------------------------------- case 39: 300 seeded ordinary months, store plus ads plus search, every template written and deck saved


def _fuzz_scenario(rng: random.Random):
    as_of = rng.choice([date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 15), date(2026, 10, 20)])
    store_rows_, store_cov = [], []
    s_hole = set(rng.sample(range(1, 29), rng.choice([0, 0, 1, 2])))
    for m in (9, 10):
        for d in days(2026, m):
            if m == 10 and d.day in s_hole:
                continue
            o = rng.randint(5, 40)
            store_rows_ += day_rows(d, o, o * rng.randint(1500, 6000), rng.choice([0, 0, 1200]), max(o - 2, 1))
    store_cov = [(date(2026, 9, 1), date(2026, 11, 2))] if not s_hole else []
    if s_hole:
        cur = date(2026, 9, 1)
        for d in sorted(s_hole):
            store_cov.append((cur, date(2026, 10, d)))
            cur = date(2026, 10, d + 1)
        store_cov.append((cur, date(2026, 11, 2)))
    cur_ccy = rng.choice(["EUR", "USD", "JPY", "GBP"])
    paid_kind = rng.choice(["full", "paused_oct", "paused_sep", "started_mid_oct", "no_purchases", "zero_clicks", "hole", "low_spend"])
    paid_rows, paid_cov = [], [(date(2026, 9, 1), date(2026, 11, 2))]
    for m in (9, 10):
        for d in days(2026, m):
            if paid_kind == "paused_oct" and m == 10:
                continue
            if paid_kind == "paused_sep" and m == 9:
                continue
            if paid_kind == "started_mid_oct" and (m == 9 or d.day < 15):
                continue
            if paid_kind == "hole" and m == 10 and d.day in (20, 21):
                continue
            spend = rng.randint(100, 30000) if paid_kind != "low_spend" else rng.randint(0, 50)
            imp = rng.randint(200, 20000)
            clicks = 0 if paid_kind == "zero_clicks" else rng.randint(0, imp // 20)
            if paid_kind == "no_purchases" or rng.random() < 0.05:
                paid_rows += paid_day(d, spend, imp, clicks, currency=cur_ccy)
            else:
                pur = rng.randint(0, 10)
                paid_rows += paid_day(d, spend, imp, clicks, pur, pur * rng.randint(1000, 9000), currency=cur_ccy)
    if paid_kind == "started_mid_oct":
        paid_cov = [(date(2026, 10, 15), date(2026, 11, 2))]
    if paid_kind == "hole":
        paid_cov = [(date(2026, 9, 1), date(2026, 10, 20)), (date(2026, 10, 22), date(2026, 11, 2))]
    search_kind = rng.choice(["full", "new_oct", "quiet_oct", "hole", "zero_impr"])
    search_rows_, search_cov, tops = [], [(date(2026, 9, 1), date(2026, 11, 2))], []
    for m in (9, 10):
        for d in days(2026, m):
            if search_kind == "new_oct" and m == 9:
                continue
            if search_kind == "quiet_oct" and m == 10:
                continue
            if search_kind == "hole" and m == 10 and d.day in (9, 10):
                continue
            imp = 0 if search_kind == "zero_impr" else rng.randint(50, 5000)
            clicks = rng.randint(0, max(imp // 10, 0))
            search_rows_ += search_day(d, clicks, imp, rng.randint(1000, 40000))
            if m == 10 and imp:
                tops.append(top(d, "query", rng.choice(["shoes", "boots", "socks", "a very long query about rechargeable headlamps"]), clicks // 2, imp // 2, 5000))
                tops.append(top(d, "page", rng.choice(["/", "/a", "/b"]), clicks // 2, imp // 2, 6000))
    if search_kind == "new_oct":
        search_cov = [(date(2026, 10, 1), date(2026, 11, 2))]
    if search_kind == "hole":
        search_cov = [(date(2026, 9, 1), date(2026, 10, 9)), (date(2026, 10, 11), date(2026, 11, 2))]
    return dict(as_of=as_of, store_rows=store_rows_, store_cov=store_cov, paid_rows=paid_rows, paid_cov=paid_cov, search_rows=search_rows_,
                search_cov=search_cov, tops=tops)


def test_case_39_three_hundred_ordinary_months_build_a_deck_each(tmp_path):
    from app.report.deck import save_deck
    from app.report.insights import template_insights
    from app.report.monthly import build_report
    from app.report.paid import attach_paid
    from app.report.search import attach_search

    failures = []
    for seed in range(300):
        sc = _fuzz_scenario(random.Random(seed))
        try:
            report = build_report(sc["store_rows"], 2026, 10, label="Store", covered=sc["store_cov"], as_of=sc["as_of"])
            attach_paid(report, sc["paid_rows"], label="Ads", covered=sc["paid_cov"], as_of=sc["as_of"])
            attach_search(report, sc["search_rows"], sc["tops"], label="Search", covered=sc["search_cov"], as_of=sc["as_of"])
            save_deck(report, template_insights(report), tmp_path / "d.pptx")
        except Exception as exc:  # noqa: BLE001
            failures.append((seed, type(exc).__name__, str(exc)[:120]))
    assert failures == [], failures[:3]
    assert CASES[39]["actual"] == "300 runs, 0 failures (fuzz_deck_300.txt)"
