"""The API layer, proven against real Postgres with real RLS.

WHAT THIS FILE IS FOR. Two questions the listing asks directly:

  "Twenty clients use the same application. How would you make sure Client A can
   never access Client B's data, even if someone MANUALLY MODIFIES an API request?"

  "Role-based access control."

Both are answered here by attacking the running application rather than by
asserting a policy exists. A viewer token tries to POST a sync. Tenant A's token
asks for tenant B's connection by id, which is the manual-modification case exactly.
A revoked key tries to keep working.

THE CONTROL CASES ARE THE LOAD-BEARING HALF. An API that returned 403 to everything
would pass every negative test in here, so each denial is paired with the same
request succeeding for the role that should have it.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("asyncpg")

DSN = os.environ.get(
    "ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@localhost:5433/analytics"
)


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason="Postgres not reachable, run docker compose up -d"
)

from app.api.auth import Role, generate_key, hash_key  # noqa: E402


@pytest.fixture(scope="module")
def seeded():
    """Two tenants, one connection and one snapshot each, and four API keys.

    Applies all three migrations to a scratch public schema, so this suite never
    depends on the order pytest happens to run files in.
    """
    keys = {
        "a_viewer": generate_key(),
        "a_operator": generate_key(),
        "a_admin": generate_key(),
        "b_admin": generate_key(),
        "revoked": generate_key(),
    }
    a, b = uuid.uuid4(), uuid.uuid4()
    conn_ids: dict[str, str] = {}

    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade")
        cur.execute("create schema public")
        for name in ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql"):
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))

        for tid, slug in ((a, "tenant-a"), (b, "tenant-b")):
            cur.execute("select set_config('app.tenant_id', %s, false)", (str(tid),))
            cur.execute("insert into tenant (id, slug, name) values (%s,%s,%s)",
                        (tid, slug, slug.upper()))
            cur.execute(
                """insert into platform_connection
                       (tenant_id, platform, external_account, credentials_enc)
                   values (%s,'shopify',%s,%s) returning id""",
                (tid, "%s.myshopify.com" % slug, b"ciphertext"),
            )
            cid = cur.fetchone()[0]
            conn_ids[slug] = str(cid)
            cur.execute(
                """insert into metric_snapshot
                       (tenant_id, connection_id, metric_key, observed_on,
                        value_numeric, currency)
                   values (%s,%s,'gross_sales', current_date - 1, %s,'USD')""",
                (tid, cid, 111 if slug == "tenant-a" else 222),
            )

        cur.execute("select set_config('app.tenant_id', '', false)")
        for label, raw, tid, role, revoked in (
            ("a_viewer", keys["a_viewer"], a, "viewer", None),
            ("a_operator", keys["a_operator"], a, "operator", None),
            ("a_admin", keys["a_admin"], a, "admin", None),
            ("b_admin", keys["b_admin"], b, "admin", None),
            ("revoked", keys["revoked"], a, "admin", "now()"),
        ):
            cur.execute(
                "insert into api_key (tenant_id, key_hash, label, role, revoked_at) "
                "values (%s,%s,%s,%s,%s)",
                (tid, hash_key(raw), label, role,
                 None if revoked is None else __import__("datetime").datetime.now()),
            )

    return {"keys": keys, "tenants": {"a": str(a), "b": str(b)},
            "connections": conn_ids}


@pytest.fixture(scope="module")
def client(seeded):
    """The real app, over ASGI, with a real pool against the real database."""
    os.environ["DATABASE_URL"] = DSN.replace("postgresql://", "postgresql://")
    os.environ.setdefault("DASHBOARD_ORIGIN", "http://localhost:3000")

    from app.api.main import app as fastapi_app

    async def _make():
        transport = httpx.ASGITransport(app=fastapi_app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    loop = asyncio.new_event_loop()
    c = loop.run_until_complete(_make())

    # LifespanManager equivalent, kept manual so the suite has no extra dependency.
    from contextlib import AsyncExitStack
    stack = AsyncExitStack()

    async def _start():
        await stack.enter_async_context(fastapi_app.router.lifespan_context(fastapi_app))

    loop.run_until_complete(_start())
    yield loop, c
    loop.run_until_complete(stack.aclose())
    loop.run_until_complete(c.aclose())
    loop.close()


def call(client, method, url, key=None, **kw):
    loop, c = client
    headers = kw.pop("headers", {})
    if key:
        headers["Authorization"] = "Bearer %s" % key
    return loop.run_until_complete(getattr(c, method)(url, headers=headers, **kw))


# ------------------------------------------------------------------ auth basics

def test_health_needs_no_credentials(client):
    assert call(client, "get", "/health").status_code == 200


def test_kpis_without_a_token_is_401(client):
    assert call(client, "get", "/api/kpis?days=7").status_code == 401


def test_kpis_with_a_bogus_token_is_401(client, seeded):
    r = call(client, "get", "/api/kpis?days=7", key="aah_not_a_real_key")
    assert r.status_code == 401


def test_a_revoked_key_stops_working(client, seeded):
    r = call(client, "get", "/api/kpis?days=7", key=seeded["keys"]["revoked"])
    assert r.status_code == 401


def test_a_valid_viewer_can_read(client, seeded):
    """The control for all three tests above. Without it, an API that rejected
    everything would look perfectly secure."""
    r = call(client, "get", "/api/kpis?days=7", key=seeded["keys"]["a_viewer"])
    assert r.status_code == 200, r.text
    assert r.json()["window_days"] == 7


# ------------------------------------------------------------------------ RBAC

def test_viewer_may_not_trigger_a_sync(client, seeded):
    r = call(client, "post", "/api/sync",
             key=seeded["keys"]["a_viewer"],
             json={"connection_id": seeded["connections"]["tenant-a"], "days": 7})
    assert r.status_code == 403
    assert "viewer" in r.json()["detail"]


def test_operator_may_trigger_a_sync(client, seeded):
    """The paired control. A 403 for everyone is not access control."""
    r = call(client, "post", "/api/sync",
             key=seeded["keys"]["a_operator"],
             json={"connection_id": seeded["connections"]["tenant-a"], "days": 7})
    assert r.status_code == 202, r.text
    assert r.json()["accepted"] is True


# -------------------------------------------------- the manual-modification case

def test_tenant_a_cannot_read_tenant_b_rows(client, seeded):
    """A sees only its own snapshot, 111, never B's 222."""
    r = call(client, "get", "/api/kpis?days=30", key=seeded["keys"]["a_admin"])
    assert r.status_code == 200
    values = {p["value_numeric"] for p in r.json()["points"]}
    assert 111 in values
    assert 222 not in values, "tenant B's row leaked into tenant A's response"


def test_tenant_b_sees_its_own_row(client, seeded):
    """Control: proves the query works at all, so the assertion above is not
    passing because the endpoint returns nothing to everyone."""
    r = call(client, "get", "/api/kpis?days=30", key=seeded["keys"]["b_admin"])
    values = {p["value_numeric"] for p in r.json()["points"]}
    assert 222 in values and 111 not in values


def test_asking_for_another_tenants_connection_by_id_returns_nothing(client, seeded):
    """The literal question in the listing: someone edits the request by hand and
    substitutes another tenant's connection id."""
    r = call(client, "get",
             "/api/kpis?days=30&connection_id=%s" % seeded["connections"]["tenant-b"],
             key=seeded["keys"]["a_admin"])
    assert r.status_code == 200
    assert r.json()["points"] == [], "filtering by another tenant's id returned rows"


def test_syncing_another_tenants_connection_is_404(client, seeded):
    """A WRITE reaching across the boundary, which returns no data and is still a
    breach. 404 rather than 403, because confirming the row exists elsewhere is a
    tenant-enumeration oracle."""
    r = call(client, "post", "/api/sync",
             key=seeded["keys"]["a_operator"],
             json={"connection_id": seeded["connections"]["tenant-b"], "days": 7})
    assert r.status_code == 404


# --------------------------------------------------------------- input handling

def test_an_arbitrary_window_is_rejected(client, seeded):
    """An open integer here is an invitation to scan the whole table."""
    r = call(client, "get", "/api/kpis?days=99999", key=seeded["keys"]["a_viewer"])
    assert r.status_code == 400


@pytest.mark.parametrize("days", [7, 30, 90])
def test_the_offered_windows_all_work(client, seeded, days):
    r = call(client, "get", "/api/kpis?days=%d" % days, key=seeded["keys"]["a_viewer"])
    assert r.status_code == 200


def test_connections_never_expose_the_credential_column(client, seeded):
    r = call(client, "get", "/api/connections", key=seeded["keys"]["a_admin"])
    assert r.status_code == 200
    body = r.text
    assert "credentials_enc" not in body
    assert "ciphertext" not in body
