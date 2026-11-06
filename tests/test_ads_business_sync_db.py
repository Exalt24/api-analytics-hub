"""Google Ads, TikTok, Square, QuickBooks and Shopmonkey through the real sync runner into real Postgres, then read back as the request role.

The whole path, against the stand-in servers (never a live account): each connector over HTTP, the SyncRunner as the sync_worker role, the dated snapshots, row
level security as the request role, the combined ad spend view read from what was stored, and the `/api/reports/ad-spend` call with real API keys. It also proves
what only a database can: migration 013 teaches the currency check about the six new money keys and the metric table about `milli_count`, a second sync rewrites the
same rows, a figure a platform restates replaces the stored one, a failed run is recorded with its code and writes nothing for a window that cannot be written
whole, and one tenant never sees another's rows.

Skips cleanly when Postgres is not running (`docker compose up -d`). Like the other database suites it drops and recreates the `public` schema of the database
it points at.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
pytest.importorskip("asyncpg")

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


# CI sets ADS_REQUIRE_DB so that a Postgres that did not start FAILS this suite instead of skipping it green.
if os.environ.get("ADS_REQUIRE_DB") == "1" and not _reachable():
    raise RuntimeError("ADS_REQUIRE_DB is set but Postgres is not reachable at %s" % DSN)

pytestmark = pytest.mark.skipif(not _reachable(), reason="Postgres not reachable, run docker compose up -d")

import httpx  # noqa: E402

from app.api.auth import generate_key, hash_key  # noqa: E402
from app.connectors.fake_http import Fault  # noqa: E402
from app.connectors.google_ads import GoogleAdsConnector  # noqa: E402
from app.connectors.google_ads_fake import FakeGoogleAds  # noqa: E402
from app.connectors.meta import MetaConnector  # noqa: E402
from app.connectors.meta_fake import FakeMeta  # noqa: E402
from app.connectors.quickbooks import QuickBooksConnector  # noqa: E402
from app.connectors.quickbooks_fake import FakeQuickBooks  # noqa: E402
from app.connectors.shopmonkey import ShopmonkeyConnector  # noqa: E402
from app.connectors.shopmonkey_fake import FakeShopmonkey  # noqa: E402
from app.connectors.square import SquareConnector  # noqa: E402
from app.connectors.square_fake import FakeSquare  # noqa: E402
from app.connectors.tiktok import TikTokConnector  # noqa: E402
from app.connectors.tiktok_fake import FakeTikTok  # noqa: E402
from app.db import Database  # noqa: E402
from app.report.adspend_service import load_view  # noqa: E402
from app.report.monthly import ReportError  # noqa: E402
from app.sync.runner import SyncRunner  # noqa: E402

TOKEN = "tok-ads-business-db-SECRET"
START, END = date(2026, 10, 1), date(2026, 10, 8)
MIGRATIONS = ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql", "006_ga4_metrics.sql", "007_meta_metrics.sql", "013_ads_and_business_metrics.sql")

GOOGLE_DAYS = {date(2026, 10, d): {"cost_micros": 40_000_000 + d * 1_000_000, "impressions": 900 + d, "clicks": 20 + d,
                                   "conversions": Decimal("1.5") if d % 2 else Decimal(2), "conversions_value": Decimal(100 + d)} for d in range(1, 8)}
TIKTOK_DAYS = {date(2026, 10, d): {"spend": "%d.25" % (15 + d), "impressions": 500 + d, "clicks": 9 + d, "conversion": d % 3} for d in range(1, 8)}
META_DAYS = {date(2026, 10, d): {"spend": "%d.00" % (25 + d), "impressions": 700 + d, "clicks": 12 + d, "purchases": d % 2, "purchase_value": "%d.00" % (d * 10)}
             for d in range(1, 8)}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def servers():
    with FakeGoogleAds(token=TOKEN) as g, FakeTikTok(token=TOKEN) as t, FakeMeta(token=TOKEN) as m, FakeSquare(token=TOKEN) as s, \
            FakeQuickBooks(token=TOKEN) as q, FakeShopmonkey(token=TOKEN) as k:
        g.set_customer("1234567890", GOOGLE_DAYS, currency="USD", leads=["2026-10-02 10:00:00", "2026-10-02 12:00:00", "2026-10-05 09:00:00"])
        t.set_advertiser("700123", TIKTOK_DAYS, currency="USD")
        t.set_advertiser("700999", {date(2026, 10, 1): {"spend": "9999.00", "impressions": 1, "clicks": 1, "conversion": 1}}, currency="EUR")
        m.set_account("5550001", META_DAYS, currency="USD")
        s.set_location("LFTM01", timezone_name="America/New_York", currency="USD")
        s.add_payment("p1", "2026-10-02T03:30:00Z", location_id="LFTM01", total=5000, refunded=500)       # 23:30 on Oct 1 in New York
        s.add_payment("p2", "2026-10-03T15:00:00Z", location_id="LFTM01", total=2500)
        q.set_company("9130350000000001")
        q.add_invoice("9130350000000001", "1", "2026-10-02", "362.07", balance="100.00")
        q.add_invoice("9130350000000001", "2", "2026-10-04", "1200.00", balance="0")
        k.add_order("o1", "2026-10-01T14:00:00Z", invoiced="2026-10-03T16:00:00Z", total_cents=123456)
        k.add_order("o2", "2026-10-02T14:00:00Z")
        k.add_order("o3", "2026-10-02T15:00:00Z")
        yield {"google": g, "tiktok": t, "meta": m, "square": s, "quickbooks": q, "shopmonkey": k}


def connect_row(cur, tenant, platform, account, name):
    cur.execute(
        "insert into platform_connection (tenant_id, platform, external_account, display_name, credentials_enc) "
        "values (%s, %s, %s, %s, %s) returning id", (tenant, platform, account, name, b"ciphertext"))
    return cur.fetchone()[0]


@pytest.fixture(scope="module")
def world(servers):
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    keys = {name: generate_key() for name in ("a_viewer", "b_admin", "c_admin")}
    ids = {}
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade; create schema public")
        for name in MIGRATIONS:
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))
        cur.execute("grant usage on schema public to sync_worker")
        for tenant, label in ((a, "tenant-a"), (b, "tenant-b"), (c, "tenant-c")):
            cur.execute("select set_config('app.tenant_id', %s, false)", (str(tenant),))
            cur.execute("insert into tenant (id, slug, name) values (%s, %s, %s)", (tenant, label, label))
        cur.execute("select set_config('app.tenant_id', %s, false)", (str(a),))
        ids["a_google"] = connect_row(cur, a, "google_ads", "123-456-7890", "Fort Myers Google")
        ids["a_tiktok"] = connect_row(cur, a, "tiktok", "700123", "Fort Myers TikTok")
        ids["a_meta"] = connect_row(cur, a, "meta", "act_5550001", "Fort Myers Meta")
        ids["a_square"] = connect_row(cur, a, "square", "LFTM01", "Fort Myers Square")
        ids["a_qb"] = connect_row(cur, a, "quickbooks", "9130350000000001", "Fort Myers QuickBooks")
        ids["a_shop"] = connect_row(cur, a, "shopmonkey", "all-locations", "Fort Myers Shopmonkey")
        cur.execute("select set_config('app.tenant_id', %s, false)", (str(b),))
        ids["b_tiktok"] = connect_row(cur, b, "tiktok", "700999", "Other tenant TikTok")
        cur.execute("select set_config('app.tenant_id', '', false)")
        for label, tenant in (("a_viewer", a), ("b_admin", b), ("c_admin", c)):
            cur.execute("insert into api_key (tenant_id, key_hash, label, role) values (%s, %s, %s, %s)",
                        (tenant, hash_key(keys[label]), label, "viewer" if label == "a_viewer" else "admin"))
    return {"a": a, "b": b, "c": c, "keys": keys, **ids}


def worker_db():
    return Database(DSN, max_size=2, role="sync_worker")


def request_db():
    return Database(DSN, max_size=2)


def new_client():
    return httpx.AsyncClient(trust_env=False, verify=False, timeout=10)


async def no_sleep(seconds):
    return None


def connectors(servers):
    from app.connectors.common import Retrier
    r = lambda: Retrier(sleep=no_sleep)  # noqa: E731
    return {
        "a_google": lambda **kw: GoogleAdsConnector(customer_id="1234567890", access_token=TOKEN, base_url=servers["google"].url, client=new_client(),
                                                    retrier=r(), local_services=True, **kw),
        "a_tiktok": lambda **kw: TikTokConnector(advertiser_id="700123", access_token=TOKEN, base_url=servers["tiktok"].url, client=new_client(), retrier=r(), **kw),
        "b_tiktok": lambda **kw: TikTokConnector(advertiser_id="700999", access_token=TOKEN, base_url=servers["tiktok"].url, client=new_client(), retrier=r(), **kw),
        "a_meta": lambda **kw: MetaConnector(ad_account_id="5550001", access_token=TOKEN, base_url=servers["meta"].url, client=new_client(), sleep=no_sleep, **kw),
        "a_square": lambda **kw: SquareConnector(location_id="LFTM01", access_token=TOKEN, base_url=servers["square"].url, client=new_client(), retrier=r(), **kw),
        "a_qb": lambda **kw: QuickBooksConnector(realm_id="9130350000000001", access_token=TOKEN, base_url=servers["quickbooks"].url, client=new_client(),
                                                 retrier=r(), **kw),
        "a_shop": lambda **kw: ShopmonkeyConnector(currency="USD", access_token=TOKEN, base_url=servers["shopmonkey"].url, client=new_client(), retrier=r(),
                                                   time_zone="America/New_York", **kw),
    }


async def sync(world, servers, key, tenant_key="a", start=START, end=END, **kw):
    db = worker_db()
    await db.connect()
    conn = connectors(servers)[key](**kw)
    try:
        return await SyncRunner(db).run_once(tenant_id=str(world[tenant_key]), connection_id=str(world[key]), connector=conn,
                                             window_start=start, window_end=end, trigger="backfill")
    finally:
        await conn.aclose()
        await db.close()


def sql(query, params=()):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


def stored(connection_id):
    return {(r[0], r[1].day): (r[2], r[3]) for r in sql(
        "select metric_key, observed_on, value_numeric, currency from metric_snapshot where connection_id = %s", (connection_id,))}


@pytest.fixture(scope="module")
def synced(world, servers):
    """Every connection of tenant A (and B's TikTok) synced once over the same week."""
    out = {key: run(sync(world, servers, key)) for key in ("a_google", "a_tiktok", "a_meta", "a_square", "a_qb", "a_shop")}
    out["b_tiktok"] = run(sync(world, servers, "b_tiktok", "b"))
    return out


# ---------------------------------------------------------------- what each sync stores

def test_every_connector_syncs_to_a_succeeded_run_with_its_rows_counted(world, synced):
    for key, out in synced.items():
        assert out.status == "succeeded", (key, out.error_code, out.error_detail)
    assert synced["a_google"].rows_written == 7 * 5 + 7 and synced["a_tiktok"].rows_written == 7 * 4 and synced["a_square"].rows_written == 7 * 3
    assert synced["a_qb"].rows_written == 7 * 3 and synced["a_shop"].rows_written == 7 * 2
    assert sql("select count(*) from sync_run where status = 'succeeded'")[0][0] == 7


def test_google_ads_is_stored_as_integers_with_a_currency_on_the_money_only(world, synced):
    got = stored(world["a_google"])
    assert got[("ad_spend", 1)] == (4100, "USD") and got[("ad_conversions", 1)] == (1500, None) and got[("ad_conversions", 2)] == (2000, None)
    assert got[("ad_conversion_value", 1)] == (10100, "USD") and got[("ad_clicks", 3)] == (23, None)
    assert [got[("ad_leads", d)][0] for d in (1, 2, 3, 4, 5, 6, 7)] == [0, 2, 0, 0, 1, 0, 0]
    assert {k[0]: v[1] for k, v in got.items() if v[1]} == {"ad_spend": "USD", "ad_conversion_value": "USD"}
    assert sql("select raw from metric_snapshot where connection_id = %s limit 1", (world["a_google"],))[0][0]["source"] == "search"


def test_tiktok_square_quickbooks_and_shopmonkey_are_stored_in_the_documented_units(world, synced):
    tiktok = stored(world["a_tiktok"])
    assert tiktok[("ad_spend", 1)] == (1625, "USD") and tiktok[("ad_conversions", 2)] == (2000, None) and tiktok[("ad_conversions", 3)] == (0, None)
    square = stored(world["a_square"])
    assert square[("payments", 1)] == (1, None) and square[("payments_total", 1)] == (5000, "USD") and square[("payments_refunded", 1)] == (500, "USD")
    assert square[("payments", 3)] == (1, None) and square[("payments_total", 2)] == (0, "USD"), "the late New York evening payment is on Oct 1, not Oct 2"
    qb = stored(world["a_qb"])
    assert qb[("invoices", 2)] == (1, None) and qb[("invoiced_amount", 2)] == (36207, "USD") and qb[("invoice_open_balance", 2)] == (10000, "USD")
    assert qb[("invoiced_amount", 4)] == (120000, "USD") and qb[("invoices", 3)] == (0, None)
    shop = stored(world["a_shop"])
    assert shop[("work_orders", 1)] == (1, None) and shop[("work_orders", 2)] == (2, None)
    assert shop[("work_order_revenue", 3)] == (123456, "USD") and shop[("work_order_revenue", 1)] == (0, "USD")


def test_syncing_the_same_window_again_rewrites_the_same_rows_and_never_adds_more(world, servers, synced):
    for key in ("a_google", "a_square", "a_shop"):
        before = sql("select count(*) from metric_snapshot where connection_id = %s", (world[key],))[0][0]
        stamp = sql("select max(synced_at) from metric_snapshot where connection_id = %s", (world[key],))[0][0]
        out = run(sync(world, servers, key))
        assert out.status == "succeeded"
        assert sql("select count(*) from metric_snapshot where connection_id = %s", (world[key],))[0][0] == before
        assert sql("select max(synced_at) from metric_snapshot where connection_id = %s", (world[key],))[0][0] > stamp


def test_a_figure_a_platform_restates_replaces_the_stored_one_and_touches_nothing_else(world, servers, synced):
    """A QuickBooks open balance falls as the customer pays, so a later sync restates it."""
    servers["quickbooks"].companies["9130350000000001"]["invoices"][0]["Balance"] = Decimal("25.50")
    try:
        run(sync(world, servers, "a_qb"))
        got = stored(world["a_qb"])
        assert got[("invoice_open_balance", 2)] == (2550, "USD") and got[("invoiced_amount", 2)] == (36207, "USD")
    finally:
        servers["quickbooks"].companies["9130350000000001"]["invoices"][0]["Balance"] = Decimal("100.00")
        run(sync(world, servers, "a_qb"))
    assert stored(world["a_qb"])[("invoice_open_balance", 2)] == (10000, "USD")


# ---------------------------------------------------------------- the database rules migration 013 adds

def test_the_database_refuses_money_without_a_currency_and_a_count_with_one_for_every_new_key(world):
    cid, tenant = world["a_google"], world["a"]

    def insert(metric, currency):
        sql("select set_config('app.tenant_id', %s, false)", (str(tenant),))
        return sql("insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric, currency) "
                   "values (%s, %s, %s, '2026-10-20', 1, %s)", (tenant, cid, metric, currency))

    money = ("ad_conversion_value", "payments_total", "payments_refunded", "invoiced_amount", "invoice_open_balance", "work_order_revenue")
    counts = ("ad_conversions", "ad_leads", "payments", "invoices", "work_orders", "ad_clicks")
    for metric in money:
        insert(metric, "USD")                                                                  # positive control
        sql("delete from metric_snapshot where connection_id = %s and observed_on = '2026-10-20'", (cid,))
        with pytest.raises(psycopg.errors.CheckViolation):
            insert(metric, None)
    for metric in counts:
        insert(metric, None)                                                                   # positive control
        sql("delete from metric_snapshot where connection_id = %s and observed_on = '2026-10-20'", (cid,))
        with pytest.raises(psycopg.errors.CheckViolation):
            insert(metric, "USD")


def test_the_metric_table_knows_the_new_keys_with_their_unit_and_kind_and_applying_the_migration_twice_is_safe(world):
    sql((ROOT / "migrations" / "013_ads_and_business_metrics.sql").read_text(encoding="utf-8"))
    got = {r[0]: (r[1], r[2]) for r in sql("select key, unit, kind from metric where key in "
                                          "('ad_conversions','ad_conversion_value','ad_leads','payments','payments_total','payments_refunded','invoices',"
                                          "'invoiced_amount','invoice_open_balance','work_orders','work_order_revenue')")}
    assert got == {
        "ad_conversions": ("milli_count", "delta"), "ad_conversion_value": ("minor_currency", "delta"), "ad_leads": ("count", "delta"),
        "payments": ("count", "delta"), "payments_total": ("minor_currency", "delta"), "payments_refunded": ("minor_currency", "delta"),
        "invoices": ("count", "delta"), "invoiced_amount": ("minor_currency", "delta"), "invoice_open_balance": ("minor_currency", "gauge"),
        "work_orders": ("count", "delta"), "work_order_revenue": ("minor_currency", "delta")}
    with pytest.raises(psycopg.errors.CheckViolation):
        sql("insert into metric (key, label, unit, kind) values ('bogus', 'Bogus', 'furlongs', 'delta')")
    sql("select 1")


def test_the_older_keys_and_checks_still_hold_after_the_migration(world):
    cid, tenant = world["a_meta"], world["a"]
    for metric, currency in (("gross_sales", None), ("ad_spend", None), ("orders", "USD"), ("sessions", "USD"), ("ad_purchase_value", None)):
        with pytest.raises(psycopg.errors.CheckViolation):
            sql("insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric, currency) "
                "values (%s, %s, %s, '2026-10-21', 1, %s)", (tenant, cid, metric, currency))


# ---------------------------------------------------------------- failures

def test_a_rejected_token_is_a_failed_run_with_its_code_and_writes_nothing(world, servers):
    before = sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_square"],))[0][0]
    out = run(_with_token(world, servers, "wrong-token"))
    assert out.status == "failed" and out.error_code == "auth_expired" and out.rows_written == 0
    assert TOKEN not in (out.error_detail or "") and "wrong-token" not in (out.error_detail or "")
    assert sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_square"],))[0][0] == before


async def _with_token(world, servers, token):
    db = worker_db()
    await db.connect()
    conn = SquareConnector(location_id="LFTM01", access_token=token, base_url=servers["square"].url, client=new_client())
    try:
        return await SyncRunner(db).run_once(tenant_id=str(world["a"]), connection_id=str(world["a_square"]), connector=conn,
                                             window_start=date(2026, 11, 1), window_end=date(2026, 11, 3), trigger="backfill")
    finally:
        await conn.aclose()
        await db.close()


def test_a_square_window_that_fails_part_way_writes_nothing_for_that_window(world, servers):
    """A day's total needs every page, so a failure on page two must not leave page one's half day behind."""
    from app.connectors.square_fake import errors_body
    payment = {"id": "late0", "created_at": "2026-10-20T15:00:00Z", "status": "COMPLETED", "location_id": "LFTM01",
               "total_money": {"amount": 100, "currency": "USD"}}
    location = {"id": "LFTM01", "status": "ACTIVE", "timezone": "America/New_York", "currency": "USD"}
    servers["square"].faults[:] = [Fault(200, {"locations": [location]}), Fault(200, {"payments": [payment], "cursor": "C1"}),
                                   Fault(403, errors_body("AUTHENTICATION_ERROR", "INSUFFICIENT_SCOPES"))]
    out = run(sync(world, servers, "a_square", start=date(2026, 10, 20), end=date(2026, 10, 22)))
    assert out.status == "failed" and out.error_code == "auth_revoked" and out.rows_written == 0
    assert sql("select count(*) from metric_snapshot where connection_id = %s and observed_on >= '2026-10-20'", (world["a_square"],))[0][0] == 0


def test_a_second_currency_on_a_later_run_of_tiktok_fails_the_run_but_keeps_the_first_run_rows(world, servers):
    first = {"page_info": {"total_page": 1}, "list": [{"dimensions": {"advertiser_id": "700123", "stat_time_day": "2026-08-02 00:00:00"},
                                                       "metrics": {"spend": "1.00", "impressions": "10", "clicks": "1", "conversion": "1", "currency": "USD"}}]}
    second = {"page_info": {"total_page": 1}, "list": [{"dimensions": {"advertiser_id": "700123", "stat_time_day": "2026-09-05 00:00:00"},
                                                        "metrics": {"spend": "1.00", "impressions": "10", "clicks": "1", "conversion": "1", "currency": "EUR"}}]}
    from app.connectors.tiktok_fake import envelope
    servers["tiktok"].faults[:] = [Fault(200, envelope(0, "OK", first)), Fault(200, envelope(0, "OK", second))]
    out = run(sync(world, servers, "a_tiktok", start=date(2026, 8, 1), end=date(2026, 9, 20)))
    assert out.status == "failed" and out.error_code == "upstream_broken" and out.rows_written == 4
    assert sql("select count(*) from metric_snapshot where connection_id = %s and observed_on = '2026-09-05'", (world["a_tiktok"],))[0][0] == 0
    assert sql("select resume_cursor from sync_run where connection_id = %s and window_start = '2026-08-01'", (world["a_tiktok"],))[0][0] == "2026-08-31:1"
    sql("delete from metric_snapshot where connection_id = %s and observed_on < '2026-10-01'", (world["a_tiktok"],))


def test_a_failed_run_is_not_coverage(world):
    covered = sql("select count(*) from sync_run where connection_id = %s and status in ('succeeded', 'partial')", (world["a_tiktok"],))[0][0]
    failed = sql("select count(*) from sync_run where connection_id = %s and status = 'failed'", (world["a_tiktok"],))[0][0]
    assert covered >= 1 and failed >= 1


# ---------------------------------------------------------------- row level security

def test_row_level_security_keeps_each_tenants_rows_apart(world, synced):
    async def read(tenant):
        db = request_db()
        await db.connect()
        try:
            async with db.tenant(str(tenant)) as conn:
                return await conn.fetch("select distinct connection_id from metric_snapshot")
        finally:
            await db.close()

    a_rows, b_rows, c_rows = run(read(world["a"])), run(read(world["b"])), run(read(world["c"]))
    assert {str(r["connection_id"]) for r in a_rows} == {str(world[k]) for k in ("a_google", "a_tiktok", "a_meta", "a_square", "a_qb", "a_shop")}
    assert {str(r["connection_id"]) for r in b_rows} == {str(world["b_tiktok"])} and c_rows == []


# ---------------------------------------------------------------- the combined view, from what was stored

def test_the_combined_view_reads_every_ad_connection_and_the_shops_booked_jobs_from_the_stored_rows(world, synced):
    async def go(tenant):
        db = request_db()
        await db.connect()
        try:
            return await load_view(db, str(tenant), START, END)
        finally:
            await db.close()

    view = run(go(world["a"]))
    by_label = {p.label: p for p in view.platforms}
    assert set(by_label) == {"Fort Myers Google", "Fort Myers TikTok", "Fort Myers Meta"} and view.booked_jobs == 3
    google = by_label["Fort Myers Google"]
    spend = sum(40_000_000 + d * 1_000_000 for d in range(1, 8)) // 10_000
    assert google.spend == spend and google.conversions == Decimal("1.5") * 4 + Decimal(2) * 3
    assert google.cost_per_conversion == Decimal(spend) / google.conversions and google.currency == "USD"
    meta = by_label["Fort Myers Meta"]
    assert meta.conversions == Decimal(sum(d % 2 for d in range(1, 8))) and meta.conversion_days == 7
    tiktok = by_label["Fort Myers TikTok"]
    assert tiktok.conversions == Decimal(sum(d % 3 for d in range(1, 8))) and tiktok.days == 7
    assert len(view.totals) == 1 and view.totals[0].currency == "USD" and view.totals[0].spend == google.spend + meta.spend + tiktok.spend
    assert sum(w.booked_jobs for w in view.weeks) == 3 and view.weeks[0].start == START and view.weeks[-1].end == END
    other = run(go(world["b"]))
    assert [p.label for p in other.platforms] == ["Other tenant TikTok"] and other.platforms[0].currency == "EUR" and other.booked_jobs is None


def test_a_tenant_with_no_ad_connection_gets_a_reason_not_an_empty_view(world, synced):
    async def go():
        db = request_db()
        await db.connect()
        try:
            return await load_view(db, str(world["c"]), START, END)
        finally:
            await db.close()

    with pytest.raises(ReportError, match="no active ad connection"):
        run(go())


# ---------------------------------------------------------------- the API call

@pytest.fixture(scope="module")
def client(world, synced):
    os.environ["DATABASE_URL"] = DSN
    os.environ.setdefault("DASHBOARD_ORIGIN", "http://localhost:3000")
    from contextlib import AsyncExitStack

    from app.api.main import app as fastapi_app

    loop = asyncio.new_event_loop()
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test", timeout=60)
    stack = AsyncExitStack()
    loop.run_until_complete(stack.enter_async_context(fastapi_app.router.lifespan_context(fastapi_app)))
    yield loop, c
    loop.run_until_complete(stack.aclose())
    loop.run_until_complete(c.aclose())
    loop.close()


def call(client, url, key=None, **params):
    loop, c = client
    headers = {"Authorization": "Bearer %s" % key} if key else {}
    return loop.run_until_complete(c.get(url, headers=headers, params=params))


def test_the_ad_spend_call_answers_a_viewer_with_the_view_and_only_that_tenants_rows(world, client):
    response = call(client, "/api/reports/ad-spend", world["keys"]["a_viewer"], start="2026-10-01", end="2026-10-08")
    assert response.status_code == 200
    body = response.json()
    assert [p["label"] for p in body["platforms"]] == ["Fort Myers Google", "Fort Myers Meta", "Fort Myers TikTok"]
    assert body["booked_jobs"] == 3 and body["totals"][0]["currency"] == "USD" and len(body["weeks"]) == 2
    assert all(p["currency"] == "USD" for p in body["platforms"]) and "9999" not in response.text
    other = call(client, "/api/reports/ad-spend", world["keys"]["b_admin"], start="2026-10-01", end="2026-10-08").json()
    assert [p["label"] for p in other["platforms"]] == ["Other tenant TikTok"] and other["platforms"][0]["currency"] == "EUR"


def test_the_ad_spend_call_refuses_a_missing_key_a_bad_window_and_a_tenant_with_nothing_to_report(world, client):
    assert call(client, "/api/reports/ad-spend", None, start="2026-10-01", end="2026-10-08").status_code == 401
    key = world["keys"]["a_viewer"]
    assert call(client, "/api/reports/ad-spend", key, start="2026-10-08", end="2026-10-01").status_code == 400
    assert call(client, "/api/reports/ad-spend", key, start="2025-01-01", end="2026-10-01").status_code == 400
    assert call(client, "/api/reports/ad-spend", key, start="soon", end="2026-10-01").status_code == 422
    empty = call(client, "/api/reports/ad-spend", world["keys"]["c_admin"], start="2026-10-01", end="2026-10-08")
    assert empty.status_code == 422 and "no active ad connection" in empty.json()["detail"]
