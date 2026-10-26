"""Search Console through the real sync runner into real Postgres, then read back as the request role.

The whole path, against the stand-in server (never a live property): GSCConnector over HTTP, the SyncRunner as
the sync_worker role, the dated snapshots and the per-query and per-page table, and row level security as the
request role. It proves what only a database can: a day's top rows are replaced as a set by a later sync, a day
that was provisional is rewritten with its settled figures, the schema refuses the broken shapes, a quota 403 is
recorded as a rate limit and a permission 403 as a revoked connection, and one tenant never sees another's rows.

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

from app.connectors.gsc import GSCConnector  # noqa: E402
from app.connectors.gsc_fake import Fault, FakeGSC, error_body  # noqa: E402
from app.db import Database  # noqa: E402
from app.sync.runner import SyncRunner  # noqa: E402

TOKEN = "tok-gsc-db-SECRET"
SITE = "sc-domain:example.com"
OTHER_SITE = "sc-domain:other.org"
TODAY = date(2026, 11, 2)
MIGRATIONS = ("001_init.sql", "002_control_plane.sql", "006_ga4_metrics.sql", "008_search_metrics.sql")

DAYS = {date(2026, 10, 1): (10, 100, 7.5), date(2026, 10, 2): (20, 200, 6.25), date(2026, 10, 31): (4, 90, 8.0)}
QUERIES = {date(2026, 10, 1): [("shoes", 6, 60, 5.0), ("boots", 4, 40, 10.0)],
           date(2026, 10, 2): [("shoes", 12, 120, 5.0), ("boots", 8, 80, 8.0)]}
PAGES = {date(2026, 10, 1): [("/a", 10, 100, 7.5)], date(2026, 10, 2): [("/a", 20, 200, 6.25)]}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def server():
    with FakeGSC(token=TOKEN, today=TODAY) as fake:
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
        ids["a_gsc"] = connect_row(cur, a, "gsc", SITE, "tenant-a search")
        cur.execute("select set_config('app.tenant_id', %s, false)", (str(b),))
        ids["b_gsc"] = connect_row(cur, b, "gsc", OTHER_SITE, "tenant-b search")
    server.set_site(SITE, DAYS, queries=QUERIES, pages=PAGES)
    server.set_site(OTHER_SITE, {date(2026, 10, 1): (999, 9999, 1.0)}, queries={date(2026, 10, 1): [("secret query", 999, 9999, 1.0)]})
    return {"a": a, "b": b, **ids}


def connector(server, site=SITE, token=TOKEN, **kw):
    client = httpx.AsyncClient(trust_env=False, verify=False, timeout=10)

    async def no_sleep(seconds):
        return None

    kw.setdefault("sleep", no_sleep)
    return GSCConnector(site_url=site, access_token=token, base_url=server.url, client=client, **kw)


async def sync(world, key, tenant_key, server, start, end, site=SITE, **kw):
    db = Database(DSN, max_size=2, role="sync_worker")
    await db.connect()
    conn = connector(server, site, **kw)
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


def counts(connection_id):
    return (sql("select count(*) from metric_snapshot where connection_id = %s", (connection_id,))[0][0],
            sql("select count(*) from search_top_row where connection_id = %s", (connection_id,))[0][0])


WINDOW = (date(2026, 10, 1), date(2026, 11, 1))


def test_a_sync_stores_three_metrics_a_day_and_the_top_rows_with_integer_figures(world, server):
    out = run(sync(world, "a_gsc", "a", server, *WINDOW))
    assert out.status == "succeeded" and out.rows_written == 9 + 6
    rows = sql("select metric_key, observed_on, value_numeric, currency from metric_snapshot where connection_id = %s order by observed_on, metric_key",
               (world["a_gsc"],))
    got = {(r[0], r[1].day): (r[2], r[3]) for r in rows}
    assert got[("search_clicks", 2)] == (20, None) and got[("search_impressions", 2)] == (200, None)
    assert got[("search_position", 1)] == (7500, None) and got[("search_position", 2)] == (6250, None)
    top = sql("select observed_on, dimension, key, clicks, impressions, position_milli from search_top_row "
              "where connection_id = %s order by observed_on, dimension, key", (world["a_gsc"],))
    assert (date(2026, 10, 2), "query", "boots", 8, 80, 8000) in top and (date(2026, 10, 1), "page", "/a", 10, 100, 7500) in top
    assert len(top) == 6 and counts(world["a_gsc"]) == (9, 6)


def test_a_provisional_days_mark_is_stored_as_a_readable_object_and_the_next_sync_settles_it(world, server):
    marks = dict(sql("select observed_on::text, raw->>'provisional' from metric_snapshot where connection_id = %s "
                     "and metric_key = 'search_clicks'", (world["a_gsc"],)))
    assert marks == {"2026-10-01": None, "2026-10-02": None, "2026-10-31": "true"}, "Oct 31 is inside the last 3 days of a Nov 2 run"
    server.today = date(2026, 11, 20)
    server.set_site(SITE, {**DAYS, date(2026, 10, 31): (40, 900, 8.0)}, queries=QUERIES, pages=PAGES)
    try:
        run(sync(world, "a_gsc", "a", server, *WINDOW))
        row = sql("select value_numeric, raw->>'provisional' from metric_snapshot where connection_id = %s and metric_key = 'search_clicks' "
                  "and observed_on = '2026-10-31'", (world["a_gsc"],))[0]
        assert row == (40, None), "the settled figure replaced the provisional one and the mark is gone"
    finally:
        server.today = TODAY
        server.set_site(SITE, DAYS, queries=QUERIES, pages=PAGES)
        run(sync(world, "a_gsc", "a", server, *WINDOW))


def test_syncing_the_same_window_again_adds_no_rows(world, server):
    before = counts(world["a_gsc"])
    out = run(sync(world, "a_gsc", "a", server, *WINDOW))
    assert out.status == "succeeded" and counts(world["a_gsc"]) == before == (9, 6)


def test_a_later_sync_replaces_a_days_top_rows_as_a_set_so_a_query_that_left_the_list_is_gone(world, server):
    server.set_site(SITE, DAYS, queries={**QUERIES, date(2026, 10, 1): [("sandals", 7, 70, 4.0)]}, pages=PAGES)
    try:
        run(sync(world, "a_gsc", "a", server, *WINDOW))
        day_one = sql("select dimension, key from search_top_row where connection_id = %s and observed_on = '2026-10-01' order by dimension, key",
                      (world["a_gsc"],))
        assert day_one == [("page", "/a"), ("query", "sandals")], "shoes and boots were replaced, not kept beside sandals"
        assert sql("select count(*) from search_top_row where connection_id = %s and observed_on = '2026-10-02'", (world["a_gsc"],))[0][0] == 3
    finally:
        server.set_site(SITE, DAYS, queries=QUERIES, pages=PAGES)
        run(sync(world, "a_gsc", "a", server, *WINDOW))
    assert counts(world["a_gsc"]) == (9, 6)


def test_the_database_refuses_a_top_row_that_cannot_be_true_and_a_count_with_a_currency(world):
    cid, tenant = world["a_gsc"], world["a"]

    def insert(dimension="query", key="k", clicks=1, impressions=2, position=3000):
        return sql("insert into search_top_row (tenant_id, connection_id, observed_on, dimension, key, clicks, impressions, position_milli) "
                   "values (%s, %s, '2026-10-20', %s, %s, %s, %s, %s)", (tenant, cid, dimension, key, clicks, impressions, position))

    insert()                                                                         # positive control
    for kwargs in ({"clicks": 3, "impressions": 2}, {"dimension": "country"}, {"key": ""}, {"clicks": -1}, {"position": -5},
                   {"key": "x" * 2049}, {"impressions": -1, "clicks": -1}):
        with pytest.raises(psycopg.errors.CheckViolation):
            insert(**kwargs)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert()
    with pytest.raises(psycopg.errors.CheckViolation):
        sql("insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric, currency) "
            "values (%s, %s, 'search_position', '2026-10-20', 1000, 'USD')", (tenant, cid))
    sql("delete from search_top_row where connection_id = %s and observed_on = '2026-10-20'", (cid,))


def test_a_permission_403_is_a_revoked_connection_and_a_quota_403_is_a_rate_limit_and_neither_writes(world, server):
    before = counts(world["a_gsc"])
    quota = error_body(403, "rateLimitExceeded", "slow")
    server.faults.extend([Fault(403, quota)] * 3)
    limited = run(sync(world, "a_gsc", "a", server, *WINDOW, max_attempts=3))
    assert limited.status == "failed" and limited.error_code == "rate_limited" and limited.rows_written == 0
    denied = run(sync(world, "a_gsc", "a", server, *WINDOW, site=OTHER_SITE.replace("other", "nope")))
    assert denied.status == "failed" and denied.error_code == "auth_revoked"
    assert counts(world["a_gsc"]) == before
    ok = run(sync(world, "a_gsc", "a", server, *WINDOW))                              # the next run succeeds
    assert ok.status == "succeeded"


def test_an_expired_token_and_an_error_inside_a_200_are_failed_runs_with_their_codes(world, server):
    bad = run(sync(world, "a_gsc", "a", server, *WINDOW, token="wrong"))
    assert bad.status == "failed" and bad.error_code == "auth_expired" and "wrong" not in (bad.error_detail or "")
    server.faults.append(Fault(200, error_body(401, "authError", "Invalid Credentials")))
    inside = run(sync(world, "a_gsc", "a", server, *WINDOW))
    assert inside.status == "failed" and inside.error_code == "auth_expired"
    server.faults.append(Fault(200, error_body(400, "invalidParameter", "bad")))
    assert run(sync(world, "a_gsc", "a", server, *WINDOW)).error_code == "upstream_broken"
    assert counts(world["a_gsc"]) == (9, 6)


def test_a_failure_in_the_top_rows_keeps_the_daily_figures_already_written(world, server):
    sql("delete from search_top_row where connection_id = %s", (world["a_gsc"],))
    sql("delete from metric_snapshot where connection_id = %s", (world["a_gsc"],))
    bad = {"keys": ["q"], "clicks": 9, "impressions": 2, "position": 3.0}
    # two date pages (three days then the empty one), then the first day's query rows are broken
    server.faults.extend([Fault(200, {"rows": [{"keys": ["2026-10-01"], "clicks": 1, "impressions": 2, "position": 3.0}]}),
                          Fault(200, {}), Fault(200, {"rows": [bad]})])
    out = run(sync(world, "a_gsc", "a", server, *WINDOW))
    assert out.status == "failed" and out.error_code == "upstream_broken"
    assert counts(world["a_gsc"]) == (3, 0), "the daily figures were committed page by page; the broken top row was not stored"
    assert run(sync(world, "a_gsc", "a", server, *WINDOW)).status == "succeeded" and counts(world["a_gsc"]) == (9, 6)


def test_row_level_security_keeps_each_tenants_search_rows_apart(world, server):
    run(sync(world, "b_gsc", "b", server, *WINDOW, site=OTHER_SITE))

    async def read(tenant):
        db = Database(DSN, max_size=2)
        await db.connect()
        try:
            async with db.tenant(str(tenant)) as conn:
                top = await conn.fetch("select connection_id, key from search_top_row")
                snap = await conn.fetch("select connection_id, value_numeric from metric_snapshot where metric_key = 'search_clicks'")
                return top, snap
        finally:
            await db.close()

    (a_top, a_snap), (b_top, b_snap) = run(read(world["a"])), run(read(world["b"]))
    assert {str(r["connection_id"]) for r in a_top + a_snap} == {str(world["a_gsc"])}
    assert {str(r["connection_id"]) for r in b_top + b_snap} == {str(world["b_gsc"])}
    assert "secret query" not in {r["key"] for r in a_top} and "secret query" in {r["key"] for r in b_top}

    async def forge():
        db = Database(DSN, max_size=2)
        await db.connect()
        try:
            async with db.tenant(str(world["a"])) as conn:
                await conn.execute("insert into search_top_row (tenant_id, connection_id, observed_on, dimension, key, clicks, impressions, position_milli) "
                                   "values ($1, $2, '2026-10-20', 'query', 'k', 1, 2, 3000)", world["b"], world["b_gsc"])
        finally:
            await db.close()

    with pytest.raises(Exception) as exc:
        run(forge())
    assert "row-level security" in str(exc.value).lower()


def test_applying_the_migration_a_second_time_is_safe(world):
    sql((ROOT / "migrations" / "008_search_metrics.sql").read_text(encoding="utf-8"))
    assert sql("select count(*) from metric where key like 'search\\_%%'")[0][0] == 3
