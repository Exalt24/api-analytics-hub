/**
 * Database access through Kysely, with the tenant boundary enforced by the
 * connection, the same way app/db.py does it for the Python API.
 *
 * THE MECHANISM, COPIED FROM app/db.py. Every query that touches tenant data runs
 * inside a transaction that has already had `app.tenant_id` set with
 * `set_config('app.tenant_id', $1, true)`, as the non-superuser role `app_request`,
 * so the row level security policies in migrations/001 do the filtering. Nothing in
 * this layer writes `WHERE tenant_id = ...` for isolation.
 *
 * `true` makes the setting transaction-local: it dies with the transaction and
 * cannot leak onto the next request that borrows the same pooled connection.
 *
 * ONE DIFFERENCE FROM THE PYTHON POOL. Python runs `set role` once per pooled
 * connection. Here the role is `set local role` inside each tenant transaction, which
 * needs no per-connection hook, so it reads the same through any Kysely dialect.
 * `assertRoleCannotBypassRls` is the same startup refusal: a role that is a superuser
 * or holds BYPASSRLS makes every policy inert.
 *
 * DRIVER-AGNOSTIC. Every function here takes a Kysely `Db`. The one place a driver is
 * named is `pgDialect` at the bottom, so a different Kysely dialect is passed to
 * `createDb` instead of it.
 */
import pg from "pg";
import { Kysely, PostgresDialect, sql, type Dialect, type Transaction } from "kysely";
import type { Database } from "@/lib/server/schema";

export type Db = Kysely<Database>;
export type TenantTx = Transaction<Database>;

export const REQUEST_ROLE = "app_request";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The configured role can see through row level security. A deployment error, never a query to retry. */
export class UnsafeDatabaseRole extends Error {
  constructor(message: string) {
    super(message);
    this.name = "UnsafeDatabaseRole";
  }
}

export function createDb(dialect: Dialect): Db {
  return new Kysely<Database>({ dialect });
}

export type TenantOptions = { role?: string };

/**
 * Run `fn` in a transaction scoped to one tenant. Everything inside is filtered by
 * row level security. A tenant id that is not a uuid is refused before any SQL, so a
 * caller that forgot to authenticate gets an error and not an empty report.
 */
export async function withTenant<T>(
  db: Db,
  tenantId: string,
  fn: (trx: TenantTx) => Promise<T>,
  options: TenantOptions = {},
): Promise<T> {
  if (!tenantId || !UUID.test(tenantId)) {
    throw new Error("tenant_id is required and must be a uuid");
  }
  const role = options.role ?? process.env.DATABASE_ROLE ?? REQUEST_ROLE;
  return db.transaction().execute(async (trx) => {
    await sql`set local role ${sql.id(role)}`.execute(trx);
    await sql`select set_config('app.tenant_id', ${tenantId}, true)`.execute(trx);
    return fn(trx);
  });
}

/**
 * A transaction with NO tenant set, for the one lookup that has to happen before the
 * tenant is known (the API key). Row level security still applies to every tenant
 * table inside it, so it sees nothing tenant-scoped.
 */
export async function unscoped<T>(
  db: Db,
  fn: (trx: TenantTx) => Promise<T>,
  options: TenantOptions = {},
): Promise<T> {
  const role = options.role ?? process.env.DATABASE_ROLE ?? REQUEST_ROLE;
  return db.transaction().execute(async (trx) => {
    await sql`set local role ${sql.id(role)}`.execute(trx);
    return fn(trx);
  });
}

/** Refuse to serve if the role is a superuser or holds BYPASSRLS. Mirrors `_assert_role_cannot_bypass_rls`. */
export async function assertRoleCannotBypassRls(db: Db, options: TenantOptions = {}): Promise<void> {
  const row = await unscoped(
    db,
    async (trx) => {
      const res = await sql<{ who: string; rolsuper: boolean; rolbypassrls: boolean }>`
        select current_user::text as who, rolsuper, rolbypassrls
          from pg_roles where rolname = current_user`.execute(trx);
      return res.rows[0];
    },
    options,
  );
  if (!row) throw new UnsafeDatabaseRole("cannot read the current role from pg_roles");
  if (row.rolsuper || row.rolbypassrls) {
    const why = row.rolsuper ? "a superuser" : "BYPASSRLS";
    throw new UnsafeDatabaseRole(`role ${row.who} is ${why}; use app_request`);
  }
}

/** The node-postgres dialect. The only place in this file that names a driver. */
export function pgDialect(connectionString: string, max = 10): Dialect {
  return new PostgresDialect({ pool: new pg.Pool({ connectionString, max }) });
}

let shared: Promise<Db> | undefined;

/** The process-wide Db for the app, checked once for a role that bypasses RLS. */
export function getDb(): Promise<Db> {
  if (shared) return shared;
  const started = (async () => {
    const url = process.env.DATABASE_URL;
    if (!url) throw new Error("DATABASE_URL is not set");
    const db = createDb(pgDialect(url));
    try {
      await assertRoleCannotBypassRls(db);
    } catch (err) {
      await db.destroy();
      throw err;
    }
    return db;
  })();
  shared = started;
  // a failed startup check is not cached: the next call tries (and fails) again
  started.catch(() => {
    if (shared === started) shared = undefined;
  });
  return started;
}
