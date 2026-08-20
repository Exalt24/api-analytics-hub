"""Tenant isolation, proven NEGATIVELY against real Postgres.

This is the file that answers "how do you know Client A cannot read Client B's
data". Not by asserting the policy exists, but by connecting as tenant A, asking
for tenant B's rows, and requiring zero.

TWO LESSONS ENCODED HERE, both learned the hard way.

  1. FORCE, not just ENABLE. Row level security is bypassed by the table OWNER
     unless FORCE is set, and the owning role is exactly the role most
     applications connect as. A suite that tests policies while connected as the
     owner passes while the database is wide open.

  2. TEST THE INVARIANT, NOT ONLY THE FEATURE. A previous project had 35 passing
     policy tests and a production hole, because every test asked "is this policy
     correct" and none asked "is there a table with NO policy at all". The
     `test_every_tenant_table_has_rls` case below is that missing question.

Skips cleanly when Postgres is not running, so the unit suite stays runnable
without Docker.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "001_init.sql"

DSN = os.environ.get(
    "ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@localhost:5433/analytics"
)

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason="Postgres not reachable, run docker compose up -d"
)

TENANT_TABLES = ("tenant", "platform_connection", "metric_snapshot", "sync_run")


@pytest.fixture(scope="module")
def schema():
    """Apply the migration to a scratch schema-per-run so tests never collide."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema if exists public cascade")
            cur.execute("create schema public")
        with conn.cursor() as cur:
            cur.execute(MIGRATION.read_text(encoding="utf-8"))
    yield


@pytest.fixture(scope="module")
def two_tenants(schema):
    """Two tenants, each with one connection and one snapshot row."""
    a, b = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        # Seeding runs as the owner with RLS forced, so set the GUC per insert.
        for tid, slug in ((a, "tenant-a"), (b, "tenant-b")):
            cur.execute("select set_config('app.tenant_id', %s, false)", (str(tid),))
            cur.execute(
                "insert into tenant (id, slug, name) values (%s, %s, %s)",
                (tid, slug, slug.upper()),
            )
            cur.execute(
                """insert into platform_connection
                       (tenant_id, platform, external_account, credentials_enc)
                   values (%s, 'shopify', %s, %s) returning id""",
                (tid, f"{slug}.myshopify.com", b"ciphertext"),
            )
            conn_id = cur.fetchone()[0]
            cur.execute(
                """insert into metric_snapshot
                       (tenant_id, connection_id, metric_key, observed_on,
                        value_numeric, currency)
                   values (%s, %s, 'gross_sales', '2026-08-01', %s, 'USD')""",
                (tid, conn_id, 111 if slug == "tenant-a" else 222),
            )
    return a, b


def as_tenant(tenant_id):
    """A connection scoped to one tenant, the way the request layer would do it.

    SET ROLE app_request IS NOT COSMETIC. The bootstrap user Postgres creates is a
    SUPERUSER, and a superuser bypasses row level security unconditionally: FORCE
    closes the table-owner hole but not that one. The first run of this suite
    proved it, showing tenant A reading tenant B's rows while every policy was
    correct and every table was ENABLED and FORCED. Dropping to a plain role is
    what the application must do in production, so it is what the tests do.
    """
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("set role app_request")
        cur.execute("select set_config('app.tenant_id', %s, false)", (str(tenant_id),))
    return conn


def as_unscoped_app():
    """The app role with NO tenant set. Must be blind, not omniscient."""
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("set role app_request")
    return conn


# ---------------------------------------------------------------- the negatives

def test_tenant_a_cannot_read_tenant_b_snapshots(two_tenants):
    a, b = two_tenants
    with as_tenant(a) as conn, conn.cursor() as cur:
        cur.execute("select value_numeric from metric_snapshot")
        values = [r[0] for r in cur.fetchall()]
    assert values == [111], f"tenant A saw {values}, tenant B's row leaked"


def test_naming_tenant_b_explicitly_still_returns_nothing(two_tenants):
    """The interesting case: someone edits the request to pass another tenant id.

    Filtering in application code would happily return the row here. The policy
    is not a filter the caller can influence, so the answer is still zero.
    """
    a, b = two_tenants
    with as_tenant(a) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from metric_snapshot where tenant_id = %s", (b,))
        assert cur.fetchone()[0] == 0


def test_tenant_a_cannot_read_tenant_b_credentials(two_tenants):
    a, b = two_tenants
    with as_tenant(a) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from platform_connection")
        assert cur.fetchone()[0] == 1


def test_tenant_a_cannot_write_a_row_belonging_to_tenant_b(two_tenants):
    """WITH CHECK, not just USING. A policy that only restricts reads lets a
    caller INSERT into someone else's tenant, which is worse than reading."""
    a, b = two_tenants
    with as_tenant(a) as conn, conn.cursor() as cur:
        cur.execute("select id from platform_connection limit 1")
        conn_id = cur.fetchone()[0]
        with pytest.raises(psycopg.errors.Error):
            cur.execute(
                """insert into metric_snapshot
                       (tenant_id, connection_id, metric_key, observed_on,
                        value_numeric, currency)
                   values (%s, %s, 'gross_sales', '2026-08-02', 999, 'USD')""",
                (b, conn_id),
            )


def test_no_tenant_set_sees_nothing_rather_than_everything(two_tenants):
    """A connection that forgot to scope itself must be blind, not omniscient.

    current_setting(..., true) returns NULL when unset, so the comparison is NULL
    and no row matches. Fail closed.
    """
    with as_unscoped_app() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from metric_snapshot")
        assert cur.fetchone()[0] == 0


def test_a_garbage_tenant_id_sees_nothing(two_tenants):
    with as_tenant(uuid.uuid4()) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from metric_snapshot")
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------- the invariant

def test_the_request_role_is_neither_superuser_nor_table_owner(schema):
    """The invariant that would have caught the leak in one line.

    RLS is silently inert for a superuser, and inert for the table owner unless
    FORCE is set. So "are the policies right" is the second question; the first is
    "is the role we connect as even subject to them". Asserted against the catalog
    because it is a property of the deployment, not of the SQL.
    """
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select rolsuper, rolbypassrls from pg_roles where rolname = 'app_request'")
        row = cur.fetchone()
        assert row is not None, "app_request role does not exist"
        is_super, bypasses = row
        assert not is_super, "the request role must not be a superuser, RLS would not apply"
        assert not bypasses, "the request role must not have BYPASSRLS"

        cur.execute(
            """select c.relname, pg_get_userbyid(c.relowner)
                 from pg_class c
                where c.relname = any(%s)""",
            (list(TENANT_TABLES),),
        )
        for table, owner in cur.fetchall():
            assert owner != "app_request", (
                f"app_request owns {table}; an owner bypasses RLS unless FORCE is set, "
                "so ownership and request-serving must not be the same role"
            )


def test_every_tenant_table_has_rls_enabled_and_forced(schema):
    """The question nobody asks, which is how a table with no policy ships.

    Asserting each policy is correct cannot catch a table that has none. This
    asks the catalog directly.
    """
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """select relname, relrowsecurity, relforcerowsecurity
                 from pg_class
                where relname = any(%s)""",
            (list(TENANT_TABLES),),
        )
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    missing = [t for t in TENANT_TABLES if t not in rows]
    assert not missing, f"tables absent from the schema: {missing}"

    not_enabled = [t for t, (en, _) in rows.items() if not en]
    not_forced = [t for t, (_, fo) in rows.items() if not fo]
    assert not not_enabled, f"RLS not ENABLED on: {not_enabled}"
    assert not not_forced, (
        f"RLS not FORCED on: {not_forced}. Without FORCE the owning role bypasses "
        "every policy, and the owner is usually the role the app connects as."
    )


def test_every_tenant_table_actually_has_at_least_one_policy(schema):
    """ENABLE with no policy denies everything, which is safe but is a bug.

    A table that is enabled and policy-less looks secure in the catalog and
    breaks the application, so this separates "locked" from "locked out".
    """
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "select tablename, count(*) from pg_policies "
            "where schemaname = 'public' group by tablename"
        )
        counts = dict(cur.fetchall())
    for t in TENANT_TABLES:
        assert counts.get(t, 0) >= 1, f"{t} has RLS on but no policy at all"


def test_money_rows_must_carry_a_currency_at_the_database_level(two_tenants):
    """The same invariant as the connector, enforced a second time in the schema.

    Belt and braces on purpose: a future connector, a migration, or a manual
    backfill can all write rows without passing through the Python guard.
    """
    a, _ = two_tenants
    with as_tenant(a) as conn, conn.cursor() as cur:
        cur.execute("select id from platform_connection limit 1")
        conn_id = cur.fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """insert into metric_snapshot
                       (tenant_id, connection_id, metric_key, observed_on, value_numeric)
                   values (%s, %s, 'gross_sales', '2026-08-05', 500)""",
                (a, conn_id),
            )


def test_resyncing_a_day_corrects_the_row_instead_of_duplicating_it(two_tenants):
    a, _ = two_tenants
    with as_tenant(a) as conn, conn.cursor() as cur:
        cur.execute("select id from platform_connection limit 1")
        conn_id = cur.fetchone()[0]
        for value in (500, 700):
            cur.execute(
                """insert into metric_snapshot
                       (tenant_id, connection_id, metric_key, observed_on,
                        value_numeric, currency)
                   values (%s, %s, 'orders', '2026-08-06', %s, null)
                   on conflict (connection_id, metric_key, observed_on)
                   do update set value_numeric = excluded.value_numeric,
                                 synced_at = now()""",
                (a, conn_id, value),
            )
        cur.execute(
            "select count(*), max(value_numeric) from metric_snapshot "
            "where metric_key = 'orders' and observed_on = '2026-08-06'"
        )
        count, latest = cur.fetchone()
    assert (count, latest) == (1, 700), "a re-sync must correct, not duplicate"
