import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import { sql } from "kysely";
import { AuthError, resolvePrincipal } from "@/lib/server/auth";
import { NoDataError, readMonthlySnapshot } from "@/lib/report/data";
import { UnsafeDatabaseRole, assertRoleCannotBypassRls, createDb, pgDialect, unscoped, withTenant, type Db } from "@/lib/server/db";
import { KEY_A, KEY_A_REVOKED, KEY_B, TENANT_A, TENANT_B, TENANT_EMPTY } from "./fixtures";

const adminDsn = inject("adminDsn");
const appDsn = inject("appDsn");
const run = describe.skipIf(!adminDsn);

run("the Kysely layer reads one tenant's snapshots under forced row level security", () => {
  let db: Db;
  beforeAll(() => {
    db = createDb(pgDialect(adminDsn, 4));
  });
  afterAll(async () => {
    await db.destroy();
  });

  it("returns tenant A's October as the fixture rows say, with the second connection folded in", async () => {
    const data = await withTenant(db, TENANT_A, (trx) => readMonthlySnapshot(trx, "2026-10"));
    expect(data.tenantName).toBe("Alpha Outfitters");
    expect(data.daily.map((r) => [r.date, r.metricKey, r.currency, r.value])).toEqual([
      ["2026-10-01", "gross_sales", "USD", 123456n],
      ["2026-10-01", "orders", null, 15n], // 12 from one connection, 3 from the other
      ["2026-10-01", "refunds", "USD", 1000n],
      ["2026-10-02", "gross_sales", "USD", 98765n],
      ["2026-10-02", "orders", null, 8n],
      ["2026-10-02", "refunds", "USD", 0n],
      ["2026-10-03", "gross_sales", "USD", 150000n],
      ["2026-10-03", "orders", null, 15n],
      ["2026-10-03", "refunds", "USD", 2550n],
    ]);
    expect(data.totals.map((t) => [t.metricKey, t.total, t.daysWithData])).toEqual([
      ["gross_sales", 372221n, 3],
      ["orders", 38n, 3],
      ["refunds", 3550n, 3],
    ]);
  });

  it("never returns a gauge, and never a day outside the month", async () => {
    const data = await withTenant(db, TENANT_A, (trx) => readMonthlySnapshot(trx, "2026-10"));
    const keys = new Set(data.daily.map((r) => r.metricKey));
    expect(keys.has("customers")).toBe(false);
    expect(keys.has("avg_order_value")).toBe(false);
    for (const r of data.daily) expect(r.date >= "2026-10-01" && r.date <= "2026-10-31").toBe(true);
    const sept = await withTenant(db, TENANT_A, (trx) => readMonthlySnapshot(trx, "2026-09"));
    expect(sept.daily.map((r) => [r.date, r.value])).toEqual([
      ["2026-09-15", 21n],
      ["2026-09-30", 99n],
    ]);
  });

  it("never returns the second tenant's rows, for either tenant", async () => {
    const a = await withTenant(db, TENANT_A, (trx) => readMonthlySnapshot(trx, "2026-10"));
    const b = await withTenant(db, TENANT_B, (trx) => readMonthlySnapshot(trx, "2026-10"));
    expect(a.tenantName).toBe("Alpha Outfitters");
    expect(b.tenantName).toBe("Bravo Bakery");
    const aValues = a.daily.map((r) => r.value);
    expect(aValues).not.toContain(777n);
    expect(aValues).not.toContain(555n);
    expect(aValues).not.toContain(7770000n);
    expect(a.daily.some((r) => r.currency === "EUR")).toBe(false);
    expect(b.daily.map((r) => [r.date, r.metricKey, r.currency, r.value])).toEqual([
      ["2026-10-01", "gross_sales", "EUR", 7770000n],
      ["2026-10-01", "orders", null, 777n],
      ["2026-10-02", "orders", null, 555n],
    ]);
  });

  it("filters a raw query with no WHERE clause, and a WHERE for the other tenant matches nothing", async () => {
    const result = await withTenant(db, TENANT_A, async (trx) => {
      const tenants = await trx.selectFrom("tenant").select(["id"]).execute();
      const snaps = await trx.selectFrom("metric_snapshot").select("tenant_id").distinct().execute();
      const other = await trx.selectFrom("metric_snapshot").selectAll().where("tenant_id", "=", TENANT_B).execute();
      const keys = await trx.selectFrom("query_embedding").select("tenant_id").distinct().execute();
      return { tenants, snaps, other, keys };
    });
    expect(result.tenants.map((t) => t.id)).toEqual([TENANT_A]);
    expect(result.snaps.map((s) => s.tenant_id)).toEqual([TENANT_A]);
    expect(result.other).toEqual([]);
    expect(result.keys.map((k) => k.tenant_id)).toEqual([TENANT_A]);
  });

  it("stays isolated when 40 requests for two tenants share a pool of two connections", async () => {
    const pool = createDb(pgDialect(adminDsn, 2));
    try {
      const jobs = Array.from({ length: 40 }, (_, i) => {
        const tenant = i % 2 === 0 ? TENANT_A : TENANT_B;
        return withTenant(pool, tenant, async (trx) => {
          await sql`select pg_sleep(0.01)`.execute(trx);
          const rows = await trx.selectFrom("metric_snapshot").select("tenant_id").distinct().execute();
          return { tenant, seen: rows.map((r) => r.tenant_id) };
        });
      });
      for (const r of await Promise.all(jobs)) expect(r.seen).toEqual([r.tenant]);
    } finally {
      await pool.destroy();
    }
  });

  it("sees nothing when no tenant is set, and a tenant with no rows gets a no-data error", async () => {
    const none = await unscoped(db, (trx) => trx.selectFrom("metric_snapshot").selectAll().execute());
    expect(none).toEqual([]);
    await expect(withTenant(db, TENANT_EMPTY, (trx) => readMonthlySnapshot(trx, "2026-10"))).rejects.toBeInstanceOf(NoDataError);
  });

  it("refuses a missing or malformed tenant id before running any SQL", async () => {
    await expect(withTenant(db, "", async () => 1)).rejects.toThrow(/tenant_id/);
    await expect(withTenant(db, "not-a-uuid", async () => 1)).rejects.toThrow(/tenant_id/);
    await expect(withTenant(db, `${TENANT_A}' or true --`, async () => 1)).rejects.toThrow(/tenant_id/);
  });

  it("keeps the tenant setting and the role inside the transaction: the pooled connection comes back clean", async () => {
    const one = createDb(pgDialect(adminDsn, 1)); // one connection, so the next query is on the same one
    try {
      await withTenant(one, TENANT_A, async () => 1);
      const after = await sql<{ tenant: string | null; who: string }>`
        select current_setting('app.tenant_id', true) as tenant, current_user::text as who`.execute(one);
      expect(after.rows[0]?.tenant ?? "").toBe("");
      expect(after.rows[0]?.who).toBe("analytics"); // the login role, not app_request
    } finally {
      await one.destroy();
    }
  });

  it("holds for a login role that is not a superuser, with no role switch of its own", async () => {
    const app = createDb(pgDialect(appDsn, 2));
    try {
      const a = await withTenant(app, TENANT_A, (trx) => readMonthlySnapshot(trx, "2026-10"), { role: "web_test_login" });
      expect(a.tenantName).toBe("Alpha Outfitters");
      expect(a.daily.map((r) => r.value)).not.toContain(777n);
      await assertRoleCannotBypassRls(app, { role: "web_test_login" });
    } finally {
      await app.destroy();
    }
  });

  it("refuses to start as a superuser, which would make every policy inert", async () => {
    await expect(assertRoleCannotBypassRls(db, { role: "analytics" })).rejects.toBeInstanceOf(UnsafeDatabaseRole);
    await expect(assertRoleCannotBypassRls(db)).resolves.toBeUndefined(); // app_request is fine
  });

  it("returns a superuser every tenant's rows, which is the failure the assertion above exists for", async () => {
    // the control for the isolation tests: without the role switch the same query sees both tenants
    const raw = await sql<{ n: string }>`select count(distinct tenant_id)::text as n from metric_snapshot`.execute(db);
    expect(raw.rows[0]?.n).toBe("2");
  });
});

run("an API key resolves to its tenant and nothing else", () => {
  let db: Db;
  beforeAll(() => {
    db = createDb(pgDialect(adminDsn, 2));
  });
  afterAll(async () => {
    await db.destroy();
  });

  it("maps each key to its own tenant and role", async () => {
    expect(await resolvePrincipal(db, KEY_A)).toMatchObject({ tenantId: TENANT_A, role: "viewer" });
    expect(await resolvePrincipal(db, KEY_B)).toMatchObject({ tenantId: TENANT_B, role: "operator" });
  });

  it("refuses an unknown, an empty and a revoked key", async () => {
    await expect(resolvePrincipal(db, "aah_not_a_real_key_000000")).rejects.toMatchObject({ status: 401 });
    await expect(resolvePrincipal(db, "")).rejects.toBeInstanceOf(AuthError);
    await expect(resolvePrincipal(db, KEY_A_REVOKED)).rejects.toMatchObject({ status: 401, message: "API key revoked" });
  });
});
