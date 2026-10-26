"""Meta through the real sync runner into real Postgres, then read back as the request role.

The whole path, against the stand-in server (never a live ad account): MetaConnector over HTTP, the SyncRunner
as the sync_worker role, the dated snapshots, and row level security as the request role. It also proves what
only a database can: the currency check knows the two new money metrics and refuses the wrong shapes, a second
sync rewrites the same rows instead of adding more, a purchase figure Meta restates replaces the stored one, a
failed run is recorded with its code and is not coverage, and one tenant never sees another's ad rows.

Skips cleanly when Postgres is not running (`docker compose up -d`). Like the other database suites it drops
and recreates the `public` schema of the database it points at.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
pytest.importorskip("asyncpg")

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@localhost:5433/analytics")


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="Postgres not reachable, run docker compose up -d")

import httpx  # noqa: E402

from app.connectors.meta import MetaConnector  # noqa: E402
from app.connectors.meta_fake import Fault, FakeMeta, error_body  # noqa: E402
from app.db import Database  # noqa: E402
from app.sync.runner import SyncRunner  # noqa: E402

TOKEN = "tok-meta-db-SECRET"
ACCOUNT = "777001"
OTHER_ACCOUNT = "777002"

DAYS = {
    date(2026, 10, 1): {"spend": "100.00", "impressions": 10000, "clicks": 250, "purchases": 5, "purchase_value": "420.50"},
    date(2026, 10, 2): {"spend": "80.25", "impressions": 8000, "clicks": 200, "purchases": None, "purchase_value": None},
}
MIGRATIONS = ("001_init.sql", "002_control_plane.sql", "006_ga4_metrics.sql", "007_meta_metrics.sql")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def server():
    with FakeMeta(token=TOKEN) as fake:
        yield fake


def connect_row(cur, tenant, platform, account, name):
    cur.execute(
        "insert into platform_connection (tenant_id, platform, external_account, display_name, credentials_enc) "
        "values (%s, %s, %s, %s, %s) returning id", (tenant, platform, account, name, b"ciphertext"))
    return cur.fetchone()[0]


@pytest.fixture(scope="module")
def world(server):
    a, b = uuid.uuid4(), uuid.uuid4()
    ids = {}
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade; create schema public")
        for name in MIGRATIONS:
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))
        cur.execute("grant usage on schema public to sync_worker")
        for tenant, label in ((a, "tenant-a"), (b, "tenant-b")):
            cur.execute("select set_config('app.tenant_id', %s, false)", (str(tenant),))
            cur.execute("insert into tenant (id, slug, name) values (%s, %s, %s)", (tenant, label, label))
        cur.execute("select set_config('app.tenant_id', %s, false)", (str(a),))
        ids["a_meta"] = connect_row(cur, a, "meta", "act_" + ACCOUNT, "tenant-a ads")
        cur.execute("select set_config('app.tenant_id', %s, false)", (str(b),))
        ids["b_meta"] = connect_row(cur, b, "meta", "act_" + OTHER_ACCOUNT, "tenant-b ads")
    server.set_account(ACCOUNT, DAYS, currency="EUR")
    server.set_account(OTHER_ACCOUNT, {date(2026, 10, 1): {"spend": "9999.00", "impressions": 1, "clicks": 1,
                                                           "purchases": 1, "purchase_value": "9999.00"}}, currency="USD")
    return {"a": a, "b": b, **ids}


def worker_db():
    return Database(DSN, max_size=2, role="sync_worker")


def request_db():
    return Database(DSN, max_size=2)


def connector(server, account=ACCOUNT, token=TOKEN, **kw):
    client = httpx.AsyncClient(trust_env=False, verify=False, timeout=10)

    async def no_sleep(seconds):
        return None

    kw.setdefault("sleep", no_sleep)
    return MetaConnector(ad_account_id=account, access_token=token, base_url=server.url, client=client, **kw)


async def sync(world, key, tenant_key, server, start, end, account=ACCOUNT, **kw):
    db = worker_db()
    await db.connect()
    conn = connector(server, account, **kw)
    try:
        return await SyncRunner(db).run_once(tenant_id=str(world[tenant_key]), connection_id=str(world[key]),
                                             connector=conn, window_start=start, window_end=end, trigger="backfill")
    finally:
        await conn.aclose()
        await db.close()


def sql(query, params=()):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


def snapshot_count(connection_id):
    return sql("select count(*) from metric_snapshot where connection_id = %s", (connection_id,))[0][0]


def test_a_sync_stores_the_ad_metrics_as_integers_with_a_currency_on_the_two_money_ones_only(world, server):
    out = run(sync(world, "a_meta", "a", server, date(2026, 10, 1), date(2026, 10, 3)))
    assert out.status == "succeeded" and out.rows_written == 8 and out.api_calls == 1
    rows = sql("select metric_key, observed_on, value_numeric, currency from metric_snapshot "
               "where connection_id = %s order by observed_on, metric_key", (world["a_meta"],))
    got = {(r[0], r[1].day): (r[2], r[3]) for r in rows}
    assert got[("ad_spend", 1)] == (10000, "EUR") and got[("ad_purchase_value", 1)] == (42050, "EUR")
    assert got[("ad_impressions", 1)] == (10000, None) and got[("ad_clicks", 1)] == (250, None) and got[("ad_purchases", 1)] == (5, None)
    assert got[("ad_spend", 2)] == (8025, "EUR") and ("ad_purchases", 2) not in got, "a day with no actions has no purchase row"
    assert sql("select status, rows_written, error_code from sync_run where connection_id = %s", (world["a_meta"],)) == [
        ("succeeded", 8, None)]
    raw = sql("select raw from metric_snapshot where connection_id = %s limit 1", (world["a_meta"],))[0][0]
    assert raw == {"source": "insights", "attribution_window": "7d_click"}


def test_syncing_the_same_window_again_rewrites_the_same_rows_and_never_adds_more(world, server):
    before = snapshot_count(world["a_meta"])
    stamp = sql("select max(synced_at) from metric_snapshot where connection_id = %s", (world["a_meta"],))[0][0]
    out = run(sync(world, "a_meta", "a", server, date(2026, 10, 1), date(2026, 10, 3)))
    assert out.status == "succeeded" and out.rows_written == 8
    assert snapshot_count(world["a_meta"]) == before == 8
    assert sql("select max(synced_at) from metric_snapshot where connection_id = %s", (world["a_meta"],))[0][0] > stamp


def test_a_purchase_figure_meta_restates_replaces_the_stored_one_and_touches_nothing_else(world, server):
    """Conversions inside the click window can still arrive after the day is over, so a later sync restates them."""
    restated = {**DAYS, date(2026, 10, 1): {**DAYS[date(2026, 10, 1)], "purchases": 9, "purchase_value": "700.00"}}
    server.set_account(ACCOUNT, restated, currency="EUR")
    try:
        run(sync(world, "a_meta", "a", server, date(2026, 10, 1), date(2026, 10, 3)))
        got = dict(sql("select metric_key, value_numeric from metric_snapshot where connection_id = %s "
                       "and observed_on = '2026-10-01'", (world["a_meta"],)))
        assert got["ad_purchases"] == 9 and got["ad_purchase_value"] == 70000 and got["ad_spend"] == 10000
        assert snapshot_count(world["a_meta"]) == 8
    finally:
        server.set_account(ACCOUNT, DAYS, currency="EUR")
        run(sync(world, "a_meta", "a", server, date(2026, 10, 1), date(2026, 10, 3)))
    assert dict(sql("select metric_key, value_numeric from metric_snapshot where connection_id = %s and observed_on = '2026-10-01'",
                    (world["a_meta"],)))["ad_purchases"] == 5


def test_the_database_refuses_money_without_a_currency_and_a_count_with_one(world):
    cid, tenant = world["a_meta"], world["a"]

    def insert(metric, value, currency):
        sql("select set_config('app.tenant_id', %s, false)", (str(tenant),))
        return sql("insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric, currency) "
                   "values (%s, %s, %s, '2026-10-20', %s, %s)", (tenant, cid, metric, value, currency))

    insert("ad_spend", 1, "EUR")                                                           # positive control
    for metric, currency in (("ad_spend", None), ("ad_purchase_value", None), ("ad_clicks", "EUR"), ("ad_impressions", "EUR"),
                             ("ad_purchases", "USD")):
        with pytest.raises(psycopg.errors.CheckViolation):
            insert(metric, 1, currency)
    sql("delete from metric_snapshot where connection_id = %s and observed_on = '2026-10-20'", (cid,))


def test_the_replaced_check_still_holds_for_the_store_money_metrics_and_applying_it_twice_is_safe(world):
    cid, tenant = world["a_meta"], world["a"]
    for metric, currency in (("gross_sales", None), ("net_sales", None), ("orders", "USD"), ("sessions", "USD")):
        with pytest.raises(psycopg.errors.CheckViolation):
            sql("insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric, currency) "
                "values (%s, %s, %s, '2026-10-21', 1, %s)", (tenant, cid, metric, currency))
    sql((ROOT / "migrations" / "007_meta_metrics.sql").read_text(encoding="utf-8"))
    assert sql("select count(*) from metric where key like 'ad\\_%%'")[0][0] == 5


def test_a_rejected_token_is_a_failed_run_with_its_code_and_writes_nothing(world, server):
    before = snapshot_count(world["a_meta"])
    bad = run(sync(world, "a_meta", "a", server, date(2026, 11, 1), date(2026, 11, 3), token="wrong"))
    assert bad.status == "failed" and bad.error_code == "auth_expired" and bad.rows_written == 0
    assert "wrong" not in (bad.error_detail or "") and TOKEN not in (bad.error_detail or "")
    assert snapshot_count(world["a_meta"]) == before


def test_an_error_object_inside_a_200_fails_the_run_as_an_expired_token_not_a_crash_or_an_empty_success(world, server):
    server.faults.append(Fault(200, error_body(190, "OAuthException", "Error validating access token: Session has expired")))
    out = run(sync(world, "a_meta", "a", server, date(2026, 10, 1), date(2026, 10, 3)))
    assert out.status == "failed" and out.error_code == "auth_expired"
    assert sql("select status, error_code from sync_run where connection_id = %s and status = 'failed' order by started_at desc limit 1",
               (world["a_meta"],)) == [("failed", "auth_expired")]


def test_a_failed_run_is_not_coverage_and_a_run_that_found_no_ads_is(world, server):
    covered = sql("select window_start, window_end from sync_run where connection_id = %s and status in ('succeeded', 'partial')",
                  (world["a_meta"],))
    failed = sql("select window_start, window_end from sync_run where connection_id = %s and status = 'failed'", (world["a_meta"],))
    assert covered and failed
    server.set_account(ACCOUNT, {}, currency="EUR")
    try:
        quiet = run(sync(world, "a_meta", "a", server, date(2026, 10, 10), date(2026, 10, 12)))
        assert quiet.status == "succeeded" and quiet.rows_written == 0
    finally:
        server.set_account(ACCOUNT, DAYS, currency="EUR")
    assert (date(2026, 10, 10), date(2026, 10, 12)) in sql(
        "select window_start, window_end from sync_run where connection_id = %s and status = 'succeeded'", (world["a_meta"],))


def test_a_second_currency_on_a_later_page_fails_the_run_but_keeps_the_first_page_and_its_cursor(world, server):
    first = {"data": [{"account_currency": "EUR", "spend": "1.00", "impressions": "10", "clicks": "1",
                       "date_start": "2026-10-20", "date_stop": "2026-10-20"}],
             "paging": {"cursors": {"after": "CUR1"}, "next": "x"}}
    second = {"data": [{"account_currency": "USD", "spend": "1.00", "impressions": "10", "clicks": "1",
                        "date_start": "2026-10-21", "date_stop": "2026-10-21"}]}
    server.faults.extend([Fault(200, first), Fault(200, second)])
    out = run(sync(world, "a_meta", "a", server, date(2026, 10, 20), date(2026, 10, 22)))
    assert out.status == "failed" and out.error_code == "upstream_broken" and out.rows_written == 3
    assert sql("select count(*) from metric_snapshot where connection_id = %s and observed_on = '2026-10-21'", (world["a_meta"],))[0][0] == 0
    assert sql("select resume_cursor from sync_run where connection_id = %s and window_start = '2026-10-20'", (world["a_meta"],))[0][0] == "CUR1"
    sql("delete from metric_snapshot where connection_id = %s and observed_on >= '2026-10-20'", (world["a_meta"],))


def test_row_level_security_keeps_each_tenants_ad_rows_apart(world, server):
    run(sync(world, "b_meta", "b", server, date(2026, 10, 1), date(2026, 10, 2), account=OTHER_ACCOUNT))

    async def read(tenant):
        db = request_db()
        await db.connect()
        try:
            async with db.tenant(str(tenant)) as conn:
                return await conn.fetch("select connection_id, metric_key, value_numeric, currency from metric_snapshot "
                                        "where metric_key like 'ad\\_%'")
        finally:
            await db.close()

    a_rows, b_rows = run(read(world["a"])), run(read(world["b"]))
    assert a_rows and b_rows
    assert {str(r["connection_id"]) for r in a_rows} == {str(world["a_meta"])}
    assert {str(r["connection_id"]) for r in b_rows} == {str(world["b_meta"])}
    assert all(r["value_numeric"] != 999900 for r in a_rows) and {r["currency"] for r in b_rows if r["currency"]} == {"USD"}


def test_the_ad_metrics_are_registered_with_their_unit_and_kind_so_nothing_sums_or_charts_them_wrongly(world):
    got = {r[0]: (r[1], r[2]) for r in sql("select key, unit, kind from metric where key in "
                                          "('ad_spend', 'ad_impressions', 'ad_clicks', 'ad_purchases', 'ad_purchase_value')")}
    assert got == {"ad_spend": ("minor_currency", "delta"), "ad_impressions": ("count", "delta"), "ad_clicks": ("count", "delta"),
                   "ad_purchases": ("count", "delta"), "ad_purchase_value": ("minor_currency", "delta")}
