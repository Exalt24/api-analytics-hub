"""The Harvest and Microsoft Graph connectors through the real sync runner into real Postgres, then read back as the request role.

The whole path, against the stand-in servers (never a live account or tenant): the connector over HTTP, the SyncRunner as the sync_worker role, the dated snapshots, and row level
security. It also proves what only a database can: migration 018 teaches the metric table the seven keys with the right units and the currency check the one money key (hours are
whole seconds, counts have no currency, the billable amount has one), a second sync rewrites the same rows, a figure the vendor restates replaces the stored one, a key that may not
read Harvest projects is a partial run that lists the amount as degraded, a revoked Microsoft app registration is a failed run with its code that writes nothing, and one tenant
never sees another's rows.

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
from app.connectors.harvest import HarvestConnector  # noqa: E402
from app.connectors.harvest_fake import FakeHarvest  # noqa: E402
from app.connectors.msgraph import MicrosoftGraphConnector  # noqa: E402
from app.connectors.msgraph_fake import FakeGraph  # noqa: E402
from app.db import Database  # noqa: E402
from app.sync.runner import SyncRunner  # noqa: E402

TOKEN, ACCOUNT, SECRET = "pat-db-SECRET", "1234567", "graph-db-SECRET"
START, END = date(2026, 10, 1), date(2026, 10, 8)
MIGRATIONS = ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql", "006_ga4_metrics.sql", "007_meta_metrics.sql", "013_ads_and_business_metrics.sql",
              "017_salesforce_metrics.sql", "018_harvest_and_graph_metrics.sql")
HARVEST_KEYS = ("harvest_hours", "harvest_billable_hours", "harvest_time_entries", "harvest_billable_amount")
GRAPH_KEYS = ("sp_register_items_created", "sp_register_items_modified", "teams_channel_messages")


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


@pytest.fixture(scope="module")
def harvest_server():
    with FakeHarvest(token=TOKEN, account_id=ACCOUNT) as fake:
        fake.add_project(1, "Marketing Website", budget=100)
        for d in range(1, 8):
            fake.add_entry(d, date(2026, 10, d), "1.5", rate=100)
            fake.add_entry(100 + d, date(2026, 10, d), "0.5", billable=False, rate=None)
        yield fake


@pytest.fixture(scope="module")
def graph_server():
    with FakeGraph(client_secret=SECRET) as fake:
        for d in range(1, 8):
            fake.add_item(d, datetime(2026, 10, d, 9, tzinfo=timezone.utc), modified=datetime(2026, 10, d, 17, tzinfo=timezone.utc))
        fake.add_message(900, datetime(2026, 10, 3, 9, tzinfo=timezone.utc))
        fake.add_message(901, datetime(2026, 10, 3, 10, tzinfo=timezone.utc))
        yield fake


@pytest.fixture(scope="module")
def world(harvest_server, graph_server):
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
        for key, platform, account in (("a_harvest", "harvest", ACCOUNT), ("a_graph", "msgraph", graph_server.tenant)):
            cur.execute("insert into platform_connection (tenant_id, platform, external_account, display_name, credentials_enc) values (%s, %s, %s, %s, %s) returning id",
                        (a, platform, account, key, b"ciphertext"))
            ids[key] = cur.fetchone()[0]
    return {"a": a, "b": b, **ids}


def harvest(server, token=TOKEN):
    return HarvestConnector(account_id=ACCOUNT, access_token=token, currency="USD", contact="ops@example.com", base_url=server.url, client=http(), retrier=Retrier(sleep=no_sleep))


def graph(server, secret=SECRET):
    return MicrosoftGraphConnector(tenant_id=server.tenant, client_id="app-client-id", client_secret=secret, site_id=server.site, list_id=server.list_id,
                                   team_id=server.team, channel_id=server.channel, graph_base=server.url, login_base=server.url, client=http(), retrier=Retrier(sleep=no_sleep))


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
def harvested(world, harvest_server):
    return run(sync(world, "a_harvest", harvest(harvest_server)))


@pytest.fixture(scope="module")
def graphed(world, graph_server):
    return run(sync(world, "a_graph", graph(graph_server)))


# ---------------------------------------------------------------- Harvest

def test_the_harvest_sync_succeeds_and_writes_four_figures_for_each_of_seven_days(world, harvested):
    assert harvested.status == "succeeded", (harvested.error_code, harvested.error_detail)
    assert harvested.rows_written == 4 * 7 and len(stored(world["a_harvest"])) == 4 * 7 and not harvested.degraded_metrics


def test_hours_are_whole_seconds_counts_have_no_currency_and_the_amount_has_one(world, harvested):
    got = stored(world["a_harvest"])
    assert got[("harvest_hours", 3)] == (7200, None) and got[("harvest_billable_hours", 3)] == (5400, None) and got[("harvest_time_entries", 3)] == (2, None)
    assert got[("harvest_billable_amount", 3)] == (15000, "USD")
    assert {c for (k, _), (_, c) in got.items() if k != "harvest_billable_amount"} == {None}


def test_migration_018_gives_the_seven_keys_their_units_and_the_currency_check_the_one_money_key(world, harvested, graphed):
    rows = {r[0]: r[1] for r in sql("select key, unit from metric where key = any(%s)", (list(HARVEST_KEYS + GRAPH_KEYS),))}
    assert rows == {"harvest_hours": "seconds", "harvest_billable_hours": "seconds", "harvest_time_entries": "count", "harvest_billable_amount": "minor_currency",
                    "sp_register_items_created": "count", "sp_register_items_modified": "count", "teams_channel_messages": "count"}
    with pytest.raises(psycopg.errors.CheckViolation):
        sql("insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric, currency) values (%s, %s, 'harvest_hours', '2026-10-21', 1, 'USD')",
            (world["a"], world["a_harvest"]))
    with pytest.raises(psycopg.errors.CheckViolation):
        sql("insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric) values (%s, %s, 'harvest_billable_amount', '2026-10-21', 1)",
            (world["a"], world["a_harvest"]))
    sql("insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric, currency) values (%s, %s, 'sf_won_amount', '2026-10-21', 1, 'USD')",
        (world["a"], world["a_harvest"]))


def test_a_second_harvest_sync_rewrites_the_same_rows_and_a_restated_entry_replaces_the_stored_figure(world, harvest_server, harvested):
    before = sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_harvest"],))[0][0]
    harvest_server.add_entry(500, date(2026, 10, 3), "1.0", rate=100)
    out = run(sync(world, "a_harvest", harvest(harvest_server)))
    assert out.status == "succeeded"
    assert sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_harvest"],))[0][0] == before
    got = stored(world["a_harvest"])
    assert got[("harvest_hours", 3)] == (10800, None) and got[("harvest_billable_amount", 3)] == (25000, "USD") and got[("harvest_time_entries", 3)] == (3, None)


def test_a_key_that_may_not_read_projects_is_a_partial_run_with_the_amount_listed_as_degraded(world, harvest_server, harvested):
    harvest_server.projects_forbidden = True
    try:
        out = run(sync(world, "a_harvest", harvest(harvest_server), start=date(2026, 11, 1), end=date(2026, 11, 3)))
    finally:
        harvest_server.projects_forbidden = False
    assert out.status == "partial" and out.degraded_metrics == ["harvest_billable_amount"] and out.rows_written == 3 * 2
    assert {k for (k, d) in stored(world["a_harvest"]) if d in (1, 2)} >= {"harvest_hours"}
    assert sql("select count(*) from metric_snapshot where connection_id = %s and metric_key = 'harvest_billable_amount' and observed_on >= '2026-11-01'", (world["a_harvest"],))[0][0] == 0


def test_a_refused_harvest_token_is_a_failed_run_with_its_code_and_writes_nothing(world, harvest_server, harvested):
    before = sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_harvest"],))[0][0]
    out = run(sync(world, "a_harvest", harvest(harvest_server, token="nobody-issued-this"), start=date(2026, 12, 1), end=date(2026, 12, 3)))
    assert out.status == "failed" and out.error_code == "auth_expired" and out.rows_written == 0
    for secret in (TOKEN, "nobody-issued-this"):
        assert secret not in (out.error_detail or "")
    assert sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_harvest"],))[0][0] == before


# ---------------------------------------------------------------- Microsoft Graph

def test_the_graph_sync_succeeds_and_writes_three_figures_for_each_of_seven_days(world, graphed):
    assert graphed.status == "succeeded", (graphed.error_code, graphed.error_detail)
    assert graphed.rows_written == 3 * 7 and len(stored(world["a_graph"])) == 3 * 7


def test_graph_counts_are_stored_with_no_currency_on_the_day_they_belong_to(world, graphed):
    got = stored(world["a_graph"])
    assert got[("sp_register_items_created", 2)] == (1, None) and got[("sp_register_items_modified", 2)] == (1, None)
    assert got[("teams_channel_messages", 3)] == (2, None) and got[("teams_channel_messages", 4)] == (0, None)
    assert {c for _, c in got.values()} == {None}


def test_a_revoked_app_registration_is_a_failed_run_with_its_code_and_writes_nothing(world, graph_server, graphed):
    before = sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_graph"],))[0][0]
    out = run(sync(world, "a_graph", graph(graph_server, secret="nobody-issued-this"), start=date(2026, 12, 1), end=date(2026, 12, 3)))
    assert out.status == "failed" and out.error_code == "auth_revoked" and out.rows_written == 0
    for secret in (SECRET, "nobody-issued-this"):
        assert secret not in (out.error_detail or "")
    assert sql("select count(*) from metric_snapshot where connection_id = %s", (world["a_graph"],))[0][0] == before


def test_a_missing_permission_is_a_failed_run_naming_the_administrator_step(world, graph_server, graphed):
    graph_server.roles = {"Files.Read.All"}
    graph_server.access_tokens.clear()
    try:
        out = run(sync(world, "a_graph", graph(graph_server), start=date(2026, 12, 5), end=date(2026, 12, 7)))
    finally:
        graph_server.roles = {"Sites.Read.All", "ChannelMessage.Read.All", "Files.Read.All"}
        graph_server.access_tokens.clear()
    assert out.status == "failed" and out.error_code == "auth_revoked" and "administrator" in (out.error_detail or "")


# ---------------------------------------------------------------- both

def test_row_level_security_keeps_each_tenants_rows_apart(world, harvested, graphed):
    async def read(tenant):
        db = Database(DSN, max_size=2)
        await db.connect()
        try:
            async with db.tenant(str(tenant)) as conn:
                return await conn.fetch("select distinct connection_id from metric_snapshot")
        finally:
            await db.close()

    assert {str(r["connection_id"]) for r in run(read(world["a"]))} == {str(world["a_harvest"]), str(world["a_graph"])}
    assert run(read(world["b"])) == []
