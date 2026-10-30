"""Replay of the blind corpus for the Meta Ads connector and the paid half of the report (output round 1).

tests/corpus/hub_meta_corpus_blind_1.jsonl holds 36 cases (23 must-pass and 13 must-fail) written by a reviewer who read
docs/META_SOURCES.md and the code. Each carries `promise` (a quote from the spec), `why` (who is harmed on an ordinary day), `source` (real: a vendor page the
case quotes, or invented) and `expected`. The adversary also recorded `actual`, what the code did at 0e30795 when it wrote the case.

Every case is replayed here against the real code, offline: the connector through httpx.MockTransport with the queued responses the case names, the paid
half of the report from stored-row dicts, and case 35 through the real sync runner and report service into Postgres from the stand-in Meta server. A case the
code agrees with must give `expected` exactly. A case the owner decided not to fix is in KNOWN_LIMITS by number with the reason, and its live outcome is
pinned to the recorded `actual`, so the limit cannot move silently. NOT_REPLAYABLE is empty on purpose: all 36 are replayed (case 35 needs the scratch database
and is skipped, never failed, when no Postgres is reachable).

Case 35 was finding F1: the schedule built the deck before the settled window of the ads sync. The case's own input says the sync ran an hour before the
schedule; the replay keeps that relation (the sync runs one hour before the committed schedule fires, 12:00 UTC at 13:00) and a second test pins the case's
literal 05:00 input to what it still does, because a sync before 12:00 UTC still stops at 30 October and the fix is to run the deck after that edge.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

from app.connectors.meta import MetaConnector  # noqa: E402
from app.report.deck import _paid_note  # noqa: E402
from app.report.monthly import ReportError, build_report  # noqa: E402
from app.report.paid import attach_paid, paid_claim_title  # noqa: E402
from report_fixtures import day_rows  # noqa: E402

CORPUS = ROOT / "tests" / "corpus" / "hub_meta_corpus_blind_1.jsonl"
CASES = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]

#: case number -> why it is not replayed offline. Empty: all 36 are replayed.
NOT_REPLAYABLE: dict[int, str] = {}

#: case number -> why the code is deliberately left as it is (the lead's decision, scored 2x1 or less). The live outcome is pinned to the recorded `actual`.
KNOWN_LIMITS = {
    26: "F5 KNOWN_LIMIT 2x1: Meta code 80000 (a business use case limit) is raised as UpstreamBroken; the run is recorded and nothing is lost, the vendor page for the table was not loaded",
    34: "F6 KNOWN_LIMIT 2x1: the purchase coverage note says 'on 29 of 31 days' for a month with two days of no delivery, a wording of a note about days with no delivery",
}

BASE = "https://graph.example.test"

# ---------------------------------------------------------------- one `actual` per kind of case, the adversary's harness reproduced


async def _no_sleep(_):
    return None


def _hosts(urls):
    hosts = sorted({httpx.URL(u).host for u in urls})
    return "" if hosts == ["graph.example.test"] or not hosts else "; hosts requested %s" % hosts


def meta_fetch(inp: dict) -> str:
    queue = list(inp["responses"])
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if not queue:
            return httpx.Response(599, json={"error": {"message": "harness: no more responses", "code": 999}})
        r = queue.pop(0)
        headers = dict(r.get("headers") or {})
        if "text" in r:
            return httpx.Response(r["status"], text=r["text"], headers=headers)
        return httpx.Response(r["status"], json=r["body"], headers=headers)

    async def go() -> str:
        try:
            con = MetaConnector(ad_account_id=inp.get("ad_account_id", "act_1234567890"), access_token="TOKEN-SECRET-123", base_url=BASE,
                                client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=_no_sleep, rng=lambda: 0.0,
                                max_attempts=inp.get("max_attempts", 3))
        except ValueError as exc:
            return "ValueError: %s" % exc
        start, end = (date.fromisoformat(x) for x in inp["window"])
        points, waits, pages = [], 0, 0
        try:
            async for page in con.fetch(window_start=start, window_end=end):
                pages += 1
                waits += page.throttle_waits
                points += page.points
        except Exception as exc:  # noqa: BLE001
            err = "%s(%s): %s" % (type(exc).__name__, getattr(exc, "code", ""), exc)
            leak = "TOKEN-SECRET-123" in str(exc)
            return "raised %s; pages before %d; points before %d%s%s" % (
                err[:220], pages, len(points), "; TOKEN LEAKED" if leak else "", _hosts(seen_urls))
        body = "; ".join("%s %s=%s%s" % (p.observed_on, p.metric_key, p.value_numeric, " " + p.currency if p.currency else "")
                         for p in sorted(points, key=lambda p: (p.observed_on, p.metric_key)))
        return "ok pages %d waits %d calls %d: %s%s" % (pages, waits, len(seen_urls), body, _hosts(seen_urls))

    return asyncio.run(go())


def _store(year, month, days):
    rows = []
    for d in days:
        rows += day_rows(date(year, month, d), 10, 25000, 0, 8)
    return rows


def _ad_rows(spec):
    rows = []
    for day, vals in spec.items():
        d = date.fromisoformat(day)
        cur = vals.get("currency", "EUR")
        for key in ("ad_spend", "ad_impressions", "ad_clicks", "ad_purchases", "ad_purchase_value"):
            if key in vals:
                rows.append({"observed_on": d, "metric_key": key, "value_numeric": vals[key],
                             "currency": cur if key in ("ad_spend", "ad_purchase_value") else None})
    return rows


def expand(inp):
    """A compact ad-day generator: {"gen": {"months": {"2026-10": [days], ...}, "spend": n, ...}} plus explicit days."""
    spec = dict(inp.get("days") or {})
    g = inp.get("gen")
    if g:
        for ym, days in g["months"].items():
            y, m = (int(x) for x in ym.split("-"))
            for d in days:
                key = date(y, m, d).isoformat()
                if key in spec:
                    continue
                v = {"ad_spend": g["spend"], "ad_impressions": g["impressions"], "ad_clicks": g["clicks"]}
                if g.get("purchases") is not None:
                    v["ad_purchases"] = g["purchases"]
                    v["ad_purchase_value"] = g["purchase_value"]
                v["currency"] = g.get("currency", "EUR")
                spec[key] = v
    return spec


def paid(inp: dict) -> str:
    y, m = (int(x) for x in inp["month"].split("-"))
    as_of = date.fromisoformat(inp["as_of"])
    covered = [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in inp["covered"]]
    store = _store(2026, 9, range(1, 31)) + _store(2026, 10, range(1, 32))
    try:
        report = build_report(store, y, m, label="Store", covered=[(date(2026, 9, 1), date(2026, 11, 1))], as_of=as_of)
        attach_paid(report, _ad_rows(expand(inp)), label="Ads", covered=covered, as_of=as_of)
    except ReportError as exc:
        return "ReportError: %s" % exc
    p = report.paid
    out = []
    for w in inp.get("show", ["title"]):
        if w == "title":
            out.append("title: " + paid_claim_title(p, "ad_spend"))
        elif w == "missing":
            out.append("missing: %s" % [d.isoformat() for d in p.current.missing_days])
        elif w == "provisional":
            out.append("provisional: %d" % len(p.provisional))
        else:
            k = p.kpi(w)
            out.append("%s %s (prev %s, %s) note '%s'" % (k.label, k.current_display, k.previous_display, k.pct_display, _paid_note(p, k)))
    return " | ".join(out)


# ---------------------------------------------------------------- case 35: the sync and the deck through the real runner into Postgres

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")
TOKEN = "tok-adv-meta"
ACCOUNT = "990001"

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


def _schedule_instant() -> datetime:
    """The first instant the committed workflow fires after 26 October 2026: 1 November at the hour of its cron expression."""
    from test_n8n_class_tables import _schedule_time
    return _schedule_time(2026, 11)


async def _sync_then_deck(sync_now: datetime, report_as_of: date) -> tuple[str, list[date], str]:
    from app.connectors.meta_fake import FakeMeta
    from app.db import Database
    from app.report.deck import dump_deck
    from app.report.service import generate
    from app.sync.runner import SyncRunner
    from app.connectors.meta import settled_window_end

    tenant = uuid.uuid4()
    ads = {}
    d = date(2026, 9, 1)
    while d < date(2026, 11, 1):
        ads[d] = {"spend": "200.00", "impressions": 10000, "clicks": 200, "purchases": 4, "purchase_value": "600.00"}
        d += timedelta(days=1)
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select set_config('app.tenant_id', %s, false)", (str(tenant),))
        cur.execute("insert into tenant (id, slug, name) values (%s, %s, %s)", (tenant, "adv-" + tenant.hex[:6], "adv tenant"))
        ids = {}
        for key, platform, account in (("shop", "shopify", "adv.myshopify.com"), ("ads", "meta", "act_" + ACCOUNT)):
            cur.execute("insert into platform_connection (tenant_id, platform, external_account, display_name, credentials_enc) "
                        "values (%s, %s, %s, %s, %s) returning id", (tenant, platform, account, key, b"x"))
            ids[key] = cur.fetchone()[0]
        d = date(2026, 9, 1)
        while d < date(2026, 11, 1):
            for r in day_rows(d, 10, 25000, 0, 8):
                cur.execute("insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric, currency) "
                            "values (%s, %s, %s, %s, %s, %s)", (tenant, ids["shop"], r["metric_key"], r["observed_on"], r["value_numeric"], r["currency"]))
            d += timedelta(days=1)
        cur.execute("insert into sync_run (tenant_id, connection_id, status, trigger, window_start, window_end, finished_at) "
                    "values (%s, %s, 'succeeded', 'schedule', '2026-09-01', '2026-11-01', now())", (tenant, ids["shop"]))
    end = settled_window_end(sync_now)
    start = end - timedelta(days=61)
    with FakeMeta(token=TOKEN) as fake:
        fake.set_account(ACCOUNT, ads, currency="EUR")
        con = MetaConnector(ad_account_id=ACCOUNT, access_token=TOKEN, base_url=fake.url,
                            client=httpx.AsyncClient(trust_env=False, timeout=10), sleep=_no_sleep)
        db = Database(DSN, max_size=2, role="sync_worker")
        await db.connect()
        try:
            await SyncRunner(db).run_once(tenant_id=str(tenant), connection_id=str(ids["ads"]), connector=con,
                                          window_start=start, window_end=end, trigger="manual")
        finally:
            await con.aclose()
            await db.close()
    db = Database(DSN, max_size=2)
    await db.connect()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path, report, _ = await generate(db, str(tenant), 2026, 10, Path(tmp) / "deck.pptx", as_of=report_as_of)
            text = dump_deck(path)
    finally:
        await db.close()
    missing = list(report.paid.current.missing_days)
    spend = next(ln for ln in text.splitlines() if ln.startswith("Ad spend |"))
    return spend, missing, text


@pytest.fixture(scope="module")
def database():
    from app.db import Database  # noqa: F401 - imported so a missing driver fails here, not inside the case
    os.environ["DATABASE_URL"] = DSN
    migrations = sorted(p.name for p in (ROOT / "migrations").glob("*.sql"))
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade; create schema public")
        for name in migrations:
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))
        cur.execute("grant usage on schema public to sync_worker")
    yield DSN


# ---------------------------------------------------------------- the replay


def actual(index: int) -> str:
    kind = CASES[index]["input"]["kind"]
    if kind == "meta_fetch":
        return meta_fetch(CASES[index]["input"])
    if kind == "paid_rows":
        return paid(CASES[index]["input"])
    raise AssertionError("case %d has kind %r, which has its own replay" % (index, kind))


def test_the_corpus_is_the_one_that_was_written():
    assert len(CASES) == 36
    assert sum(1 for c in CASES if c["expect"] == "pass") == 23 and sum(1 for c in CASES if c["expect"] == "fail") == 13
    for n, c in enumerate(CASES):
        # a real case may name the vendor page it quotes after the word, an invented one carries the word alone
        assert c["source"].split(" ")[0] in ("real", "invented"), n
        assert len(c["why"]) >= 10 and c["expected"] and c["promise"], n


def test_no_case_is_skipped_silently():
    assert NOT_REPLAYABLE == {}
    assert {c["input"]["kind"] for c in CASES} == {"meta_fetch", "paid_rows", "pipeline"}
    assert set(KNOWN_LIMITS) <= set(range(len(CASES)))
    assert [n for n, c in enumerate(CASES) if c["input"]["kind"] == "pipeline"] == [35]


REPLAYED = [n for n, c in enumerate(CASES) if c["input"]["kind"] in ("meta_fetch", "paid_rows")]


@pytest.mark.parametrize("index", [n for n in REPLAYED if n not in KNOWN_LIMITS])
def test_the_code_gives_the_outcome_the_corpus_expects(index):
    case = CASES[index]
    assert actual(index) == case["expected"], "case %d (%s): %s" % (index, case["expect"], case["why"])


@pytest.mark.parametrize("index", [n for n in REPLAYED if n not in KNOWN_LIMITS])
def test_the_recorded_actual_still_holds(index):
    assert actual(index) == CASES[index]["actual"]


@pytest.mark.parametrize("index", sorted(KNOWN_LIMITS))
def test_a_known_limit_is_pinned_to_what_the_code_does_today(index):
    assert actual(index) == CASES[index]["actual"], KNOWN_LIMITS[index]
    assert actual(index) != CASES[index]["expected"], "the limit moved: remove case %d from KNOWN_LIMITS" % index


@needs_postgres
def test_case_35_the_deck_the_schedule_builds_holds_every_day_of_the_ad_month(database):
    """F1 fixed: the sync runs an hour before the committed schedule, at the edge of the settled window, and the deck is built at the schedule."""
    case = CASES[35]
    assert case["expected"].startswith("Ad spend EUR 6,200.00 for October with no ad day missing")
    fires = _schedule_instant()
    sync_now = fires - timedelta(hours=1)
    assert sync_now.hour == 12 and sync_now.date() == date(2026, 11, 1)
    spend, missing, text = asyncio.run(_sync_then_deck(sync_now, fires.date()))
    assert missing == [], "ads still miss %s" % missing
    assert spend.startswith("Ad spend | EUR 6,200.00 |"), spend
    assert "missing days of ads data" not in text and "October is missing" not in text


@needs_postgres
def test_case_35_as_recorded_a_sync_before_the_settled_edge_still_leaves_the_last_day_out(database):
    """The literal input, a sync at 05:00 UTC: its default end is still 31 October, which is why the run is scheduled after 12:00 UTC."""
    case = CASES[35]
    sync_now = datetime.fromisoformat(case["input"]["sync_now"])
    assert sync_now.hour == 5
    spend, missing, text = asyncio.run(_sync_then_deck(sync_now, date.fromisoformat(case["input"]["report_as_of"])))
    assert missing == [date(2026, 10, 31)] and spend.startswith("Ad spend | EUR 6,000.00 |")
    assert "ads missing days ['2026-10-31']" in case["actual"] and "Ad spend EUR 6,000.00" in case["actual"]
