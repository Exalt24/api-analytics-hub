"""The Salesforce connector through the real sync runner into real Postgres, then read back as the request role.

The whole path, against the stand-in server (never a live org): the connector over HTTP (token exchange, describes, four queries), the SyncRunner as the sync_worker
role, the dated snapshots, and row level security as the request role. It also proves what only a database can: migration 017 teaches the metric table the five keys
and the currency check the one money key, counts are stored with no currency and the won amount with one, a second sync rewrites the same rows, a figure Salesforce
restates replaces the stored one, a refused refresh token is a failed run with its code that writes nothing, and one tenant never sees another's rows.

Skips cleanly when Postgres is not running (`docker compose up -d`); CI sets ADS_REQUIRE_DB=1 so a Postgres that did not start fails the suite instead.
Like the other database suites it drops and recreates the `public` schema of the database it points at.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import date, datetime, timezone
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


if os.environ.get("ADS_REQUIRE_DB") == "1" and not _reachable():
    raise RuntimeError("ADS_REQUIRE_DB is set but Postgres is not reachable at %s" % DSN)

pytestmark = pytest.mark.skipif(not _reachable(), reason="Postgres not reachable, run docker compose up -d")

import httpx  # noqa: E402

from app.connectors.common import Retrier  # noqa: E402
from app.connectors.salesforce import SalesforceConnector  # noqa: E402
from app.connectors.salesforce_fake import FakeSalesforce  # noqa: E402
from app.db import Database  # noqa: E402
from app.sync.runner import SyncRunner  # noqa: E402

CLIENT_ID, SECRET, REFRESH = "3MVG9-db-client", "db-consumer-SECRET", "db-refresh-SECRET"
START, END = date(2026, 10, 1), date(2026, 10, 8)
MIGRATIONS = ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql", "006_ga4_metrics.sql", "007_meta_metrics.sql", "013_ads_and_business_metrics.sql",
              "017_salesforce_metrics.sql")
KEYS = ("sf_leads_created", "sf_leads_converted", "sf_opportunities_created", "sf_opportunities_won", "sf_won_amount")


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
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH) as fake:
        for d in range(1, 8):
            fake.add_lead(d, datetime(2026, 10, d, 9, tzinfo=timezone.utc), converted_on=date(2026, 10, d) if d % 3 == 0 else None)
            fake.add_lead(100 + d, datetime(2026, 10, d, 15, tzinfo=timezone.utc))
        fake.add_opportunity(1, datetime(2026, 10, 2, 9, tzinfo=timezone.utc), close=date(2026, 10, 4), won=True, amount=Decimal("1500.25"))
        fake.add_opportunity(2, datetime(2026, 10, 3, 9, tzinfo=timezone.utc), close=date(2026, 10, 4), won=True, amount=Decimal("99.75"))
        fake.add_opportunity(3, datetime(2026, 10, 3, 9, tzinfo=timezone.utc), close=date(2026, 10, 5), won=False, amount=Decimal("5000"))
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
        for tenant, key in ((a, "a_sf"), (b, "b_other")):
            cur.execute("select set_config('app.tenant_id', %s, false)", (str(tenant),))
            cur.execute(
                "insert into platform_connection (tenant_id, platform, external_account, display_name, credentials_enc) "
                "values (%s, 'salesforce', %s, %s, %s) returning id", (tenant, "127.0.0.1" if key == "a_sf" else "other.my.salesforce.com", key, b"ciphertext"))
            ids[key] = cur.fetchone()[0]
    return {"a": a, "b": b, **ids}


def connector(server, refresh=REFRESH):
    return SalesforceConnector(instance_url=server.url, currency="USD", client_id=CLIENT_ID, client_secret=SECRET, refresh_token=refresh, login_url=server.url,
                               client=httpx.AsyncClient(trust_env=False, verify=False, timeout=10), retrier=Retrier(sleep=no_sleep))


async def sync(world, server, start=START, end=END, refresh=REFRESH):
    db = Database(DSN, max_size=2, role="sync_worker")
    await db.connect()
    conn = connector(server, refresh)
    try:
        return await SyncRunner(db).run_once(tenant_id=str(world["a"]), connection_id=str(world["a_sf"]), connector=conn,
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


def test_the_sync_succeeds_and_writes_five_figures_for_each_of_seven_days(world, synced):
    assert synced.status == "succeeded", (synced.error_code, synced.error_detail)
    assert synced.rows_written == 5 * 7 and len(stored(world["a_sf"])) == 5 * 7


def test_counts_have_no_currency_and_the_won_amount_has_one(world, synced):
    got = stored(world["a_sf"])
    assert got[("sf_leads_created", 1)] == (2, None) and got[("sf_leads_converted", 3)] == (1, None) and got[("sf_leads_converted", 4)] == (0, None)
    assert got[("sf_opportunities_created", 3)] == (2, None) and got[("sf_opportunities_won", 4)] == (2, None) and got[("sf_opportunities_won", 5)] == (0, None)
    assert got[("sf_won_amount", 4)] == (160000, "USD") and got[("sf_won_amount", 5)] == (0, "USD")
    assert {c for (k, _), (_, c) in got.items() if k != "sf_won_amount"} == {None}


def test_the_currency_check_refuses_a_currency_on_a_count_and_demands_one_on_the_amount(world, synced):
    with pytest.raises(psycopg.errors.CheckViolation):
        sql("insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric, currency) "
            "values (%s, %s, 'sf_leads_created', '2026-10-21', 1, 'USD')", (world["a"], world["a_sf"]))
    with pytest.raises(psycopg.errors.CheckViolation):
        sql("insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric) "
            "values (%s, %s, 'sf_won_amount', '2026-10-21', 1)", (world["a"], world["a_sf"]))


def test_the_five_keys_are_in_the_metric_table(world, synced):
    rows = sql("select key, unit, kind from metric where left(key, 3) = 'sf_' order by key")
    assert [r[0] for r in rows] == sorted(KEYS)
    assert {r[0]: r[1] for r in rows}["sf_won_amount"] == "minor_currency" and {r[1] for r in rows if r[0] != "sf_won_amount"} == {"count"}


def test_a_second_sync_rewrites_the_same_rows_and_a_restated_figure_replaces_the_stored_one(world, server, synced):
    before = sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_sf"],))[0][0]
    server.add_opportunity(9, datetime(2026, 10, 4, 9, tzinfo=timezone.utc), close=date(2026, 10, 4), won=True, amount=Decimal("100"))
    out = run(sync(world, server))
    assert out.status == "succeeded"
    assert sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_sf"],))[0][0] == before
    assert stored(world["a_sf"])[("sf_won_amount", 4)] == (170000, "USD") and stored(world["a_sf"])[("sf_opportunities_won", 4)] == (3, None)


def test_a_refused_refresh_token_is_a_failed_run_with_its_code_and_writes_nothing(world, server, synced):
    before = sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_sf"],))[0][0]
    out = run(sync(world, server, start=date(2026, 11, 1), end=date(2026, 11, 3), refresh="nobody-issued-this"))
    assert out.status == "failed" and out.error_code == "auth_revoked" and out.rows_written == 0
    for secret in (SECRET, REFRESH, "nobody-issued-this"):
        assert secret not in (out.error_detail or "")
    assert sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_sf"],))[0][0] == before


def test_the_org_daily_limit_is_a_failed_run_and_writes_nothing_for_that_window(world, synced):
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH, api_limit=2, api_used=2) as limited:
        out = run(sync(world, limited, start=date(2026, 12, 1), end=date(2026, 12, 3)))
    assert out.status == "failed" and out.error_code == "rate_limited" and out.rows_written == 0
    assert sql("select count(*) from metric_snapshot where connection_id = %s and observed_on >= '2026-12-01'", (world["a_sf"],))[0][0] == 0


def test_row_level_security_keeps_each_tenants_rows_apart(world, synced):
    async def read(tenant):
        db = Database(DSN, max_size=2)
        await db.connect()
        try:
            async with db.tenant(str(tenant)) as conn:
                return await conn.fetch("select distinct connection_id from metric_snapshot")
        finally:
            await db.close()

    assert {str(r["connection_id"]) for r in run(read(world["a"]))} == {str(world["a_sf"])}
    assert run(read(world["b"])) == []
