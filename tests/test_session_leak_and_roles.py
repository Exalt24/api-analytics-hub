"""Two guards that the mutation harness proved nothing was testing.

Both fail OPEN in the sense that the application keeps working perfectly while
being wrong, which is exactly the class of defect that survives a green suite.

  1. THE TENANT GUC MUST BE TRANSACTION-SCOPED. `set_config(..., true)` dies with
     the transaction; `set_config(..., false)` sets it for the SESSION, and a
     session is a pooled connection that the next request will borrow. With the
     session-scoped version, request two inherits request one's tenant and reads
     its rows. It tests perfectly in development, where there is only ever one
     request in flight, and it is a cross-tenant leak under load.

  2. AN UNRECOGNISED ROLE MUST FAIL CLOSED. Defaulting an unknown role to viewer
     is the tempting choice and it silently grants access after a typo in a
     migration.

The first test pins the pool to ONE connection on purpose. That is what
guarantees the second query reuses the first query's physical connection, which is
the only condition under which the bug is observable.
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
pytest.importorskip("asyncpg")
pytest.importorskip("fastapi")

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

from app.api.auth import Authenticator, Role  # noqa: E402
from app.db import Database  # noqa: E402


@pytest.fixture(scope="module")
def two_tenants():
    a, b = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade; create schema public")
        for name in ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql"):
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))
        for tid, slug, value in ((a, "leak-a", 111), (b, "leak-b", 222)):
            cur.execute("select set_config('app.tenant_id', %s, false)", (str(tid),))
            cur.execute("insert into tenant (id, slug, name) values (%s,%s,%s)",
                        (tid, slug, slug))
            cur.execute(
                """insert into platform_connection
                       (tenant_id, platform, external_account, credentials_enc)
                   values (%s,'shopify',%s,%s) returning id""",
                (tid, slug + ".myshopify.com", b"x"),
            )
            cid = cur.fetchone()[0]
            cur.execute(
                """insert into metric_snapshot
                       (tenant_id, connection_id, metric_key, observed_on,
                        value_numeric, currency)
                   values (%s,%s,'gross_sales', current_date - 1, %s,'USD')""",
                (tid, cid, value),
            )
    return str(a), str(b)


async def _no_reset(conn):
    """Replace asyncpg's per-release RESET ALL with nothing.

    MEASURED, not assumed: with the default reset in place this test passed even
    when the guard was mutated to session scope, because the driver cleared the
    GUC before the next acquire could observe it. The mutation harness caught the
    test proving nothing.

    Disabling it here is the only way to assert that OUR scoping holds by itself.
    A driver change, a different pool, or PgBouncer in transaction mode removes
    that net without anyone touching this code, and then the session-scoped
    version is a live cross-tenant leak.
    """
    return None


def test_the_tenant_setting_does_not_survive_onto_the_next_request(two_tenants):
    """A single pooled connection, used twice. The second use must see nothing."""
    a, _b = two_tenants

    async def go():
        # ONE connection in the pool, so the second acquire is guaranteed to be
        # the same physical connection that just served tenant A, and no reset in
        # between so the connection state is genuinely carried over.
        db = Database(dsn=DSN, min_size=1, max_size=1)
        db._reset_override = _no_reset  # noqa: SLF001 - deliberate, see above
        await db.connect()
        try:
            async with db.tenant(a) as conn:
                first = await conn.fetch("select value_numeric from metric_snapshot")

            # No tenant set. RLS should now match nothing at all.
            async with db.unscoped() as conn:
                after = await conn.fetch("select value_numeric from metric_snapshot")
                guc = await conn.fetchval(
                    "select current_setting('app.tenant_id', true)"
                )
            return first, after, guc
        finally:
            await db.close()

    first, after, guc = asyncio.run(go())

    assert [r["value_numeric"] for r in first] == [111], "tenant A could not read itself"
    assert after == [], (
        "the previous request's tenant survived onto the next use of the same "
        "pooled connection, which is a cross-tenant leak under concurrency"
    )
    assert not guc, "app.tenant_id outlived its transaction: %r" % (guc,)


def test_a_tenant_scoped_read_still_works_at_all(two_tenants):
    """Control. Without it, a pool that returned nothing to everybody would pass
    the leak test above and look secure."""
    a, b = two_tenants

    async def go():
        db = Database(dsn=DSN, min_size=1, max_size=1)
        await db.connect()
        try:
            async with db.tenant(a) as conn:
                ra = await conn.fetch("select value_numeric from metric_snapshot")
            async with db.tenant(b) as conn:
                rb = await conn.fetch("select value_numeric from metric_snapshot")
            return [r["value_numeric"] for r in ra], [r["value_numeric"] for r in rb]
        finally:
            await db.close()

    ra, rb = asyncio.run(go())
    assert ra == [111]
    assert rb == [222], "the same pooled connection must serve B its OWN rows"


class _FakeConn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, *_a, **_k):
        return self._row


class _FakeDb:
    """Minimal stand-in so an impossible database state can be exercised.

    The role column carries a CHECK constraint, so an invalid role cannot be
    inserted. It CAN arrive from a future migration that adds a role the code does
    not know, which is precisely the case that must fail closed, so it is
    simulated here rather than left untested because the schema forbids it today.
    """

    def __init__(self, row):
        self._row = row

    def unscoped(self):
        row = self._row

        class _Ctx:
            async def __aenter__(self):
                return _FakeConn(row)

            async def __aexit__(self, *_a):
                return False

        return _Ctx()


def test_an_unknown_role_is_refused_rather_than_downgraded():
    from fastapi import HTTPException

    auth = Authenticator(_FakeDb({
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "role": "superuser",       # not in the enum
        "revoked_at": None,
    }))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.resolve("aah_whatever"))
    assert exc.value.status_code == 403
    assert "unknown role" in str(exc.value.detail)


def test_a_known_role_still_resolves():
    """Control for the test above."""
    tid = uuid.uuid4()
    auth = Authenticator(_FakeDb({
        "id": uuid.uuid4(),
        "tenant_id": tid,
        "role": "operator",
        "revoked_at": None,
    }))
    principal = asyncio.run(auth.resolve("aah_whatever"))
    assert principal.role is Role.OPERATOR
    assert principal.tenant_id == str(tid)
    assert principal.may("write:sync")
    assert not principal.may("write:connections")
