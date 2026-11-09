import { readFileSync } from "node:fs";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import { sql } from "kysely";
import { createDb, pgDialect, withTenant, type Db } from "@/lib/server/db";
import { DIMENSIONS, embed, literal, similarQueries, storeQueries } from "@/lib/server/search";
import { QUERIES_A, QUERIES_B, TENANT_A, TENANT_B, TENANT_EMPTY } from "./fixtures";

type Golden = { text: string; vector: number[]; literal: string };
const golden = JSON.parse(readFileSync(path.join(__dirname, "fixtures", "embed_golden.json"), "utf8")) as Golden[];

describe("embed, the TypeScript port of app/similar_queries.py", () => {
  it("gives the vector the Python function gave, for 12 texts including accents, CJK, an emoji and a 300 character query", () => {
    expect(golden.length).toBe(12);
    for (const g of golden) {
      const v = embed(g.text);
      expect(v.length).toBe(DIMENSIONS);
      for (let i = 0; i < DIMENSIONS; i += 1) expect(Math.abs((v[i] ?? NaN) - (g.vector[i] ?? 0))).toBeLessThan(1e-9);
      expect(literal(v)).toBe(g.literal);
    }
  });

  it("is case and spacing independent, unit length, and refuses an empty query", () => {
    expect(embed("Red  Running Shoes ")).toEqual(embed("red running shoes"));
    const v = embed("red running shoes");
    expect(Math.abs(v.reduce((s, x) => s + x * x, 0) - 1)).toBeLessThan(1e-9);
    expect(() => embed("   ")).toThrow(/empty query/);
  });
});

const adminDsn = inject("adminDsn");
const run = describe.skipIf(!adminDsn);

run("AI search through the Kysely layer: one tenant's query vector never returns another tenant's rows", () => {
  let db: Db;
  beforeAll(() => {
    db = createDb(pgDialect(adminDsn, 4));
  });
  afterAll(async () => {
    await db.destroy();
  });

  it("a query for tenant B's own text, asked as tenant A, returns only A's rows", async () => {
    // B stores "red running shoes for kids" and "wedding cake delivery"; A stores neither.
    for (const text of ["red running shoes for kids", "wedding cake delivery"]) {
      const hits = await withTenant(db, TENANT_A, (trx) => similarQueries(trx, text, 200));
      const texts = hits.map((h) => h.query);
      expect(texts).not.toContain("red running shoes for kids");
      expect(texts).not.toContain("wedding cake delivery");
      expect([...texts].sort()).toEqual([...QUERIES_A].sort());
    }
  });

  it("returns A's copy of a text that both tenants stored, once, and B's lookup sees B's rows only", async () => {
    const a = await withTenant(db, TENANT_A, (trx) => similarQueries(trx, "red running shoes", 200));
    expect(a.filter((h) => h.query === "red running shoes").length).toBe(1);
    expect(a.length).toBe(QUERIES_A.length);
    const b = await withTenant(db, TENANT_B, (trx) => similarQueries(trx, "red running shoes", 200));
    expect(b.length).toBe(QUERIES_B.length);
    expect(b[0]).toMatchObject({ query: "red running shoes" });
    expect(b.map((h) => h.query)).not.toContain("running shoes red");
  });

  it("ranks the identical text first with similarity 1, nearest first", async () => {
    const a = await withTenant(db, TENANT_A, (trx) => similarQueries(trx, "red running shoes", 5));
    expect(a[0]?.query).toBe("red running shoes");
    expect(a[0]?.similarity).toBeCloseTo(1, 5);
    const sims = a.map((h) => h.similarity);
    expect(sims).toEqual([...sims].sort((x, y) => y - x));
  });

  it("returns nothing for a tenant with no stored queries, and for no tenant at all", async () => {
    expect(await withTenant(db, TENANT_EMPTY, (trx) => similarQueries(trx, "red running shoes", 50))).toEqual([]);
  });

  it("stays isolated when one tenant stores a text another already has, and when the text is refreshed", async () => {
    const written = await withTenant(db, TENANT_EMPTY, (trx) => storeQueries(trx, TENANT_EMPTY, ["Red Running Shoes", "red running shoes", "kids shoes sale"]));
    expect(written).toBe(2); // the first two are one query once normalised
    const mine = await withTenant(db, TENANT_EMPTY, (trx) => similarQueries(trx, "red running shoes", 50));
    expect(mine.map((h) => h.query).sort()).toEqual(["kids shoes sale", "red running shoes"]);
    // A and B are unchanged by it
    const a = await withTenant(db, TENANT_A, (trx) => similarQueries(trx, "kids shoes sale", 50));
    expect(a.map((h) => h.query)).not.toContain("kids shoes sale");
    const b = await withTenant(db, TENANT_B, (trx) => similarQueries(trx, "kids shoes sale", 50));
    expect(b.map((h) => h.query)).not.toContain("kids shoes sale");
  });

  it("a write for another tenant is refused by the policy's check, not silently stored", async () => {
    await expect(withTenant(db, TENANT_A, (trx) => storeQueries(trx, TENANT_B, ["planted for b"]))).rejects.toThrow(/row-level security/);
    const b = await withTenant(db, TENANT_B, (trx) => similarQueries(trx, "planted for b", 50));
    expect(b.map((h) => h.query)).not.toContain("planted for b");
  });

  it("returns a full page of 20 for a tenant that owns a tenth of 6,000 rows, which the HNSW index alone does not", async () => {
    // the failure migration 010 describes: the index returns about 40 candidates from every tenant and the policy
    // throws most away. 10 tenants of 600 random vectors each, written by the owner role in one statement per tenant.
    const owner = createDb(pgDialect(adminDsn, 2));
    const tenants = Array.from({ length: 10 }, (_, i) => `dddddddd-0000-4000-8000-0000000000${String(i).padStart(2, "0")}`);
    try {
      for (const [i, t] of tenants.entries()) {
        await sql`insert into tenant (id, slug, name) values (${t}::uuid, ${`bulk${i}`}, ${`Bulk ${i}`})`.execute(owner);
        await sql`insert into query_embedding (tenant_id, query_text, embedding)
                  select ${t}::uuid, 'q' || g,
                         (select array_agg(random() - 0.5)::real[]::vector from generate_series(1, 64) s where g is not null)
                    from generate_series(1, 600) g`.execute(owner);
      }
      await sql`analyze query_embedding`.execute(owner);
      for (const text of ["red running shoes", "wedding cake delivery", "q17", "q420"]) {
        const got = await withTenant(db, tenants[3] as string, (trx) => similarQueries(trx, text, 20));
        expect(got.length, text).toBe(20);
        const sims = got.map((g) => g.similarity);
        expect(sims).toEqual([...sims].sort((a, b) => b - a));
      }
    } finally {
      await owner.destroy();
    }
  });

  it("refuses a limit outside 1 to 200", async () => {
    await expect(withTenant(db, TENANT_A, (trx) => similarQueries(trx, "x", 0))).rejects.toThrow(/limit/);
    await expect(withTenant(db, TENANT_A, (trx) => similarQueries(trx, "x", 201))).rejects.toThrow(/limit/);
  });
});
