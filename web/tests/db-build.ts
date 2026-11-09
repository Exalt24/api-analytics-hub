/**
 * Builds a scratch database: every migration in order, then the fixture rows, plus a login
 * role that is not a superuser. Shared by the Vitest global setup and the Playwright one.
 */
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import pg from "pg";
import { seed } from "./fixtures";

export const ADMIN_DSN = process.env.ANALYTICS_TEST_DSN ?? "postgresql://analytics:analytics@127.0.0.1:5433/analytics";

export function withDatabase(dsn: string, db: string): string {
  const url = new URL(dsn);
  url.pathname = `/${db}`;
  return url.toString();
}

export type Built = { adminDsn: string; appDsn: string };

/** Why Postgres cannot be used for these tests, or "" when it can. */
export async function unavailableReason(): Promise<string> {
  const server = new pg.Client({ connectionString: ADMIN_DSN, connectionTimeoutMillis: 3000 });
  try {
    await server.connect();
  } catch (err) {
    return `Postgres is not reachable: ${String(err)}`;
  }
  try {
    const have = await server.query("select count(*)::int as n from pg_available_extensions where name = 'vector'");
    return have.rows[0].n === 1 ? "" : "this Postgres has no pgvector";
  } finally {
    await server.end();
  }
}

export async function buildDatabase(name: string): Promise<Built> {
  const server = new pg.Client({ connectionString: ADMIN_DSN });
  await server.connect();
  try {
    await server.query(`drop database if exists ${name} with (force)`);
    await server.query(`create database ${name}`);
  } finally {
    await server.end();
  }
  const adminDsn = withDatabase(ADMIN_DSN, name);
  const db = new pg.Client({ connectionString: adminDsn });
  await db.connect();
  try {
    const dir = path.resolve(__dirname, "../../migrations");
    for (const file of readdirSync(dir).filter((f) => f.endsWith(".sql")).sort()) {
      await db.query(readFileSync(path.join(dir, file), "utf8"));
    }
    await seed(db);
    // a login role that is not a superuser, so a connection that skips `set local role` still meets row level security
    await db.query("drop role if exists web_test_login");
    await db.query("create role web_test_login login password 'web_test_login' in role app_request");
  } finally {
    await db.end();
  }
  const app = new URL(adminDsn);
  app.username = "web_test_login";
  app.password = "web_test_login";
  return { adminDsn, appDsn: app.toString() };
}
