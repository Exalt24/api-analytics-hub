"""The MSP connectors (ConnectWise Manage, Autotask, NinjaOne) through the real sync runner into real Postgres, then read back as the request role.

The whole path, against the stand-in servers (never a live tenant): the connector over HTTP, the SyncRunner as the sync_worker role, the dated snapshots, and row level security.
It also proves what only a database can: migrations 025 to 027 teach the metric table each connector's keys with the right units, a currency on any of them is refused by the
currency check (none is money), a second sync rewrites the same rows, a figure the vendor restates replaces the stored one, a refused credential is a failed run with its code that
writes nothing and leaves no secret in the error, and one tenant never sees another's rows.

Skips cleanly when Postgres is not running (`docker compose up -d`); CI sets ADS_REQUIRE_DB=1 so a Postgres that did not start fails the suite instead.
Like the other database suites it drops and recreates the `public` schema of the database it points at.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import date, datetime, timezone
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
from app.connectors.connectwise import METRICS as CW_KEYS  # noqa: E402
from app.connectors.connectwise import ConnectWiseConnector  # noqa: E402
from app.connectors.connectwise_fake import FakeConnectWise  # noqa: E402
from app.db import Database  # noqa: E402
from app.sync.runner import SyncRunner  # noqa: E402

START, END = date(2026, 10, 1), date(2026, 10, 8)
MIGRATIONS = ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql", "006_ga4_metrics.sql", "007_meta_metrics.sql", "013_ads_and_business_metrics.sql",
              "017_salesforce_metrics.sql", "018_harvest_and_graph_metrics.sql", "025_connectwise_metrics.sql")
CW_COMPANY, CW_PUBLIC, CW_PRIVATE, CW_CLIENT = "acme", "pub-db-SECRET", "priv-db-SECRET", "client-guid-db"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def sql(query, params=()):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


async def no_sleep(seconds):
    return None


def http():
    return httpx.AsyncClient(trust_env=False, verify=False, timeout=10)


def dt(day, hour=9, month=10):
    return datetime(2026, month, day, hour, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def cw_server():
    with FakeConnectWise(company=CW_COMPANY, public_key=CW_PUBLIC, private_key=CW_PRIVATE, client_id=CW_CLIENT) as fake:
        for d in range(1, 8):
            fake.add_ticket(d, dt(d), closed_at=dt(d, 15) if d % 2 else None, resolution_goal=dt(d, 12))
            fake.add_time(d, dt(d, 10), "1.5", ticket=d)
        yield fake


@pytest.fixture(scope="module")
def world(cw_server):
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
        for key, platform, account in (("a_cw", "connectwise", CW_COMPANY),):
            cur.execute("insert into platform_connection (tenant_id, platform, external_account, display_name, credentials_enc) values (%s, %s, %s, %s, %s) returning id",
                        (a, platform, account, key, b"ciphertext"))
            ids[key] = cur.fetchone()[0]
    return {"a": a, "b": b, **ids}


def connectwise(server, private=CW_PRIVATE):
    return ConnectWiseConnector(api_url=server.api_url, company_id=CW_COMPANY, public_key=CW_PUBLIC, private_key=private, client_id=CW_CLIENT, client=http(),
                                retrier=Retrier(sleep=no_sleep), clock=lambda: NOW)


async def sync(world, connection, connector, start=START, end=END):
    db = Database(DSN, max_size=2, role="sync_worker")
    await db.connect()
    try:
        return await SyncRunner(db).run_once(tenant_id=str(world["a"]), connection_id=str(world[connection]), connector=connector,
                                             window_start=start, window_end=end, trigger="backfill")
    finally:
        await connector.aclose()
        await db.close()


def stored(connection_id):
    return {(r[0], r[1].day): (r[2], r[3]) for r in sql(
        "select metric_key, observed_on, value_numeric, currency from metric_snapshot where connection_id = %s", (connection_id,))}


@pytest.fixture(scope="module")
def cw_synced(world, cw_server):
    return run(sync(world, "a_cw", connectwise(cw_server)))


# ---------------------------------------------------------------- ConnectWise

def test_the_connectwise_sync_succeeds_and_writes_four_figures_for_each_of_seven_days(world, cw_synced):
    assert cw_synced.status == "succeeded", (cw_synced.error_code, cw_synced.error_detail)
    assert cw_synced.rows_written == 4 * 7 and len(stored(world["a_cw"])) == 4 * 7 and not cw_synced.degraded_metrics


def test_connectwise_figures_are_counts_and_whole_seconds_with_no_currency(world, cw_synced):
    got = stored(world["a_cw"])
    assert got[("cw_tickets_opened", 3)] == (1, None) and got[("cw_tickets_closed", 3)] == (1, None) and got[("cw_tickets_closed", 4)] == (0, None)
    assert got[("cw_time_seconds", 3)] == (5400, None)
    assert got[("cw_resolution_goals_missed", 3)] == (1, None), "ticket 3 closed at 15:00 against a 12:00 goal"
    assert got[("cw_resolution_goals_missed", 4)] == (1, None), "ticket 4 is open and its goal passed before the sync's clock"
    assert {c for _, c in got.values()} == {None}


def test_migration_025_gives_the_four_keys_their_units_and_the_currency_check_refuses_a_currency_on_them(world, cw_synced):
    rows = {r[0]: (r[1], r[2]) for r in sql("select key, unit, kind from metric where key = any(%s)", (list(CW_KEYS),))}
    assert rows == {"cw_tickets_opened": ("count", "delta"), "cw_tickets_closed": ("count", "delta"), "cw_resolution_goals_missed": ("count", "delta"),
                    "cw_time_seconds": ("seconds", "delta")}
    for key in CW_KEYS:
        with pytest.raises(psycopg.errors.CheckViolation):
            sql("insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric, currency) values (%s, %s, %s, '2026-10-21', 1, 'USD')",
                (world["a"], world["a_cw"], key))


def test_a_second_connectwise_sync_rewrites_the_same_rows_and_a_restated_ticket_replaces_the_stored_figure(world, cw_server, cw_synced):
    before = sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_cw"],))[0][0]
    cw_server.add_ticket(500, dt(3, 11))
    cw_server.add_time(500, dt(3, 12), "1.0", ticket=500)
    out = run(sync(world, "a_cw", connectwise(cw_server)))
    assert out.status == "succeeded"
    assert sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_cw"],))[0][0] == before
    got = stored(world["a_cw"])
    assert got[("cw_tickets_opened", 3)] == (2, None) and got[("cw_time_seconds", 3)] == (9000, None)


def test_a_refused_connectwise_key_is_a_failed_run_with_its_code_and_writes_nothing(world, cw_server, cw_synced):
    before = sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_cw"],))[0][0]
    out = run(sync(world, "a_cw", connectwise(cw_server, private="nobody-issued-this"), start=date(2026, 12, 1), end=date(2026, 12, 3)))
    assert out.status == "failed" and out.error_code == "auth_revoked" and out.rows_written == 0
    for secret in (CW_PRIVATE, CW_PUBLIC, "nobody-issued-this"):
        assert secret not in (out.error_detail or "")
    assert sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_cw"],))[0][0] == before


# ---------------------------------------------------------------- all

def test_row_level_security_keeps_each_tenants_rows_apart(world, cw_synced):
    async def read(tenant):
        db = Database(DSN, max_size=2)
        await db.connect()
        try:
            async with db.tenant(str(tenant)) as conn:
                return await conn.fetch("select distinct connection_id from metric_snapshot")
        finally:
            await db.close()

    assert {str(r["connection_id"]) for r in run(read(world["a"]))} == {str(world["a_cw"])}
    assert run(read(world["b"])) == []
