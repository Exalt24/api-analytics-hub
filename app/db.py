"""Database access, with the tenant boundary enforced by the connection itself.

THE ONE IDEA IN THIS FILE. Every query that touches tenant data runs inside a
transaction that has already had `app.tenant_id` set, as a NON-superuser role, so
the row level security policies in `migrations/001_init.sql` do the filtering. The
application never writes `WHERE tenant_id = ...` for isolation, because one
forgotten WHERE clause is the entire breach and no amount of review reliably
catches the one that is missing.

WHY `set_config(..., true)` AND NOT `SET`. The `true` makes it transaction-local,
so the setting dies with the transaction and cannot leak onto the next request that
borrows the same pooled connection. A session-level SET on a pooled connection is
how one tenant ends up reading another's rows under load, and it tests perfectly
in development where there is only ever one request in flight.

WHY THE ROLE MATTERS MORE THAN THE POLICIES. Measured on this project: the first
isolation run had tenant A reading tenant B's rows with RLS enabled AND forced and
every policy correct, because the tests connected as the bootstrap user, which
Postgres creates as a SUPERUSER, and a superuser bypasses RLS unconditionally.
FORCE closes the table-owner hole, not the superuser hole. So the pool refuses to
start if its role can bypass RLS, rather than trusting the deployment to have got
the connection string right.
"""
from __future__ import annotations

import contextlib
import os
from typing import Any, AsyncIterator

import asyncpg

DSN_ENV = "DATABASE_URL"


class UnsafeDatabaseRole(RuntimeError):
    """The configured role can see through row level security.

    Its own type because this is not a query bug to retry, it is a deployment that
    must not be allowed to serve traffic at all.
    """


class Database:
    def __init__(self, dsn: str | None = None, min_size: int = 1, max_size: int = 10,
                 role: str | None = None):
        self._dsn = dsn or os.environ.get(DSN_ENV)
        if not self._dsn:
            raise RuntimeError("%s is not set" % DSN_ENV)
        self._min = min_size
        self._max = max_size
        #: Drop into this role on every pooled connection. The login role a
        #: deployment uses is frequently the schema owner, and an owner bypasses
        #: RLS unless FORCE is set, while a superuser bypasses it even then. SET
        #: ROLE changes current_user, so the policies then apply for real, and the
        #: startup assertion below verifies that rather than assuming it.
        self._role = role or os.environ.get("DATABASE_ROLE") or "app_request"
        self._pool: asyncpg.Pool | None = None
        #: Test seam. asyncpg issues RESET ALL when a connection returns to the
        #: pool, which clears any session GUC and therefore HIDES a session-scoped
        #: `set_config` bug completely. A test that wants to prove our own
        #: transaction scoping holds on its own has to remove that net first, and
        #: it should: a different driver, a different pool, or PgBouncer in
        #: transaction mode removes it without anyone editing this file.
        self._reset_override = None

    async def connect(self) -> None:
        role = self._role

        async def _init(conn: asyncpg.Connection) -> None:
            if role:
                # Quoted as an identifier. The value comes from configuration, and
                # configuration is not automatically trustworthy input.
                await conn.execute('set role "%s"' % role.replace('"', '""'))

        kwargs = {"init": _init}
        if self._reset_override is not None:
            kwargs["reset"] = self._reset_override
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min, max_size=self._max, **kwargs
        )
        await self._assert_role_cannot_bypass_rls()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("connect() has not been awaited")
        return self._pool

    async def _assert_role_cannot_bypass_rls(self) -> None:
        """Refuse to serve if the role is a superuser or holds BYPASSRLS.

        A startup check rather than a test, because a test proves it about the CI
        database and says nothing about the connection string in production, which
        is exactly where the difference bites.
        """
        row = await self.pool.fetchrow(
            "select current_user as who, rolsuper, rolbypassrls "
            "from pg_roles where rolname = current_user"
        )
        if row is None:
            raise UnsafeDatabaseRole("cannot read the current role from pg_roles")
        if row["rolsuper"] or row["rolbypassrls"]:
            raise UnsafeDatabaseRole(
                "connected as %r, which %s. Row level security does not apply to it, "
                "so every tenant policy in the schema is inert. Use the app_request "
                "role created by migrations/001_init.sql."
                % (row["who"],
                   "is a SUPERUSER" if row["rolsuper"] else "holds BYPASSRLS")
            )

    @contextlib.asynccontextmanager
    async def tenant(self, tenant_id: str) -> AsyncIterator[asyncpg.Connection]:
        """A transaction scoped to one tenant. Everything inside is filtered by RLS."""
        if not tenant_id:
            # An empty GUC makes every policy's nullif(...) comparison NULL, which
            # is false for every row. That fails closed, which is right, but it
            # produces "no data" rather than an error and that is a confusing way
            # to learn the caller forgot to authenticate.
            raise ValueError("tenant_id is required")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "select set_config('app.tenant_id', $1, true)", str(tenant_id)
                )
                yield conn

    @contextlib.asynccontextmanager
    async def unscoped(self) -> AsyncIterator[asyncpg.Connection]:
        """A transaction with NO tenant set, for control-plane work only.

        Named to be conspicuous in review. RLS still applies, so this sees nothing
        tenant-scoped; it exists for the scheduler's own tables and for functions
        that take the tenant as an argument.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                yield conn


async def fetch_kpis(
    conn: asyncpg.Connection,
    *,
    days: int,
    connection_id: str | None = None,
) -> list[dict[str, Any]]:
    """Daily rows for the last `days` complete days, newest last.

    Reads STORED snapshots and never a vendor API, which is the property the whole
    architecture exists for. The date filter uses observed_on, the date the
    platform attributed the value to, so a late sync cannot shuffle a row into the
    wrong window.
    """
    sql = """
        select observed_on,
               metric_key,
               value_numeric,
               currency
          from metric_snapshot
         where observed_on > current_date - $1::int
           and ($2::uuid is null or connection_id = $2::uuid)
         order by observed_on, metric_key
    """
    rows = await conn.fetch(sql, days, connection_id)
    return [dict(r) for r in rows]


async def latest_runs(conn: asyncpg.Connection, limit: int = 10) -> list[dict[str, Any]]:
    """Recent sync runs. Surfacing these is what makes "the number looks wrong"
    answerable: without a run log there is no way to tell a stale figure from a
    correct one."""
    rows = await conn.fetch(
        """
        select id, connection_id, trigger, status, started_at, finished_at,
               rows_written, api_calls, throttle_waits, error_detail
          from sync_run
         order by started_at desc
         limit $1
        """,
        limit,
    )
    return [dict(r) for r in rows]
