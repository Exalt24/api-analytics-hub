"""Replay of the second blind corpus for the ads, search and monthly run work (output round 2).

tests/corpus/hub_all_corpus_blind_2.jsonl holds 43 cases (20 must-pass and 23 must-fail) written by a reviewer on a different model family who
read the finished build at 71cffbb and ran every case on a git archive of it. Each carries `promise` (a quote from the spec), `why` (who is harmed
on an ordinary day), `source` (real: the vendor's documented shape; invented: every tenant, key and row is made up), `expected` and `command`, whose
last word is the case's id in the reviewer's harness. The reviewer also recorded `actual`, what 71cffbb did.

Every case is replayed here against the real code: the connectors through httpx stand-ins that answer with the vendors' documented shapes, the sync
scripts' own `main`, the endpoint in process against Postgres with real row level security (the scratch database the DSN names), and the workflow's
own expressions evaluated in node. NOT_REPLAYABLE is empty on purpose. The database cases are skipped, never failed, when Postgres is missing and
the workflow cases when node is missing; both run in CI. The cases run once, in the order the reviewer ran them, because some build on the rows or
marks of the one before.

Cases the code now answers differently from the recorded `actual` are the fixed findings: F1 (case 34, a return on ad spend of 3.00 on a month whose
spend and purchase value give 1.50), F2 (case 35, a default window that never stored 1 October), F3 (cases 36 and 37, a sync wrote the account the
environment named into another client's connection). KNOWN_LIMITS is the rejected finding, pinned to the recorded `actual`: F4 (case 38, one client
listed twice in HUB_TENANTS_JSON with the same key, the same class as the accepted delivered mark limit of round 1).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
import uuid
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

CORPUS = ROOT / "tests" / "corpus" / "hub_all_corpus_blind_2.jsonl"
CASES = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]
IDS = [c["command"].split()[-1] for c in CASES]

#: case number (1 based, the corpus order) -> why it is not replayed offline. Empty: all 43 are replayed.
NOT_REPLAYABLE: dict[int, str] = {}

#: case number -> why the code is deliberately left as it is (the lead's decision, scored 1x3 or less). The live outcome is pinned to the recorded `actual`.
KNOWN_LIMITS = {
    38: "F4 KNOWN_LIMIT 1x3: a client listed twice in HUB_TENANTS_JSON with the same key and a different paid_connection_id gets the latest deck of the month "
        "under both entries, needs a workflow entry that lists one client twice, the same class as the delivered mark limit of round 1 (case 19 there)",
}

#: the cases the old code failed and this change fixed; each is checked by its own predicate below
FIXED = {"F1", "F2", "F3", "F3b"}

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@localhost:5433/analytics")
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

MIGRATIONS = sorted(p.name for p in (ROOT / "migrations").glob("*.sql"))


# ---------------------------------------------------------------- the reviewer's harness (hb.py), reproduced against the repository's own code


def days(start: date, end: date):
    d = start
    while d < end:
        yield d
        d += timedelta(days=1)


def meta_handler(account_rows: dict, page_size: int = 10, log: list | None = None):
    """account id -> insight rows already in the Graph JSON shape. Pages by an integer cursor and names a `next` URL the connector must not follow."""
    import httpx

    def handle(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(str(request.url))
        path = request.url.path
        qs = parse_qs(request.url.query.decode())
        acct = path.split("/act_")[1].split("/")[0]
        if not path.endswith("/insights"):
            return httpx.Response(200, json={"account_status": 1, "currency": "EUR", "timezone_name": "Europe/Dublin", "id": "act_" + acct})
        rng = json.loads(qs["time_range"][0])
        since, until = date.fromisoformat(rng["since"]), date.fromisoformat(rng["until"])
        rows = [r for r in account_rows.get(acct, []) if since <= date.fromisoformat(r["date_start"]) <= until]
        off = int(qs.get("after", ["0"])[0].replace("c", "") or 0)
        page = rows[off: off + page_size]
        body = {"data": page}
        if off + page_size < len(rows):
            nxt = "c%d" % (off + page_size)
            body["paging"] = {"cursors": {"before": "c%d" % off, "after": nxt},
                              "next": "https://graph.facebook.com/v26.0/act_%s/insights?access_token=SECRET&after=%s" % (acct, nxt)}
        else:
            body["paging"] = {"cursors": {"before": "c%d" % off, "after": "c%d" % (off + len(page))}}
        return httpx.Response(200, json=body)
    return handle


def meta_row(day: date, spend: str, impressions: int, clicks: int, currency="EUR", purchases=None, value=None, actions_without_purchase=False):
    """A Graph insights row as the Ads Insights reference shows it: numbers as numeric strings, actions per window."""
    row = {"account_currency": currency, "spend": spend, "impressions": str(impressions), "clicks": str(clicks),
           "date_start": day.isoformat(), "date_stop": day.isoformat()}
    acts = [{"action_type": "link_click", "value": str(max(clicks - 3, 0)), "7d_click": str(max(clicks - 3, 0))},
            {"action_type": "page_engagement", "value": str(clicks), "7d_click": str(clicks)}]
    if purchases is not None:
        acts.append({"action_type": "omni_purchase", "value": str(purchases + 1), "7d_click": str(purchases)})
    if purchases is not None or actions_without_purchase:
        row["actions"] = acts
    if value is not None:
        row["action_values"] = [{"action_type": "omni_purchase", "value": value, "7d_click": value}]
    return row


def meta_connector(handler, account="1001", **kw):
    import httpx
    from app.connectors.meta import MetaConnector

    async def no_sleep(_):
        return None
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kw.setdefault("sleep", no_sleep)
    return MetaConnector(ad_account_id=account, access_token="tok-blind", client=client, **kw)


def gsc_handler(site_days: dict, top: dict | None = None, first_incomplete: str | None = None, log: list | None = None):
    import httpx
    top = top or {}

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if log is not None:
            log.append((str(request.url), body))
        s, e = date.fromisoformat(body["startDate"]), date.fromisoformat(body["endDate"])
        dims = body["dimensions"]
        if dims == ["date"]:
            rows = [{"keys": [d.isoformat()], **v} for d, v in sorted(site_days.items()) if s <= d <= e]
            rows = rows[body.get("startRow", 0): body.get("startRow", 0) + body["rowLimit"]]
            out = {"responseAggregationType": "byProperty"}
            if rows:
                out["rows"] = rows
            if first_incomplete:
                out["metadata"] = {"first_incomplete_date": first_incomplete}
            return httpx.Response(200, json=out)
        rows = top.get((s, dims[0]), [])[: body["rowLimit"]]
        return httpx.Response(200, json={"rows": rows, "responseAggregationType": "byPage" if dims[0] == "page" else "byProperty"} if rows else {})
    return handle


def gsc_connector(handler, site="sc-domain:example-blind.com", **kw):
    import httpx
    from app.connectors.gsc import GSCConnector

    async def no_sleep(_):
        return None
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kw.setdefault("sleep", no_sleep)
    return GSCConnector(site_url=site, access_token="tok-blind", client=client, **kw)


def err_handler(status, body):
    import httpx
    return lambda request: httpx.Response(status, json=body)


def collect(connector, start, end):
    async def go():
        pts, rows, err = [], [], None
        try:
            async for page in connector.fetch(window_start=start, window_end=end):
                pts += page.points
                rows += page.rows
        except Exception as exc:  # noqa: BLE001
            err = "%s: %s" % (type(exc).__name__, exc)
        await connector.aclose()
        return pts, rows, err
    return asyncio.run(go())


def run_script(mod, argv, env):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    o, e = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            code = mod.main(argv)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    return code, (o.getvalue() + e.getvalue()).strip()


def sql(query, params=(), tenant=None):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        if tenant:
            cur.execute("select set_config('app.tenant_id', %s, false)", (str(tenant),))
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


def reset():
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade; create schema public")
        for name in MIGRATIONS:
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))
        cur.execute("grant usage on schema public to sync_worker")


def tenant(name: str, role="operator") -> tuple[str, str]:
    from app.api.auth import generate_key, hash_key
    tid = str(uuid.uuid4())
    key = generate_key()
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select set_config('app.tenant_id', %s, false)", (tid,))
        cur.execute("insert into tenant (id, slug, name) values (%s, %s, %s)", (tid, name.lower().replace(" ", "-")[:40] + tid[:6], name))
        cur.execute("select set_config('app.tenant_id', '', false)")
        cur.execute("insert into api_key (tenant_id, key_hash, label, role) values (%s, %s, %s, %s)", (tid, hash_key(key), name, role))
    return tid, key


def add_key(tid, role):
    from app.api.auth import generate_key, hash_key
    key = generate_key()
    sql("insert into api_key (tenant_id, key_hash, label, role) values (%s, %s, %s, %s)", (tid, hash_key(key), role, role))
    return key


def connection(tid, platform, account, name, status="active"):
    return str(sql("insert into platform_connection (tenant_id, platform, external_account, display_name, credentials_enc, status) "
                   "values (%s, %s, %s, %s, %s, %s) returning id", (tid, platform, account, name, b"x", status), tenant=tid)[0][0])


def store(tid, name, currency="USD", start=date(2026, 9, 1), end=date(2026, 11, 1), orders=20, aov=5000):
    cid = connection(tid, "shopify", name + ".myshopify.com", name + " store")
    payload = []
    for i, d in enumerate(days(start, end)):
        n = orders + (i % 5)
        rows = [
            {"metric_key": "orders", "value_numeric": n, "currency": None},
            {"metric_key": "gross_sales", "value_numeric": n * aov, "currency": currency},
            {"metric_key": "refunds", "value_numeric": 0, "currency": currency},
            {"metric_key": "net_sales", "value_numeric": n * aov, "currency": currency},
            {"metric_key": "customers", "value_numeric": n - 1, "currency": None},
        ]
        for p in rows:
            p["observed_on"] = d.isoformat()
        payload += rows
    for p in payload:
        p.update({"tenant_id": tid, "connection_id": cid, "raw": {}})
    sql("select upsert_snapshots(%s::jsonb)", (json.dumps(payload),), tenant=tid)
    sql("insert into sync_run (tenant_id, connection_id, trigger, window_start, window_end, status, finished_at) "
        "values (%s, %s, 'schedule', %s, %s, 'succeeded', now())", (tid, cid, start, end), tenant=tid)
    return cid


def sync(tid, cid, connector, start, end):
    from app.db import Database
    from app.sync.runner import SyncRunner

    async def go():
        db = Database(DSN, max_size=2, role="sync_worker")
        await db.connect()
        try:
            return await SyncRunner(db).run_once(tenant_id=tid, connection_id=cid, connector=connector,
                                                 window_start=start, window_end=end, trigger="manual")
        finally:
            await db.close()
            await connector.aclose()
    return asyncio.run(go())


class Api:
    """The API through ASGI, in process, with its lifespan."""

    def __init__(self):
        import httpx
        from contextlib import AsyncExitStack
        from app.api.main import app
        self.loop = asyncio.new_event_loop()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub", timeout=120)
        self.stack = AsyncExitStack()
        self.loop.run_until_complete(self.stack.enter_async_context(app.router.lifespan_context(app)))

    def call(self, method, url, key=None, headers=None, **kw):
        h = dict(headers or {})
        if key:
            h["Authorization"] = "Bearer " + key
        return self.loop.run_until_complete(getattr(self.client, method)(url, headers=h, **kw))

    def build(self, key, idem, month="2026-10", **body):
        return self.call("post", "/api/reports/monthly", key, headers={"Idempotency-Key": idem}, json={"month": month, **body})

    def close(self):
        self.loop.run_until_complete(self.stack.aclose())
        self.loop.run_until_complete(self.client.aclose())
        self.loop.close()


def dump(deck_bytes: bytes) -> str:
    from pptx import Presentation
    from app.report.deck import dump_deck
    return dump_deck(Presentation(io.BytesIO(deck_bytes)))


# ---------------------------------------------------------------- the cases, in the order the reviewer ran them (cases.py), each returning its `actual`

REPLAY: dict = {}
STATE: dict = {}


def case(cid):
    def wrap(fn):
        REPLAY[cid] = fn
        return fn
    return wrap


@case("M1")
def m1():
    row = meta_row(date(2026, 10, 3), "123.45", 5000, 120, purchases=7, value="412.30")
    pts, _, err = collect(meta_connector(meta_handler({"1001": [row]})), date(2026, 10, 3), date(2026, 10, 4))
    return err or sorted((p.metric_key, p.value_numeric, p.currency) for p in pts)


@case("M2")
def m2():
    row = meta_row(date(2026, 10, 3), "15000", 9000, 300, currency="JPY", purchases=4, value="48000")
    pts, _, err = collect(meta_connector(meta_handler({"1001": [row]})), date(2026, 10, 3), date(2026, 10, 4))
    return err or sorted((p.metric_key, p.value_numeric, p.currency) for p in pts if p.currency)


@case("M3")
def m3():
    row = meta_row(date(2026, 10, 3), "50.00", 4000, 90)
    pts, _, err = collect(meta_connector(meta_handler({"1001": [row]})), date(2026, 10, 3), date(2026, 10, 4))
    return err or sorted(p.metric_key for p in pts)


@case("M4")
def m4():
    log = []
    rows = [meta_row(d, "10.00", 100, 5, purchases=1, value="20.00") for d in days(date(2026, 10, 1), date(2026, 11, 1))]
    pts, _, err = collect(meta_connector(meta_handler({"1001": rows}, page_size=10, log=log)), date(2026, 10, 1), date(2026, 11, 1))
    return err or {"days": len({p.observed_on for p in pts}), "requests": len(log), "any_request_carried_SECRET": any("SECRET" in u for u in log)}


@case("M5")
def m5():
    body = {"error": {"message": "Error validating access token: Session has expired", "type": "OAuthException", "code": 190, "error_subcode": 463, "fbtrace_id": "A1"}}
    return collect(meta_connector(err_handler(400, body)), date(2026, 10, 1), date(2026, 10, 8))[2]


@case("M6")
def m6():
    return collect(meta_connector(err_handler(200, {"error": {"message": "(#200) Requires ads_read permission", "code": 200}})), date(2026, 10, 1), date(2026, 10, 8))[2]


@case("M7")
def m7():
    return collect(meta_connector(err_handler(400, {"error": {"message": "User request limit reached", "code": 17}})), date(2026, 10, 1), date(2026, 10, 8))[2]


@case("M8")
def m8():
    row = meta_row(date(2026, 10, 3), "50.00", 4000, 90)
    row.pop("account_currency")
    return collect(meta_connector(meta_handler({"1001": [row]})), date(2026, 10, 3), date(2026, 10, 4))[2]


@case("M9")
def m9():
    rows = [meta_row(date(2026, 10, 3), "50.00", 4000, 90), meta_row(date(2026, 10, 4), "50.00", 4000, 90, currency="USD")]
    return collect(meta_connector(meta_handler({"1001": rows})), date(2026, 10, 3), date(2026, 10, 5))[2]


@case("M10")
def m10():
    import httpx

    def h(request):
        row = meta_row(date(2026, 10, 3), "50.00", 4000, 90)
        return httpx.Response(200, json={"data": [row] if "after" not in str(request.url) else [],
                                         "paging": {"cursors": {"after": "SAME"}, "next": "https://graph.facebook.com/x?after=SAME"}})
    return collect(meta_connector(h), date(2026, 10, 3), date(2026, 10, 5))[2]


@case("G1")
def g1():
    sd = {d: {"clicks": 40, "impressions": 1200, "ctr": 0.033, "position": 6.4} for d in days(date(2026, 10, 25), date(2026, 11, 1))}
    pts, _, err = collect(gsc_connector(gsc_handler(sd, first_incomplete="2026-10-30"), top_n=0), date(2026, 10, 25), date(2026, 11, 1))
    return err or sorted({(p.observed_on.isoformat(), bool((p.raw or {}).get("provisional"))) for p in pts})


@case("G2")
def g2():
    sd = {date(2026, 10, 5): {"clicks": 12.0, "impressions": "340", "ctr": 0.035, "position": 8.25}}
    pts, _, err = collect(gsc_connector(gsc_handler(sd), top_n=0), date(2026, 10, 5), date(2026, 10, 6))
    return err or sorted((p.metric_key, p.value_numeric) for p in pts)


@case("G3")
def g3():
    log = []
    sd = {date(2026, 10, 5): {"clicks": 1, "impressions": 10, "ctr": 0.1, "position": 3.0}}
    err = collect(gsc_connector(gsc_handler(sd, log=log), site="sc-domain:bayside-dental.example", top_n=0), date(2026, 10, 5), date(2026, 10, 6))[2]
    return err or log[0][0].split("/sites/")[1].split("/")[0]


@case("G4")
def g4():
    sd = {date(2026, 10, 5): {"clicks": 50, "impressions": 40, "ctr": 1.25, "position": 3.0}}
    return collect(gsc_connector(gsc_handler(sd), top_n=0), date(2026, 10, 5), date(2026, 10, 6))[2]


@case("G5")
def g5():
    body = {"error": {"code": 403, "message": "Quota exceeded for quota metric", "errors": [{"domain": "usageLimits", "reason": "quotaExceeded", "message": "Quota exceeded"}]}}
    return collect(gsc_connector(err_handler(403, body), top_n=0), date(2026, 10, 5), date(2026, 10, 6))[2]


@case("G6")
def g6():
    body = {"error": {"code": 403, "message": "User does not have sufficient permission for site", "errors": [{"domain": "global", "reason": "forbidden", "message": "x"}]}}
    return collect(gsc_connector(err_handler(403, body), top_n=0), date(2026, 10, 5), date(2026, 10, 6))[2]


@case("G7")
def g7():
    sd = {date(2026, 10, 5): {"clicks": 3.5, "impressions": 40, "ctr": 0.1, "position": 3.0}}
    return collect(gsc_connector(gsc_handler(sd), top_n=0), date(2026, 10, 5), date(2026, 10, 6))[2]


@case("G8")
def g8():
    pts, _, err = collect(gsc_connector(gsc_handler({}), top_n=0), date(2026, 10, 5), date(2026, 10, 6))
    return err or {"points": len(pts)}


def world():
    """Harbor (store, EUR ads, search), Bayside (store only), Northwind (store and two ad accounts)."""
    reset()
    w = {}
    w["a"], w["ka"] = tenant("Harbor Coffee Roasters")
    store(w["a"], "harbor")
    w["a_ads"] = connection(w["a"], "meta", "act_3001", "Harbor ads")
    rows = [meta_row(d, "80.00", 4000, 100, purchases=3, value="210.00") for d in days(date(2026, 9, 1), date(2026, 11, 1))]
    sync(w["a"], w["a_ads"], meta_connector(meta_handler({"3001": rows}), account="3001"), date(2026, 9, 1), date(2026, 11, 1))
    w["a_gsc"] = connection(w["a"], "gsc", "sc-domain:harbor.example", "Harbor search")
    sd = {d: {"clicks": 100, "impressions": 3000, "ctr": 0.03, "position": 9.0} for d in days(date(2026, 9, 1), date(2026, 11, 1))}
    sync(w["a"], w["a_gsc"], gsc_connector(gsc_handler(sd), site="sc-domain:harbor.example", top_n=0), date(2026, 9, 1), date(2026, 11, 1))
    w["kav"] = add_key(w["a"], "viewer")
    w["b"], w["kb"] = tenant("Bayside Dental")
    store(w["b"], "bayside")
    w["d"], w["kd"] = tenant("Northwind Outdoor")
    store(w["d"], "northwind")
    w["d_ads1"] = connection(w["d"], "meta", "act_4001", "Northwind ads EU")
    w["d_ads2"] = connection(w["d"], "meta", "act_4002", "Northwind ads UK")
    return w


def api():
    if "api" not in STATE:
        os.environ["DATABASE_URL"] = DSN
        os.environ.setdefault("DASHBOARD_ORIGIN", "http://localhost:3100")
        for k in ("GROQ_API_KEY", "LLM_API_KEY"):
            os.environ.pop(k, None)
        STATE["api"] = Api()
    return STATE["api"]


def need_world():
    if "w" not in STATE:
        STATE["w"] = world()
    return STATE["w"]


@case("A1")
def a1():
    w = need_world()
    j = api().build(w["ka"], "monthly-report:harbor-coffee-roasters:2026-10", as_of="2026-11-01")
    body = j.json()
    STATE["a_sha"] = body.get("sha256")
    return {"status": j.status_code, "state": body.get("status"), "slides": body.get("slides"), "sources": body.get("sources")}


@case("A2")
def a2():
    w = need_world()
    r = api().build(w["ka"], "monthly-report:harbor-coffee-roasters:2026-10", as_of="2026-11-01")
    n = sql("select count(*) from report_run where idempotency_key = 'monthly-report:harbor-coffee-roasters:2026-10'")[0][0]
    return {"status": r.status_code, "state": r.json().get("status"), "same_sha": r.json().get("sha256") == STATE.get("a_sha"), "rows": n}


@case("A3")
def a3():
    w = need_world()
    r = api().build(w["kav"], "monthly-report:viewer-try:2026-10", as_of="2026-11-01")
    return {"status": r.status_code, "rows": sql("select count(*) from report_run where idempotency_key = 'monthly-report:viewer-try:2026-10'")[0][0]}


@case("A4")
def a4():
    w = need_world()
    r = api().build(w["ka"], "monthly-report:harbor-coffee-roasters:2026-10", month="2026-09", as_of="2026-11-01")
    return {"status": r.status_code, "detail": r.json().get("detail")}


@case("A5")
def a5():
    w = need_world()
    rb = api().call("get", "/api/reports/monthly/2026-10/deck", w["kb"])
    ra = api().call("get", "/api/reports/monthly/2026-10/deck", w["ka"])
    return {"bayside": rb.status_code, "harbor": ra.status_code}


@case("A6")
def a6():
    w = need_world()
    r = api().build(w["kd"], "monthly-report:northwind-outdoor:2026-10", as_of="2026-11-01")
    return {"status": r.status_code, "detail": r.json().get("detail")}


@case("A7")
def a7():
    w = need_world()
    r = api().build(w["kd"], "monthly-report:northwind-outdoor:2026-10", as_of="2026-11-01", paid_connection_id=w["d_ads1"])
    return {"status": r.status_code, "state": r.json().get("status"), "sources": r.json().get("sources")}


@case("A8")
def a8():
    w = need_world()
    r = api().build(w["kb"], "monthly-report:bayside-dental:2026-10", as_of="2026-11-01", paid_connection_id=w["a_ads"])
    return {"status": r.status_code, "detail": r.json().get("detail")}


@case("A9")
def a9():
    tid, key = tenant("Ads Only Agency Client")
    cid = connection(tid, "meta", "act_5001", "ads only")
    sync(tid, cid, meta_connector(meta_handler({"5001": [meta_row(date(2026, 10, 2), "5.00", 10, 1)]}), account="5001"), date(2026, 10, 1), date(2026, 11, 1))
    r = api().build(key, "monthly-report:ads-only-agency-client:2026-10", as_of="2026-11-01")
    return {"status": r.status_code, "detail": r.json().get("detail")}


@case("A10")
def a10():
    tid, key = tenant("Currency Switch Client")
    store(tid, "currency-switch")
    cid = connection(tid, "meta", "act_6001", "switch ads")
    sync(tid, cid, meta_connector(meta_handler({"6001": [meta_row(d, "5.00", 10, 1) for d in days(date(2026, 9, 1), date(2026, 10, 1))]}), account="6001"),
         date(2026, 9, 1), date(2026, 10, 1))
    sync(tid, cid, meta_connector(meta_handler({"6001": [meta_row(d, "5.00", 10, 1, currency="USD") for d in days(date(2026, 10, 1), date(2026, 11, 1))]}),
                                  account="6001"), date(2026, 10, 1), date(2026, 11, 1))
    r = api().build(key, "monthly-report:currency-switch-client:2026-10", as_of="2026-11-01")
    return {"status": r.status_code, "detail": r.json().get("detail")}


@case("A11")
def a11():
    w = need_world()
    m1_ = api().call("post", "/api/reports/monthly/2026-10/delivered", w["ka"]).json()
    rep = api().build(w["ka"], "monthly-report:harbor-coffee-roasters:2026-10", as_of="2026-11-01").json()
    m2_ = api().call("post", "/api/reports/monthly/2026-10/delivered", w["ka"]).json()
    return {"first": m1_.get("already_delivered"), "replay_delivered": rep.get("delivered"), "second": m2_.get("already_delivered")}


@case("A12")
def a12():
    w = need_world()
    r = api().build(w["ka"], "monthly report Harbor Oct", as_of="2026-11-01")
    return {"status": r.status_code, "detail": r.json().get("detail")}


@case("A13")
def a13():
    w = need_world()
    r = api().call("get", "/api/reports/monthly/2026-10/deck", w["ka"])
    return {"status": r.status_code, "body_sha_matches_header": hashlib.sha256(r.content).hexdigest() == r.headers.get("X-Report-Sha256"),
            "matches_build": r.headers.get("X-Report-Sha256") == STATE.get("a_sha")}


@case("D1")
def d1():
    w = need_world()
    text = dump(api().call("get", "/api/reports/monthly/2026-10/deck", w["ka"]).content)
    lines = [ln for ln in text.splitlines() if ln.startswith(("Ad spend |", "Cost per click |", "Purchase value |"))]
    return {"lines": lines, "any_dollar": any("$" in ln for ln in lines)}


@case("D2")
def d2():
    w = need_world()
    text = dump(api().call("get", "/api/reports/monthly/2026-10/deck", w["ka"]).content)
    return [ln for ln in text.splitlines() if ln.startswith("May still change")]


@case("F1")
def f1():
    tid, key = tenant("Roas Check Client")
    store(tid, "roas-check")
    cid = connection(tid, "meta", "act_7001", "roas ads")
    rows = []
    for m in (9, 10):
        for i in range(20):
            d = date(2026, m, i + 1)
            rows.append(meta_row(d, "100.00", 5000, 100, purchases=3, value="300.00") if i % 2 == 0
                        else meta_row(d, "100.00", 5000, 100, actions_without_purchase=True))
    run = sync(tid, cid, meta_connector(meta_handler({"7001": rows}), account="7001"), date(2026, 9, 1), date(2026, 11, 1))
    r = api().build(key, "monthly-report:roas-check-client:2026-10", as_of="2026-11-15")
    text = dump(api().call("get", "/api/reports/monthly/2026-10/deck", key).content)
    want = ("Ad spend |", "Purchases |", "Purchase value |", "Return on ad spend |")
    return {"sync": run.status, "build": r.status_code, "lines": [ln for ln in text.splitlines() if ln.startswith(want)][:8]}


def _stand_ins():
    from app.connectors.gsc_fake import FakeGSC
    from app.connectors.meta_fake import FakeMeta
    return FakeMeta(token="tok-agency"), FakeGSC(token="tok-agency")


def _meta_days(base):
    return {d: {"spend": "%.2f" % (base + d.day), "impressions": 1000 * d.day, "clicks": 30 + d.day, "purchases": 2, "purchase_value": "%.2f" % (3 * base)}
            for d in days(date(2026, 9, 1), date(2026, 11, 1))}


@case("F2")
def f2():
    import sync_gsc
    import sync_meta
    fm, fg = _stand_ins()
    with fm, fg:
        fm.set_account("1111", _meta_days(100.0), currency="EUR")
        fg.set_site("sc-domain:f2.example", {d: (100 + d.day, 3000 + d.day, 7.5) for d in days(date(2026, 9, 1), date(2026, 11, 1))})
        tid, key = tenant("F2 Monthly Sync Client")
        store(tid, "f2")
        m = connection(tid, "meta", "act_1111", "f2 ads")
        g = connection(tid, "gsc", "sc-domain:f2.example", "f2 search")
        envm = {"META_AD_ACCOUNT_ID": "1111", "META_ACCESS_TOKEN": "tok-agency", "META_API_BASE_URL": fm.url}
        envg = {"GSC_SITE_URL": "sc-domain:f2.example", "GSC_ACCESS_TOKEN": "tok-agency", "GSC_API_BASE_URL": fg.url}
        outs = [run_script(sync_meta, ["--tenant", tid, "--connection", m, "--end", e], envm)[1] for e in ("2026-10-01", "2026-11-01")]
        outs += [run_script(sync_gsc, ["--tenant", tid, "--connection", g, "--end", e], envg)[1] for e in ("2026-10-01", "2026-11-01")]
        r = api().build(key, "monthly-report:f2-monthly-sync-client:2026-10", as_of="2026-11-01")
        text = dump(api().call("get", "/api/reports/monthly/2026-10/deck", key).content)
    return {"syncs": outs, "build": r.status_code, "missing_lines": [ln for ln in text.splitlines() if ln.startswith("Days with no data in October")]}


@case("F3")
def f3():
    import sync_meta
    fm, fg = _stand_ins()
    with fm, fg:
        fm.set_account("1111", _meta_days(100.0), currency="EUR")
        fm.set_account("2222", _meta_days(20.0), currency="EUR")
        tid, key = tenant("F3 Client B Pets")
        store(tid, "f3b")
        mb = connection(tid, "meta", "act_2222", "Client B ads")
        env = {"META_AD_ACCOUNT_ID": "2222", "META_ACCESS_TOKEN": "tok-agency", "META_API_BASE_URL": fm.url}
        run_script(sync_meta, ["--tenant", tid, "--connection", mb, "--end", "2026-10-01", "--days", "30"], env)
        stale = run_script(sync_meta, ["--tenant", tid, "--connection", mb, "--end", "2026-11-01", "--days", "31"], {**env, "META_AD_ACCOUNT_ID": "1111"})
        r = api().build(key, "monthly-report:f3-client-b-pets:2026-10", as_of="2026-11-01")
        text = dump(api().call("get", "/api/reports/monthly/2026-10/deck", key).content)
    return {"stale_sync": stale, "build": r.status_code, "lines": [ln for ln in text.splitlines() if ln.startswith(("Ad spend |", "--- slide 6"))][:3]}


@case("F3b")
def f3b():
    import sync_gsc
    fm, fg = _stand_ins()
    with fm, fg:
        fg.set_site("sc-domain:client-a.example", {d: (100 + d.day, 3000 + d.day, 7.5) for d in days(date(2026, 9, 1), date(2026, 11, 1))})
        fg.set_site("sc-domain:client-b.example", {d: (5, 400, 22.0) for d in days(date(2026, 9, 1), date(2026, 11, 1))})
        tid, key = tenant("F3b Client B Pets")
        store(tid, "f3bb")
        gb = connection(tid, "gsc", "sc-domain:client-b.example", "Client B search")
        env = {"GSC_SITE_URL": "sc-domain:client-b.example", "GSC_ACCESS_TOKEN": "tok-agency", "GSC_API_BASE_URL": fg.url}
        run_script(sync_gsc, ["--tenant", tid, "--connection", gb, "--end", "2026-10-01", "--days", "30"], env)
        stale = run_script(sync_gsc, ["--tenant", tid, "--connection", gb, "--end", "2026-11-01", "--days", "31"], {**env, "GSC_SITE_URL": "sc-domain:client-a.example"})
        r = api().build(key, "monthly-report:f3b-client-b-pets:2026-10", as_of="2026-11-01")
        text = dump(api().call("get", "/api/reports/monthly/2026-10/deck", key).content)
    return {"stale_sync": stale, "build": r.status_code, "lines": [ln for ln in text.splitlines() if ln.startswith("Search clicks |")][:2]}


@case("F4")
def f4():
    w = need_world()
    k1, k2 = "monthly-report:northwind-eu:2026-10", "monthly-report:northwind-uk:2026-10"
    sync(w["d"], w["d_ads1"], meta_connector(meta_handler({"4001": [meta_row(d, "50.00", 1000, 20) for d in days(date(2026, 9, 1), date(2026, 11, 1))]}), account="4001"),
         date(2026, 9, 1), date(2026, 11, 1))
    sync(w["d"], w["d_ads2"], meta_connector(meta_handler({"4002": [meta_row(d, "9.00", 300, 4, currency="GBP") for d in days(date(2026, 9, 1), date(2026, 11, 1))]}), account="4002"),
         date(2026, 9, 1), date(2026, 11, 1))
    eu = api().build(w["kd"], k1, as_of="2026-11-01", paid_connection_id=w["d_ads1"]).json()
    uk = api().build(w["kd"], k2, as_of="2026-11-01", paid_connection_id=w["d_ads2"]).json()
    fetch_for_eu = api().call("get", "/api/reports/monthly/2026-10/deck", w["kd"])
    api().call("post", "/api/reports/monthly/2026-10/delivered", w["kd"])
    eu_again = api().build(w["kd"], k1, as_of="2026-11-01", paid_connection_id=w["d_ads1"]).json()
    return {"eu_sha": eu.get("sha256", "")[:12], "uk_sha": uk.get("sha256", "")[:12], "fetched_for_eu_entry": (fetch_for_eu.headers.get("X-Report-Sha256") or "")[:12],
            "eu_replay_delivered_after_eu_send": eu_again.get("delivered")}


@case("S5")
def s5():
    import sync_meta
    from app.connectors.meta_fake import FakeMeta
    w = need_world()
    fm = FakeMeta(token="tok-agency")
    with fm:
        fm.set_account("3001", _meta_days(10.0), currency="EUR")
        code, text = run_script(sync_meta, ["--tenant", w["b"], "--connection", w["a_ads"], "--end", "2026-11-01"],
                                {"META_AD_ACCOUNT_ID": "3001", "META_ACCESS_TOKEN": "tok-agency", "META_API_BASE_URL": fm.url})
    return {"exit": code, "text": text}


_NODE_SCRIPT = r"""
const fs = require('fs');
const wf = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const node = (n) => wf.nodes.find((x) => x.name === n);
function evalExpr(text, $json) {
  const body = text.startsWith('=') ? text.slice(1) : text;
  return body.replace(/\{\{([\s\S]*?)\}\}/g, (_, expr) => {
    const v = new Function('$json', 'return (' + expr + ');')($json);
    return typeof v === 'object' ? JSON.stringify(v) : String(v);
  });
}
const build = node('Build the report').parameters;
const keyText = build.headerParameters.parameters.find((p) => p.name === 'Idempotency-Key').value;
const tenants = [
  { name: 'Example Outfitters', api_key: 'k1' },
  { name: 'Smith & Sons Plumbing, LLC', api_key: 'k3' },
  { name: 'Acme', api_key: 'k5', paid_connection_id: '6f1c2b0e-1111-4c2a-9a10-000000000001' },
  { name: 'Acme EU', api_key: 'k5', paid_connection_id: '6f1c2b0e-1111-4c2a-9a10-000000000002', search_connection_id: '' },
  { name: 'Bayside Dental', api_key: 'k6', connection_id: null, web_connection_id: '6f1c2b0e-1111-4c2a-9a10-000000000003' },
];
const out = [];
for (const t of tenants) {
  for (const as_of of ['', '2026-11-02']) {
    const $json = { month: '2026-10', as_of, hub_url: 'http://127.0.0.1:8000', tenant: t };
    out.push({ tenant: t.name, as_of, key: evalExpr(keyText, $json), body: evalExpr(build.jsonBody, $json) });
  }
}
const ifs = {};
for (const n of ['Already delivered?', 'Email sent?', 'Any failed?']) ifs[n] = node(n).parameters.conditions.conditions[0].leftValue;
const evalBool = (text, $json) => new Function('$json', 'return (' + text.replace(/^=\{\{|\}\}$/g, '') + ');')($json);
const checks = {
  email_sent_no_credential: evalBool(ifs['Email sent?'], { error: { message: 'Node does not have any credentials set' } }),
  email_sent_accepted: evalBool(ifs['Email sent?'], { accepted: ['owner@agency.example'] }),
  any_failed_mixed: evalBool(ifs['Any failed?'], { data: [{ status: 'built, delivery off' }, { status: 'failed' }] }),
  any_failed_none: evalBool(ifs['Any failed?'], { data: [{ status: 'delivered' }, { status: 'skipped, already delivered' }] }),
};
const sched = node('Schedule, 1st of the month 13:00 UTC').parameters.rule.interval[0].expression;
console.log(JSON.stringify({ cron: sched, timezone: wf.settings.timezone, keys_and_bodies: out, checks }));
"""


def _n8n():
    if "n8n" not in STATE:
        done = subprocess.run([NODE, "-e", _NODE_SCRIPT, "-", str(ROOT / "n8n" / "monthly_report.workflow.json")], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        assert done.returncode == 0, done.stderr
        STATE["n8n"] = json.loads(done.stdout)
    return STATE["n8n"]


@case("N1")
def n1():
    from app.report.runs import KEY_RE
    k = [x for x in _n8n()["keys_and_bodies"] if x["tenant"].startswith("Smith")][0]["key"]
    return {"key": k, "valid": bool(KEY_RE.match(k))}


@case("N2")
def n2():
    return {x["tenant"] + (" as_of" if x["as_of"] else ""): x["body"] for x in _n8n()["keys_and_bodies"] if x["tenant"].startswith(("Acme", "Example"))}


@case("N3")
def n3():
    c = _n8n()["checks"]
    return {"no_credential": c["email_sent_no_credential"], "accepted": c["email_sent_accepted"]}


@case("N4")
def n4():
    j = _n8n()
    return {"mixed": j["checks"]["any_failed_mixed"], "all_good": j["checks"]["any_failed_none"], "cron": j["cron"], "tz": j["timezone"]}


# ---------------------------------------------------------------- what each case must give, in the reviewer's `expected`

def _is(*prefixes):
    return lambda a: isinstance(a, str) and a.startswith(prefixes)


def _lines(a, *needles):
    text = "\n".join(a["lines"])
    return all(n in text for n in needles)


EXPECT = {
    "M1": lambda a: a == [["ad_clicks", 120, None], ["ad_impressions", 5000, None], ["ad_purchase_value", 41230, "EUR"], ["ad_purchases", 7, None], ["ad_spend", 12345, "EUR"]],
    "M2": lambda a: a == [["ad_purchase_value", 48000, "JPY"], ["ad_spend", 15000, "JPY"]],
    "M3": lambda a: a == ["ad_clicks", "ad_impressions", "ad_spend"],
    "M4": lambda a: a == {"days": 31, "requests": 4, "any_request_carried_SECRET": False},
    "M5": _is("AuthExpired"),
    "M6": _is("AuthRevoked"),
    "M7": _is("RateLimited: gave up after 3 attempts"),
    "M8": _is("UpstreamBroken: a row for 2026-10-03 has no account_currency"),
    "M9": _is("UpstreamBroken: rows name two currencies: EUR, USD"),
    "M10": _is("UpstreamBroken: paging does not advance"),
    "G1": lambda a: a == [[f"2026-10-{d}", d >= 30] for d in range(25, 32)],
    "G2": lambda a: a == [["search_clicks", 12], ["search_impressions", 340], ["search_position", 8250]],
    "G3": lambda a: a == "sc-domain%3Abayside-dental.example",
    "G4": _is("UpstreamBroken"),
    "G5": _is("RateLimited"),
    "G6": _is("AuthRevoked"),
    "G7": _is("UpstreamBroken"),
    "G8": lambda a: a == {"points": 0},
    "A1": lambda a: a == {"status": 200, "state": "created", "slides": 9, "sources": ["store", "ads", "search"]},
    "A2": lambda a: a == {"status": 200, "state": "replayed", "same_sha": True, "rows": 1},
    "A3": lambda a: a == {"status": 403, "rows": 0},
    "A4": lambda a: a["status"] == 422 and "this key was used for 2026-10" in a["detail"],
    "A5": lambda a: a == {"bayside": 404, "harbor": 200},
    "A6": lambda a: a["status"] == 422 and "name one" in a["detail"],
    "A7": lambda a: a["status"] == 200 and a["state"] == "created" and "ads" in a["sources"],
    "A8": lambda a: a["status"] == 422 and "no active paid media connection found with that id" in a["detail"],
    "A9": lambda a: a["status"] == 422 and "no active connection found for this tenant" in a["detail"],
    "A10": lambda a: a["status"] == 422 and "ad spend in October 2026 is in USD and in September 2026 in EUR" in a["detail"],
    "A11": lambda a: a == {"first": False, "replay_delivered": True, "second": True},
    "A12": lambda a: a["status"] == 400 and "8 to 128 letters, digits and : _ . @ -" in a["detail"],
    "A13": lambda a: a == {"status": 200, "body_sha_matches_header": True, "matches_build": True},
    "D1": lambda a: a["lines"] and not a["any_dollar"] and all("EUR" in ln for ln in a["lines"] if not ln.startswith("Cost per click | n/a")),
    "D2": lambda a: a == ["May still change, ads: Oct 25, Oct 26, Oct 27, Oct 28, Oct 29, Oct 30, Oct 31.", "May still change, search: Oct 29, Oct 30, Oct 31."],
    # the fixed findings: what the case expected, now true
    "F1": lambda a: a["sync"] == "succeeded" and a["build"] == 200 and _lines(a, "Ad spend | EUR 2,000.00", "Purchases | 30 |", "Purchase value | EUR 3,000.00", "Return on ad spend | 1.50 |")
    and "3.00" not in "\n".join(a["lines"]),
    "F2": lambda a: a["build"] == 200 and all(s.startswith("sync succeeded: window 2026-09-01 to 2026-10-01") or s.startswith("sync succeeded: window 2026-10-01 to 2026-11-01")
                                              for s in a["syncs"]) and a["missing_lines"] == ["Days with no data in October, store: none.", "Days with no data in October, ads: none.",
                                                                                              "Days with no data in October, search: none."],
    "F3": lambda a: a["stale_sync"][0] == 2 and "1111" in a["stale_sync"][1] and "act_2222" in a["stale_sync"][1] and "tok-agency" not in a["stale_sync"][1]
    and "3,596.00" not in "\n".join(a["lines"]),
    "F3b": lambda a: a["stale_sync"][0] == 2 and "sc-domain:client-a.example" in a["stale_sync"][1] and "sc-domain:client-b.example" in a["stale_sync"][1]
    and "tok-agency" not in a["stale_sync"][1] and "3,596" not in "\n".join(a["lines"]),
    "S5": lambda a: a["exit"] == 2 and "that tenant has no active meta connection with that id" in a["text"],
    "N1": lambda a: a == {"key": "monthly-report:smith-sons-plumbing-llc:2026-10", "valid": True},
    "N2": lambda a: json.loads(a["Acme"])["paid_connection_id"].endswith("0001") and a["Example Outfitters"] == '{"month":"2026-10"}'
    and json.loads(a["Acme EU"]) == {"month": "2026-10", "paid_connection_id": "6f1c2b0e-1111-4c2a-9a10-000000000002"},
    "N3": lambda a: a == {"no_credential": False, "accepted": True},
    "N4": lambda a: a == {"mixed": True, "all_good": False, "cron": "0 13 1 * *", "tz": "UTC"},
}

#: what the pinned limit would look like if it had moved: the EU entry fetching its own deck and reading delivered after its send
MOVED = {"F4": lambda a: a["fetched_for_eu_entry"] == a["eu_sha"] or a["eu_replay_delivered_after_eu_send"] is True}

_HEX12 = re.compile(r"\b[0-9a-f]{12}\b")


def mask(text: str) -> str:
    """A deck holds the time it was made, so its hash differs between runs; the rest of the line is compared."""
    return _HEX12.sub("<hash>", text)


def _actual_text(actual) -> str:
    return json.dumps(actual, default=str, ensure_ascii=False)


@pytest.fixture(scope="module")
def replayed():
    """Every case once, in the reviewer's order: {id: actual or 'CRASH ...'}. The database and node cases are left out when either is missing."""
    results = {}
    try:
        for cid, fn in REPLAY.items():
            needs_db = cid.startswith(("A", "D", "F", "S")) and cid != "G8"
            if (needs_db and (psycopg is None or not _reachable())) or (cid.startswith("N") and NODE is None):
                continue
            try:
                results[cid] = json.loads(_actual_text(fn()))
            except Exception:  # noqa: BLE001
                results[cid] = "CRASH " + traceback.format_exc()[-800:]
    finally:
        if "api" in STATE:
            STATE.pop("api").close()
        STATE.pop("w", None)
    return results


def test_the_corpus_is_the_one_that_was_written():
    assert len(CASES) == 43
    assert sum(1 for c in CASES if c["expect"] == "pass") == 20 and sum(1 for c in CASES if c["expect"] == "fail") == 23
    for n, c in enumerate(CASES, 1):
        assert c["source"] in ("real", "invented"), n
        assert len(c["why"]) >= 10 and c["expected"] and c["promise"] and c["command"], n


def test_every_case_has_a_replay_and_an_expectation_and_none_is_skipped_silently():
    assert NOT_REPLAYABLE == {}
    assert IDS == list(REPLAY) and len(set(IDS)) == 43
    assert set(IDS) - set(EXPECT) == {"F4"}, "every case but the pinned limit states what it expects"
    assert set(KNOWN_LIMITS) == {IDS.index("F4") + 1} and {IDS.index(f) + 1 for f in FIXED} == {34, 35, 36, 37}


NUMBERED = [(n, cid) for n, cid in enumerate(IDS, 1) if cid not in KNOWN_LIMITS.values() and n not in KNOWN_LIMITS]
WITH_DB = lambda cid: cid.startswith(("A", "D", "F", "S")) and cid != "G8"  # noqa: E731


def _marks(cid):
    marks = []
    if WITH_DB(cid):
        marks.append(needs_postgres)
    if cid.startswith("N") or WITH_DB(cid):
        marks.append(needs_node if cid.startswith("N") else needs_postgres)
    return marks


def _param(n, cid):
    return pytest.param(n, cid, marks=_marks(cid), id="%d-%s" % (n, cid))


@pytest.mark.parametrize("n, cid", [_param(n, cid) for n, cid in NUMBERED])
def test_the_code_gives_the_outcome_the_corpus_expects(replayed, n, cid):
    case_ = CASES[n - 1]
    got = replayed[cid]
    assert not (isinstance(got, str) and got.startswith("CRASH")), got
    assert EXPECT[cid](got), "case %d (%s): %s | expected %s | got %s" % (n, case_["expect"], case_["why"], case_["expected"], _actual_text(got))


@pytest.mark.parametrize("n, cid", [_param(n, cid) for n, cid in NUMBERED if cid not in FIXED])
def test_the_recorded_actual_still_holds_for_every_case_that_was_not_fixed(replayed, n, cid):
    assert mask(_actual_text(replayed[cid])) == mask(CASES[n - 1]["actual"])


@pytest.mark.parametrize("n, cid", [_param(n, cid) for n, cid in NUMBERED if cid in FIXED])
def test_a_fixed_finding_no_longer_gives_what_71cffbb_gave(replayed, n, cid):
    assert mask(_actual_text(replayed[cid])) != mask(CASES[n - 1]["actual"]), "case %d still gives the recorded actual" % n


@needs_postgres
@pytest.mark.parametrize("n", sorted(KNOWN_LIMITS))
def test_a_known_limit_is_pinned_to_what_the_code_does_today(replayed, n):
    cid = IDS[n - 1]
    got = replayed[cid]
    assert not (isinstance(got, str) and got.startswith("CRASH")), got
    assert mask(_actual_text(got)) == mask(CASES[n - 1]["actual"]), KNOWN_LIMITS[n]
    assert not MOVED[cid](got), "the limit moved: remove case %d from KNOWN_LIMITS" % n


@needs_postgres
def test_case_34_the_month_with_zero_purchase_days_prints_a_return_that_agrees_with_its_own_spend_and_value(replayed):
    lines = replayed["F1"]["lines"]
    spend = next(ln for ln in lines if ln.startswith("Ad spend |"))
    value = next(ln for ln in lines if ln.startswith("Purchase value |"))
    roas = next(ln for ln in lines if ln.startswith("Return on ad spend |"))
    assert "EUR 2,000.00" in spend and "EUR 3,000.00" in value and roas.split(" | ")[1] == "1.50", lines
    assert not any("on 10 of" in ln for ln in lines if ln.startswith("Return on ad spend")), "the return covers the 20 days with spend and a purchase count"


@needs_postgres
def test_case_36_the_refusal_names_both_accounts_and_nothing_was_written_for_the_stale_run(replayed):
    code, text = replayed["F3"]["stale_sync"]
    assert code == 2 and text == "no sync: the environment names act_1111 but that connection holds act_2222"
