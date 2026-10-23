"""Observability against real Postgres: which connections are called stuck, who may see them, the /metrics page and the alert script.

The seeded world has one healthy connection and one of every problem kind, plus three that must NOT be reported (paused, brand new, healthy-with-a-recent-lease), and a second
tenant with its own stale connection, so the cross-tenant read and the tenant-scoped read can be told apart.

Skips cleanly when Postgres is not running (`docker compose up -d`).
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("asyncpg")

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@localhost:5433/analytics")


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="Postgres not reachable, run docker compose up -d")

from app.api.auth import generate_key, hash_key  # noqa: E402

MIGRATIONS = ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql", "004_observability.sql")
EXPECTED = {("stale", "A-stale"), ("expired_lease", "A-expired"), ("never_succeeded", "A-never"), ("stale", "B-stale")}


def _connection(cur, tenant, name, *, status="active", created="now()", cadence=None):
    cur.execute("select set_config('app.tenant_id', %s, false)", (str(tenant),))
    cur.execute(
        "insert into platform_connection (tenant_id, platform, external_account, display_name, credentials_enc, status, created_at) "
        "values (%s, 'shopify', %s, %s, %s, %s, " + created + ") returning id",
        (tenant, name + ".myshopify.com", name, b"ciphertext", status),
    )
    cid = cur.fetchone()[0]
    if cadence:
        cur.execute("insert into sync_schedule (connection_id, tenant_id, cadence_seconds) values (%s, %s, %s)", (cid, tenant, cadence))
    return cid


def _run(cur, tenant, cid, *, status, started, finished=None, lease=None):
    cur.execute(
        "insert into sync_run (tenant_id, connection_id, trigger, window_start, window_end, status, started_at, finished_at, lease_expires_at, claim_token) "
        "values (%s, %s, 'schedule', current_date - 1, current_date, %s, " + started + ", " + (finished or "null") + ", " + (lease or "null") + ", gen_random_uuid())",
        (tenant, cid, status),
    )


@pytest.fixture(scope="module")
def world():
    a, b = uuid.uuid4(), uuid.uuid4()
    key = generate_key()
    ids = {}
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade; create schema public")
        for name in MIGRATIONS:
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))
        for tid, slug in ((a, "tenant-a"), (b, "tenant-b")):
            cur.execute("select set_config('app.tenant_id', %s, false)", (str(tid),))
            cur.execute("insert into tenant (id, slug, name) values (%s, %s, %s)", (tid, slug, slug))

        ids["A-ok"] = _connection(cur, a, "A-ok", cadence=900)
        _run(cur, a, ids["A-ok"], status="succeeded", started="now() - interval '5 minutes'", finished="now() - interval '100 seconds'")

        ids["A-stale"] = _connection(cur, a, "A-stale", cadence=900)          # three cadences are 2700 s; the last success is 4 hours old
        _run(cur, a, ids["A-stale"], status="succeeded", started="now() - interval '4 hours'", finished="now() - interval '4 hours'")
        _run(cur, a, ids["A-stale"], status="failed", started="now() - interval '10 minutes'", finished="now() - interval '9 minutes'")

        ids["A-expired"] = _connection(cur, a, "A-expired", cadence=900)      # a recent success, so NOT stale; only the dead lease is wrong
        _run(cur, a, ids["A-expired"], status="succeeded", started="now() - interval '20 minutes'", finished="now() - interval '19 minutes'")
        _run(cur, a, ids["A-expired"], status="running", started="now() - interval '30 minutes'", lease="now() - interval '10 minutes'")

        ids["A-never"] = _connection(cur, a, "A-never", created="now() - interval '2 days'")
        ids["A-paused"] = _connection(cur, a, "A-paused", status="paused", created="now() - interval '9 days'")
        ids["A-new"] = _connection(cur, a, "A-new")                           # younger than any window: no run yet is normal

        ids["B-stale"] = _connection(cur, b, "B-stale", created="now() - interval '3 days'")   # no schedule: the 6 hour default applies
        _run(cur, b, ids["B-stale"], status="succeeded", started="now() - interval '10 hours'", finished="now() - interval '10 hours'")

        cur.execute("select set_config('app.tenant_id', '', false)")
        cur.execute("insert into api_key (tenant_id, key_hash, label, role) values (%s, %s, 'a_viewer', 'viewer')", (a, hash_key(key)))
    return {"a": str(a), "b": str(b), "key": key, "ids": {k: str(v) for k, v in ids.items()}}


def _fetch_as(role, sql, tenant=None):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute('set role "%s"' % role)
        if tenant:
            cur.execute("select set_config('app.tenant_id', %s, false)", (tenant,))
        cur.execute(sql)
        return cur.fetchall()


def _names(world, rows):
    by_id = {v: k for k, v in world["ids"].items()}
    return {(kind, by_id[str(cid)]) for cid, _plat, kind, _age in rows}


def test_the_worker_role_sees_every_problem_and_only_problems(world):
    rows = _fetch_as("sync_worker", "select * from sync_problems()")
    assert _names(world, rows) == EXPECTED
    assert all(age > 0 for *_rest, age in rows)


def test_a_healthy_a_paused_and_a_brand_new_connection_are_not_reported(world):
    reported = {name for _kind, name in _names(world, _fetch_as("sync_worker", "select * from sync_problems()"))}
    assert reported.isdisjoint({"A-ok", "A-paused", "A-new"})


def test_the_expired_lease_connection_is_not_also_called_stale(world):
    kinds = [kind for kind, name in _names(world, _fetch_as("sync_worker", "select * from sync_problems()")) if name == "A-expired"]
    assert kinds == ["expired_lease"]


def test_a_request_role_sees_only_its_own_tenants_problems_and_unscoped_sees_none(world):
    own = _names(world, _fetch_as("app_request", "select * from sync_problems()", tenant=world["a"]))
    assert own == {x for x in EXPECTED if x[1].startswith("A-")}
    assert _fetch_as("app_request", "select * from sync_problems()") == []


def test_a_threshold_argument_changes_the_answer_for_a_connection_with_no_schedule(world):
    wide = _names(world, _fetch_as("sync_worker", "select * from sync_problems(172800)"))
    assert ("stale", "B-stale") not in wide and ("stale", "A-stale") in wide      # B has no schedule: 10 h is inside 48 h; A-stale's own cadence rules


def test_run_counts_are_grouped_by_platform_and_status(world):
    rows = {(plat, status): n for plat, status, n in _fetch_as("sync_worker", "select * from sync_run_counts()")}
    assert rows == {("shopify", "succeeded"): 4, ("shopify", "failed"): 1, ("shopify", "running"): 1}


# --------------------------------------------------------------------------------------- the API

@pytest.fixture(scope="module")
def client(world):
    os.environ["DATABASE_URL"] = DSN
    os.environ.setdefault("DASHBOARD_ORIGIN", "http://localhost:3000")
    from contextlib import AsyncExitStack

    from app.api.main import app as fastapi_app

    loop = asyncio.new_event_loop()
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test")
    stack = AsyncExitStack()
    loop.run_until_complete(stack.enter_async_context(fastapi_app.router.lifespan_context(fastapi_app)))
    yield loop, c, fastapi_app
    loop.run_until_complete(stack.aclose())
    loop.run_until_complete(c.aclose())
    loop.close()


def call(client, url, **kw):
    loop, c, _app = client
    return loop.run_until_complete(c.get(url, **kw))


def test_the_metrics_page_reports_the_seeded_problems(client, world):
    r = call(client, "/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    page = r.text
    assert "hub_scrape_ok 1" in page and "hub_sync_problems_total 4" in page
    assert 'hub_sync_problems{kind="stale",platform="shopify"} 2' in page
    assert 'hub_sync_problems{kind="expired_lease",platform="shopify"} 1' in page
    assert 'hub_sync_runs_24h{platform="shopify",status="running"} 1' in page


def test_the_oldest_problem_age_is_the_largest_age_among_problems(client, world):
    page = call(client, "/metrics").text
    oldest = [float(x.split()[-1]) for x in page.splitlines() if x.startswith("hub_sync_oldest_problem_age_seconds")][0]
    assert 172800 - 60 < oldest < 172800 + 600


def test_the_page_carries_no_tenant_id_no_shop_domain_and_no_credential(client, world):
    page = call(client, "/metrics").text
    for forbidden in (world["a"], world["b"], "myshopify", "ciphertext", world["key"], *world["ids"].values()):
        assert forbidden not in page


def test_a_request_is_counted_by_route_template_logged_and_gets_a_request_id(client, world):
    loop, c, app = client
    seen = []

    import logging

    class Grab(logging.Handler):
        def emit(self, record):
            seen.append(self.format(record))

    from app.obs import JsonFormatter
    h = Grab()
    h.setFormatter(JsonFormatter())
    logging.getLogger("hub").addHandler(h)
    try:
        r = call(client, "/api/kpis?days=7", headers={"Authorization": "Bearer %s" % world["key"], "x-request-id": "trace-1234567890"})
        assert r.status_code == 200 and r.headers["x-request-id"] == "trace-1234567890"
        call(client, "/no/such/path/%s" % world["ids"]["A-ok"])
    finally:
        logging.getLogger("hub").removeHandler(h)
    lines = [json.loads(s) for s in seen]
    req = [x for x in lines if x.get("event") == "request" and x.get("route") == "/api/kpis"][0]
    assert req["status"] == 200 and req["tenant_id"] == world["a"] and req["request_id"] == "trace-1234567890" and req["duration_ms"] >= 0
    assert world["key"] not in "\n".join(seen) and "days=7" not in "\n".join(seen)          # no token, no query string
    unmatched = [x for x in lines if x.get("event") == "request" and x.get("status") == 404][0]
    assert unmatched["route"] == "unmatched" and world["ids"]["A-ok"] not in "\n".join(seen)   # a raw path with an id never becomes a route label
    page = call(client, "/metrics").text
    assert 'route="/api/kpis",status="200"' in page and "unmatched" in page and world["ids"]["A-ok"] not in page


def test_a_database_failure_is_reported_as_scrape_ok_zero_not_as_zeros_and_not_as_a_500(client, world, monkeypatch):
    import app.api.main as main

    async def boom(_db):
        raise ConnectionError("db down postgresql://u:secret@h/d")

    monkeypatch.setattr(main, "read_gauges", boom)
    r = call(client, "/metrics")
    assert r.status_code == 200 and "hub_scrape_ok 0" in r.text and "hub_sync_problems" not in r.text and "secret" not in r.text
    assert "hub_http_requests_total" in r.text


def test_an_optional_token_protects_the_metrics_page(client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "scrape-token-1234")
    assert call(client, "/metrics").status_code == 401
    assert call(client, "/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert call(client, "/metrics", headers={"Authorization": "Bearer scrape-token-1234"}).status_code == 200


def test_the_request_pool_cannot_become_the_worker_role(client):
    """The reason the gauges use their own pool: app_request must not be able to cross tenants by asking."""
    # A superuser session may SET ROLE to anything, which would make this test meaningless. The property is about a deployment's own login role, so connect as a plain
    # login that is a member of app_request only, the way a production request pool is.
    login = "hub_login_test"
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select 1 from pg_roles where rolname = %s", (login,))
        if cur.fetchone() is None:
            cur.execute("create role %s login password 'pw-login-test'" % login)
        cur.execute("grant app_request to %s" % login)
        cur.execute("revoke sync_worker from %s" % login)
    plain = DSN.replace("analytics:analytics@", "%s:pw-login-test@" % login)
    with psycopg.connect(plain, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("set role app_request")                       # positive control: the member role works
        cur.execute("reset role")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("set role sync_worker")


# ------------------------------------------------------------------------------ the alert script

class _Slack(http.server.BaseHTTPRequestHandler):
    posts: list = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        _Slack.posts.append((self.path, json.loads(body)))
        self.send_response(_Slack.status)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def slack():
    _Slack.posts = []
    _Slack.status = 200
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Slack)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:%d/services/T000/B000/SECRETTOKEN" % srv.server_port
    srv.shutdown()


def _script(url, state, *extra):
    env = {**os.environ, "DATABASE_URL": DSN, "SLACK_WEBHOOK_URL": url}
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "check_sync_health.py"), "--state", str(state), *extra],
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_the_script_alerts_once_then_stays_quiet_then_alerts_again_when_the_set_changes(world, slack, tmp_path):
    state = tmp_path / "state.json"
    first = _script(slack, state)
    assert first.returncode == 1 and len(_Slack.posts) == 1
    text = _Slack.posts[0][1]["text"]
    assert "4 connection problem(s)" in text and world["ids"]["A-stale"][:8] in text and world["ids"]["B-stale"][:8] in text
    assert "SECRETTOKEN" not in first.stdout + first.stderr
    assert '"event":"sync_problem"' in first.stdout

    second = _script(slack, state)
    assert second.returncode == 1 and len(_Slack.posts) == 1               # same set, inside the window: no second post

    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("set role sync_worker")
        cur.execute("update sync_run set status = 'failed', finished_at = now() where status = 'running'")
    third = _script(slack, state)
    assert third.returncode == 1 and len(_Slack.posts) == 2 and "3 connection problem(s)" in _Slack.posts[1][1]["text"]


def test_a_failed_delivery_exits_2_keeps_the_state_and_never_prints_the_webhook(world, slack, tmp_path):
    state = tmp_path / "state.json"
    _Slack.status = 500
    r = _script(slack, state)
    assert r.returncode == 2 and not state.exists()
    assert "SECRETTOKEN" not in r.stdout + r.stderr and "127.0.0.1" not in r.stdout + r.stderr
    _Slack.status = 200
    again = _script(slack, state)
    assert again.returncode == 1 and len(_Slack.posts) == 2                 # the failed attempt was recorded by the server too; this one delivered


def test_an_unreachable_database_is_exit_2_never_exit_0(slack, tmp_path):
    env = {**os.environ, "DATABASE_URL": "postgresql://analytics:analytics@127.0.0.1:1/analytics", "SLACK_WEBHOOK_URL": slack}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_sync_health.py")], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 2 and "SECRETTOKEN" not in r.stdout + r.stderr
