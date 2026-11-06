"""The Google Business Profile connector through the real sync runner into real Postgres, then read back as the request role.

The whole path, against the stand-in server (never a live listing): the connector over HTTP, the SyncRunner as the sync_worker role, the dated snapshots, and
row level security as the request role. It also proves what only a database can: migration 014 teaches the metric table the six listing keys, a count is stored
with no currency (the currency check still refuses one), a second sync rewrites the same rows, a figure Google restates replaces the stored one, a rejected
token is a failed run with its code that writes nothing, and one tenant never sees another's rows.

Skips cleanly when Postgres is not running (`docker compose up -d`); CI sets ADS_REQUIRE_DB=1 so a Postgres that did not start fails the suite instead.
Like the other database suites it drops and recreates the `public` schema of the database it points at.
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

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


if os.environ.get("ADS_REQUIRE_DB") == "1" and not _reachable():
    raise RuntimeError("ADS_REQUIRE_DB is set but Postgres is not reachable at %s" % DSN)

pytestmark = pytest.mark.skipif(not _reachable(), reason="Postgres not reachable, run docker compose up -d")

import httpx  # noqa: E402

from app.connectors.common import Retrier  # noqa: E402
from app.connectors.fake_http import Fault  # noqa: E402
from app.connectors.gbp import GBPConnector  # noqa: E402
from app.connectors.gbp_fake import FakeGBP  # noqa: E402
from app.db import Database  # noqa: E402
from app.sync.runner import SyncRunner  # noqa: E402

TOKEN = "tok-gbp-db-SECRET"
LOCATION = "5550001112223334"
START, END = date(2026, 10, 1), date(2026, 10, 8)
MIGRATIONS = ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql", "006_ga4_metrics.sql", "007_meta_metrics.sql", "013_ads_and_business_metrics.sql",
              "014_business_profile_metrics.sql")


def run(coro):
    return asyncio.run(coro)


def sql(query, params=()):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


async def no_sleep(seconds):
    return None


@pytest.fixture(scope="module")
def server():
    with FakeGBP(token=TOKEN) as fake:
        fake.set_location(LOCATION)
        for d in range(1, 8):
            day = date(2026, 10, d)
            fake.set_series(LOCATION, "BUSINESS_IMPRESSIONS_MOBILE_SEARCH", {day: 100 + d})
            fake.set_series(LOCATION, "BUSINESS_IMPRESSIONS_DESKTOP_MAPS", {day: 10})
            fake.set_series(LOCATION, "CALL_CLICKS", {day: d % 3})
        fake.set_series(LOCATION, "BUSINESS_BOOKINGS", {date(2026, 10, 4): 2})
        yield fake


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
        for tenant, key in ((a, "a_gbp"), (b, "b_other")):
            cur.execute("select set_config('app.tenant_id', %s, false)", (str(tenant),))
            cur.execute(
                "insert into platform_connection (tenant_id, platform, external_account, display_name, credentials_enc) "
                "values (%s, 'gbp', %s, %s, %s) returning id", (tenant, LOCATION if key == "a_gbp" else "999", key, b"ciphertext"))
            ids[key] = cur.fetchone()[0]
    return {"a": a, "b": b, **ids}


def connector(server, token=TOKEN):
    return GBPConnector(location_id=LOCATION, access_token=token, base_url=server.url,
                        client=httpx.AsyncClient(trust_env=False, verify=False, timeout=10), retrier=Retrier(sleep=no_sleep))


async def sync(world, server, start=START, end=END, token=TOKEN):
    db = Database(DSN, max_size=2, role="sync_worker")
    await db.connect()
    conn = connector(server, token)
    try:
        return await SyncRunner(db).run_once(tenant_id=str(world["a"]), connection_id=str(world["a_gbp"]), connector=conn,
                                             window_start=start, window_end=end, trigger="backfill")
    finally:
        await conn.aclose()
        await db.close()


def stored(connection_id):
    return {(r[0], r[1].day): (r[2], r[3]) for r in sql(
        "select metric_key, observed_on, value_numeric, currency from metric_snapshot where connection_id = %s", (connection_id,))}


@pytest.fixture(scope="module")
def synced(world, server):
    return run(sync(world, server))


def test_the_sync_succeeds_and_writes_six_counts_for_each_of_seven_days(world, synced):
    assert synced.status == "succeeded", (synced.error_code, synced.error_detail)
    assert synced.rows_written == 6 * 7 and len(stored(world["a_gbp"])) == 6 * 7


def test_counts_are_stored_with_no_currency_and_impressions_are_the_surfaces_added(world, synced):
    got = stored(world["a_gbp"])
    assert got[("gbp_impressions", 1)] == (111, None) and got[("gbp_impressions", 7)] == (117, None)
    assert got[("gbp_calls", 2)] == (2, None) and got[("gbp_calls", 3)] == (0, None)
    assert got[("gbp_bookings", 4)] == (2, None) and got[("gbp_bookings", 5)] == (0, None)
    assert all(currency is None for _, currency in got.values())


def test_the_currency_check_still_refuses_a_currency_on_a_listing_count(world, synced):
    with pytest.raises(psycopg.errors.CheckViolation):
        sql("insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric, currency) "
            "values (%s, %s, 'gbp_calls', '2026-10-21', 1, 'USD')", (world["a"], world["a_gbp"]))


def test_the_six_keys_are_in_the_metric_table_as_delta_counts(world, synced):
    rows = sql("select key, unit, kind from metric where left(key, 4) = 'gbp_' order by key")
    assert [r[0] for r in rows] == sorted(["gbp_impressions", "gbp_calls", "gbp_website_clicks", "gbp_direction_requests", "gbp_messages", "gbp_bookings"])
    assert {(r[1], r[2]) for r in rows} == {("count", "delta")}


def test_a_second_sync_rewrites_the_same_rows_and_a_restated_figure_replaces_the_stored_one(world, server, synced):
    before = sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_gbp"],))[0][0]
    server.set_series(LOCATION, "CALL_CLICKS", {date(2026, 10, 2): 9})        # Google restates a day
    out = run(sync(world, server))
    assert out.status == "succeeded"
    assert sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_gbp"],))[0][0] == before
    assert stored(world["a_gbp"])[("gbp_calls", 2)] == (9, None)


def test_a_rejected_token_is_a_failed_run_with_its_code_and_writes_nothing(world, server, synced):
    before = sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_gbp"],))[0][0]
    out = run(sync(world, server, start=date(2026, 11, 1), end=date(2026, 11, 3), token="wrong-token"))
    assert out.status == "failed" and out.error_code == "auth_expired" and out.rows_written == 0
    assert TOKEN not in (out.error_detail or "") and "wrong-token" not in (out.error_detail or "")
    assert sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_gbp"],))[0][0] == before


def test_an_answer_with_a_breakdown_nobody_asked_for_fails_the_run_and_writes_nothing_for_that_window(world, server, synced):
    body = {"multiDailyMetricTimeSeries": [{"dailyMetricTimeSeries": [
        {"dailyMetric": "CALL_CLICKS", "dailySubEntityType": {"dayOfWeek": "MONDAY"}, "timeSeries": {"datedValues": []}}]}]}
    server.faults[:] = [Fault(200, body)]
    out = run(sync(world, server, start=date(2026, 10, 20), end=date(2026, 10, 22)))
    assert out.status == "failed" and out.error_code == "upstream_broken" and out.rows_written == 0
    assert sql("select count(*) from metric_snapshot where connection_id = %s and observed_on >= '2026-10-20'", (world["a_gbp"],))[0][0] == 0


def test_row_level_security_keeps_each_tenants_rows_apart(world, synced):
    async def read(tenant):
        db = Database(DSN, max_size=2)
        await db.connect()
        try:
            async with db.tenant(str(tenant)) as conn:
                return await conn.fetch("select distinct connection_id from metric_snapshot")
        finally:
            await db.close()

    assert {str(r["connection_id"]) for r in run(read(world["a"]))} == {str(world["a_gbp"])}
    assert run(read(world["b"])) == []
