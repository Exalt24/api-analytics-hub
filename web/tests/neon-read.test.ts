/**
 * The Reports page's Kysely read path against a hosted Neon database. Skipped unless NEON_APP_URL is set.
 *
 * NEON_APP_URL      a login role that is NOT a superuser and is a member of app_request (the owner role Neon creates holds BYPASSRLS)
 * NEON_TENANT_ID    the tenant that `seed_demo.py` printed
 * NEON_DIRECT_URL   optional: the owner role, to show the startup refusal fires for it
 *
 * Database: the one `seed_demo.py` filled (ANALYTICS_TEST_DSN pointed at Neon). Nothing here writes.
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { sql } from "kysely";
import { NoDataError, readMonthlySnapshot } from "@/lib/report/data";
import { UnsafeDatabaseRole, assertRoleCannotBypassRls, createDb, pgDialect, withTenant, type Db } from "@/lib/server/db";

const appUrl = process.env.NEON_APP_URL ?? "";
const tenant = process.env.NEON_TENANT_ID ?? "";
const ownerUrl = process.env.NEON_DIRECT_URL ?? "";
const run = describe.skipIf(!appUrl || !tenant);

function monthOf(offset: number): string {
  const d = new Date();
  const m = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + offset, 1));
  return `${m.getUTCFullYear()}-${String(m.getUTCMonth() + 1).padStart(2, "0")}`;
}

run("the Kysely read path against Neon", () => {
  let db: Db;
  beforeAll(() => {
    db = createDb(pgDialect(appUrl, 4));
  });
  afterAll(async () => {
    await db.destroy();
  });

  it("starts: the login role is neither a superuser nor BYPASSRLS", async () => {
    await assertRoleCannotBypassRls(db);
  });

  it("reads this month and last month for the seeded tenant and prints the counts", async () => {
    const out: string[] = [];
    for (const offset of [-1, 0]) {
      const month = monthOf(offset);
      const data = await withTenant(db, tenant, (trx) => readMonthlySnapshot(trx, month));
      expect(data.tenantName).toContain("invented data");
      expect(data.daily.length).toBeGreaterThan(0);
      out.push(
        `${month}: ${data.daily.length} daily rows, ` +
          data.totals.map((t) => `${t.metricKey}=${t.total} over ${t.daysWithData} days`).join(", "),
      );
    }
    console.log(`NEON READ ${out.join(" | ")}`);
  });

  it("counts the tenant's snapshot rows through RLS, and sees none without a tenant", async () => {
    const scoped = await withTenant(db, tenant, async (trx) => (await sql<{ n: string }>`select count(*)::text as n from metric_snapshot`.execute(trx)).rows[0].n);
    const other = await withTenant(db, "00000000-0000-4000-8000-000000000000", async (trx) => (await sql<{ n: string }>`select count(*)::text as n from metric_snapshot`.execute(trx)).rows[0].n);
    console.log(`NEON RLS metric_snapshot rows: seeded tenant ${scoped}, a different tenant id ${other}`);
    expect(Number(scoped)).toBeGreaterThan(0);
    expect(other).toBe("0");
  });

  it("a different tenant id gets no report, not this tenant's", async () => {
    await expect(
      withTenant(db, "00000000-0000-4000-8000-000000000000", (trx) => readMonthlySnapshot(trx, monthOf(0))),
    ).rejects.toThrow(/tenant not visible|no stored data/);
    expect(NoDataError).toBeDefined();
  });

  it.skipIf(!ownerUrl)("the owner role is refused at startup because it holds BYPASSRLS", async () => {
    const ownerDb = createDb(pgDialect(ownerUrl, 2));
    try {
      await expect(assertRoleCannotBypassRls(ownerDb, { role: "neondb_owner" })).rejects.toBeInstanceOf(UnsafeDatabaseRole);
    } finally {
      await ownerDb.destroy();
    }
  });
});
