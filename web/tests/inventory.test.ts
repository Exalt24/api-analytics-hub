import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import pg from "pg";
import { NoPlanError, compareLines, displayName, readPlan, summarise, type PlanLine } from "@/lib/inventory/data";
import { addPromotion, buildPoCsv, buildSheetsPaste, loadPlan, poFilename, runPoExport } from "@/lib/inventory/export";
import { LABEL_TEXT, SHORTAGE_TEXT, basisText, coverText, matchesFilter, trendText } from "@/lib/inventory/format";
import { poRequestSchema, promoFormSchema, promoRequestSchema } from "@/lib/inventory/input";
import { buildPurchaseOrders, orderable, poRows } from "@/lib/inventory/po";
import { toView } from "@/lib/inventory/view";
import { createDb, pgDialect, withTenant, type Db } from "@/lib/server/db";
import { KEY_A, KEY_A_REVOKED, KEY_B, PLAN_DAY, TENANT_A, TENANT_B, TENANT_EMPTY, V } from "./fixtures";
import { readCsv, readXlsx } from "./readback";

const adminDsn = inject("adminDsn");
const run = describe.skipIf(!adminDsn);

/** A plan line with every field named, so a test changes only what it is about. */
function line(over: Partial<PlanLine>): PlanLine {
  return {
    variantId: "v", product: "Tee", variant: "Default Title", sku: "T", supplier: "Acme", status: "ok", stockReadOn: PLAN_DAY, available: 10, incoming: 0, velocity: 2, velocitySd: 0,
    countedDays: 20, outDays: 8, unknownDays: 0, windowDays: 28, unitsRecent: 40, unitsPrior: 10, trendRatio: null, revenueMinor: 100000n, currency: "USD", coverDays: 5,
    forecastCoverDays: 5, stockoutDate: null, leadTimeDays: 9, safetyStock: 0, reorderPoint: 18, needsReorder: true, suggestedQty: 60, unitCostMinor: 800n, abcClass: "A",
    label: "best_seller", shortage: "cover_below_lead_time", inboundSource: "none", inboundExpectedOn: null, inboundLate: false, ...over,
  };
}

describe("the purchase order drafts", () => {
  it("groups the lines that need an order by supplier, with hand-worked totals and dates", () => {
    const lines = [
      line({ variantId: "1", sku: "TEE-1", product: "Studio Tee" }),
      line({ variantId: "2", sku: "SOCK-L", product: "Wool Sock", variant: "Large", supplier: "Borealis", leadTimeDays: 30, coverDays: 0, suggestedQty: 120, unitCostMinor: null }),
      line({ variantId: "3", sku: "HAT-1", product: "Hat", needsReorder: false, suggestedQty: 0 }),
      line({ variantId: "4", sku: "MUG-1", product: "Mug", supplier: null, status: "no_supplier", leadTimeDays: null }),
      line({ variantId: "5", sku: "GIFT", product: "Gift card", status: "untracked" }),
      line({ variantId: "6", sku: "TEE-2", product: "Studio Tee", variant: "Blue", suggestedQty: 12, unitCostMinor: 1250n, coverDays: 3 }),
      line({ variantId: "7", sku: "ZERO", product: "Zero", needsReorder: true, suggestedQty: 0 }),
      line({ variantId: "8", sku: "ABOVE", product: "Above", needsReorder: false, suggestedQty: 24 }),
    ];
    const orders = buildPurchaseOrders(lines, "2026-11-24");
    expect(orders.map((o) => o.supplier)).toEqual(["Acme", "Borealis"]);
    const [acme, borealis] = orders;
    expect(acme?.expectedOn).toBe("2026-12-03"); // 24 November plus 9 days
    expect(acme?.lines.map((l) => [l.sku, l.quantity, l.lineTotalMinor])).toEqual([
      ["TEE-2", 12, 15000n], // least cover first: 12 x 12.50
      ["TEE-1", 60, 48000n], // 60 x 8.00
    ]);
    expect(acme?.totalMinor).toBe(63000n);
    expect(acme?.linesWithoutCost).toBe(0);
    expect(borealis?.expectedOn).toBe("2026-12-24"); // plus 30 days
    expect(borealis?.totalMinor).toBeNull(); // no line has a cost: no total is invented
    expect(borealis?.linesWithoutCost).toBe(1);
    expect(orderable(lines).map((l) => l.sku)).toEqual(["TEE-1", "SOCK-L", "TEE-2"]);
  });

  it("a supplier filter keeps one supplier, and an unknown supplier gives nothing", () => {
    const lines = [line({ variantId: "1" }), line({ variantId: "2", supplier: "Borealis" })];
    expect(buildPurchaseOrders(lines, PLAN_DAY, "Borealis").map((o) => o.supplier)).toEqual(["Borealis"]);
    expect(buildPurchaseOrders(lines, PLAN_DAY, "Nobody")).toEqual([]);
  });

  it("a lead time that crosses a month and a year end lands on the right day", () => {
    const o = buildPurchaseOrders([line({ leadTimeDays: 45 })], "2026-12-01");
    expect(o[0]?.expectedOn).toBe("2027-01-15");
  });

  it("writes the CSV exactly, blank where there is no cost and never a zero", () => {
    const orders = buildPurchaseOrders(
      [
        line({ variantId: "1", sku: "TEE-1", product: "Studio Tee" }),
        line({ variantId: "2", sku: "SOCK-L", product: "Wool Sock", variant: "Large", supplier: "Borealis", leadTimeDays: 30, coverDays: 0, suggestedQty: 120, unitCostMinor: null }),
      ],
      PLAN_DAY,
    );
    expect(buildPoCsv(poRows(orders))).toBe(
      [
        "supplier,expected_on,sku,description,quantity,unit_cost,line_total,currency,days_of_cover,class",
        "Acme,2026-12-03,TEE-1,Studio Tee,60,8.00,480.00,USD,5.0,best_seller",
        "Borealis,2026-12-24,SOCK-L,Wool Sock - Large,120,,,USD,0.0,best_seller",
        "",
      ].join("\r\n"),
    );
  });

  it("a zero decimal currency is written without cents", () => {
    const rows = poRows(buildPurchaseOrders([line({ currency: "JPY", unitCostMinor: 800n })], PLAN_DAY));
    expect(rows[1]?.slice(5, 8)).toEqual(["800", "48000", "JPY"]);
  });

  it("a description that would run as a formula is defused in the CSV and the paste", () => {
    const rows = poRows(buildPurchaseOrders([line({ product: "=HYPERLINK(\"http://x\")" })], PLAN_DAY));
    expect(buildPoCsv(rows)).toContain("'=HYPERLINK");
    expect(buildSheetsPaste(rows)).toContain("\t'=HYPERLINK");
  });

  it("the Sheets paste is tab separated, one row a line, and a tab inside a cell cannot add a column", () => {
    const rows = poRows(buildPurchaseOrders([line({ product: "Odd\tName\nHere" })], PLAN_DAY));
    const paste = buildSheetsPaste(rows);
    const parsed = paste.trimEnd().split("\r\n").map((l) => l.split("\t"));
    expect(parsed).toHaveLength(2);
    expect(parsed.every((r) => r.length === 10)).toBe(true);
    expect(parsed[1]?.[3]).toBe("Odd Name Here");
  });

  it("names the file for the plan date, the format and the supplier", () => {
    expect(poFilename("2026-11-24", "csv")).toBe("purchase-orders-2026-11-24.csv");
    expect(poFilename("2026-11-24", "sheets", "Acme Goods & Co.")).toBe("purchase-orders-2026-11-24-acme-goods-co.tsv");
    expect(poFilename("2026-11-24", "xlsx")).toBe("purchase-orders-2026-11-24.xlsx");
  });
});

describe("reading order, summary and words", () => {
  it("lists shortages first (out of stock, then cover, then projected), then what needs ordering, then least cover", () => {
    const lines = [
      line({ variantId: "a", product: "A", shortage: null, needsReorder: false, coverDays: 1 }),
      line({ variantId: "b", product: "B", shortage: "stockout_projected", coverDays: 12 }),
      line({ variantId: "c", product: "C", shortage: "cover_below_lead_time", coverDays: 4 }),
      line({ variantId: "d", product: "D", shortage: "out_of_stock", coverDays: 0 }),
      line({ variantId: "e", product: "E", shortage: null, needsReorder: true, coverDays: 1 }),
      line({ variantId: "f", product: "F", shortage: null, needsReorder: false, coverDays: null }),
    ];
    expect(lines.slice().sort(compareLines).map((l) => l.variantId)).toEqual(["d", "c", "b", "e", "a", "f"]);
  });

  it("counts the summary and names a variant by its product unless it has a real variant title", () => {
    const lines = [
      line({ label: "best_seller" }),
      line({ label: "rising", shortage: null }),
      line({ label: "slow_mover", needsReorder: false, suggestedQty: 0, shortage: null }),
      line({ label: "steady", shortage: null, needsReorder: true, suggestedQty: 0 }),
    ];
    expect(summarise(lines)).toEqual({ variants: 4, shortages: 1, toReorder: 2, bestSellers: 1, rising: 1, slowMovers: 1 });
    expect(displayName({ product: "Tee", variant: "Default Title" })).toBe("Tee");
    expect(displayName({ product: "Tee", variant: "" })).toBe("Tee");
    expect(displayName({ product: "Tee", variant: "Large" })).toBe("Tee - Large");
  });

  it("states cover, trend, basis and the filters in the words the page shows", () => {
    expect(coverText(null)).toBe("no sales");
    expect(coverText(5)).toBe("5.0");
    expect(trendText(null, "rising")).toBe("new");
    expect(trendText(null, "steady")).toBe("-");
    expect(trendText(2.04, "rising")).toBe("2.0x");
    expect(basisText(20, 28, 8, 0)).toBe("20 of 28 days counted; 8 out of stock, 0 assumed on sale");
    expect(LABEL_TEXT.best_seller).toBe("Best seller");
    expect(SHORTAGE_TEXT.cover_below_lead_time).toBe("Cover under lead time");
    const l = { label: "rising" as const, shortage: null, needsReorder: true, suggestedQty: 5 };
    expect([matchesFilter(l, "all"), matchesFilter(l, "short"), matchesFilter(l, "reorder"), matchesFilter(l, "rising"), matchesFilter(l, "steady")]).toEqual([true, false, true, true, false]);
    expect(matchesFilter({ ...l, needsReorder: true, suggestedQty: 0 }, "reorder")).toBe(false);
  });
});

describe("the request schemas", () => {
  it("accepts a good promotion and refuses a bad factor, a bad date and a reversed range", () => {
    const good = { startsOn: "2026-12-01", endsOn: "2026-12-07", factor: 1.5 };
    expect(promoFormSchema.safeParse(good).success).toBe(true);
    expect(promoFormSchema.safeParse({ ...good, factor: 0 }).success).toBe(false);
    expect(promoFormSchema.safeParse({ ...good, factor: 20.5 }).success).toBe(false);
    expect(promoFormSchema.safeParse({ ...good, factor: 20 }).success).toBe(true);
    expect(promoFormSchema.safeParse({ ...good, startsOn: "2026-13-01" }).success).toBe(false);
    expect(promoFormSchema.safeParse({ ...good, startsOn: "2026-12-1", endsOn: "2026-12-5" }).success).toBe(false);
    expect(promoFormSchema.safeParse({ ...good, startsOn: "2026-12-08" }).success).toBe(false);
    expect(promoFormSchema.safeParse({ ...good, endsOn: "2026-12-01" }).success).toBe(true);
    expect(promoRequestSchema.safeParse({ ...good, apiKey: "short" }).success).toBe(false);
    expect(promoRequestSchema.safeParse({ ...good, apiKey: "long_enough_key" }).success).toBe(true);
  });

  it("an export request needs a known format and a key, and ignores a tenant id", () => {
    expect(poRequestSchema.safeParse({ apiKey: "long_enough_key", format: "pdf" }).success).toBe(false);
    const parsed = poRequestSchema.safeParse({ apiKey: "long_enough_key", format: "csv", tenantId: TENANT_B });
    expect(parsed.success).toBe(true);
    expect(parsed.success && "tenantId" in parsed.data).toBe(false);
  });
});

run("the Inventory page's data layer on the real database", () => {
  let db: Db;
  let admin: pg.Client;
  beforeAll(async () => {
    db = createDb(pgDialect(adminDsn, 4));
    admin = new pg.Client({ connectionString: adminDsn });
    await admin.connect();
  });
  afterAll(async () => {
    await admin.end();
    await db.destroy();
  });
  const deps = () => ({ db });

  it("reads tenant A's stored plan as the fixture rows say, shortages first", async () => {
    const plan = await withTenant(db, TENANT_A, (trx) => readPlan(trx));
    expect(plan.tenantName).toBe("Alpha Outfitters");
    expect(plan.asOf).toBe(PLAN_DAY);
    expect(plan.lines.map((l) => [l.sku, l.shortage, l.suggestedQty])).toEqual([
      ["SOCK-L", "out_of_stock", 120],
      ["TEE-1", "cover_below_lead_time", 60],
      ["CAP-R", null, 0],
      ["MUG-1", null, 0],
      ["HAT-1", null, 0],
    ]);
    const tee = plan.lines.find((l) => l.sku === "TEE-1");
    expect(tee).toMatchObject({ available: 10, velocity: 2, coverDays: 5, reorderPoint: 18, leadTimeDays: 9, unitCostMinor: 800n, supplier: "Acme Goods", abcClass: "A", label: "best_seller", stockReadOn: PLAN_DAY, stockoutDate: "2026-11-29" });
    expect(plan.alerts.map((a) => [a.kind, a.sku, a.eventRef]).sort()).toEqual([
      ["cover_below_lead_time", "TEE-1", "2026-11-14"],
      ["out_of_stock", "SOCK-L", "start"],
    ]);
  });

  it("a tenant sees only its own plan, and a tenant with no plan gets a named error", async () => {
    const b = await withTenant(db, TENANT_B, (trx) => readPlan(trx));
    expect(b.lines.map((l) => l.sku)).toEqual(["LOAF-1"]);
    expect(b.alerts).toEqual([]);
    await expect(withTenant(db, TENANT_EMPTY, (trx) => readPlan(trx))).rejects.toBeInstanceOf(NoPlanError);
  });

  it("loadPlan resolves the key to the tenant, shapes the page's view and refuses a bad or revoked key", async () => {
    const ok = await loadPlan(deps(), { apiKey: KEY_A });
    expect(ok.ok).toBe(true);
    if (!ok.ok) return;
    expect(ok.plan.summary).toEqual({ variants: 5, shortages: 2, toReorder: 2, bestSellers: 1, rising: 1, slowMovers: 1 });
    expect(ok.plan.orders.map((o) => [o.supplier, o.expectedOn, o.total, o.linesWithoutCost])).toEqual([
      ["Acme Goods", "2026-12-03", "$480.00", 0],
      ["Borealis Textiles", "2026-12-24", null, 1],
    ]);
    expect(ok.plan.lines.find((l) => l.sku === "TEE-1")?.revenue).toBe("$1,000.00");
    expect(JSON.stringify(ok)).not.toContain("LOAF-1"); // tenant B's variant never reaches tenant A's page
    expect(JSON.parse(JSON.stringify(ok))).toBeTruthy(); // serialisable: no bigint left in the view
    expect(await loadPlan(deps(), { apiKey: "not_a_real_key_at_all" })).toMatchObject({ ok: false, code: "unauthorized" });
    expect(await loadPlan(deps(), { apiKey: KEY_A_REVOKED })).toMatchObject({ ok: false, code: "unauthorized" });
    expect(await loadPlan(deps(), { apiKey: "x" })).toMatchObject({ ok: false, code: "invalid_input" });
    expect(await loadPlan(deps(), {})).toMatchObject({ ok: false, code: "invalid_input" });
  });

  it("toView of the same plan is the same figures the data layer read", async () => {
    const plan = await withTenant(db, TENANT_A, (trx) => readPlan(trx));
    expect(toView(plan).lines.map((l) => l.name)).toEqual(plan.lines.map((l) => displayName(l)));
  });

  it("writes tenant A's purchase orders as a CSV that reads back to the hand-worked lines", async () => {
    const r = await runPoExport(deps(), { apiKey: KEY_A, format: "csv" });
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.filename).toBe("purchase-orders-2026-11-24.csv");
    expect(r.mimeType).toBe("text/csv; charset=utf-8");
    expect(readCsv(Buffer.from(r.base64, "base64"))).toEqual([
      ["supplier", "expected_on", "sku", "description", "quantity", "unit_cost", "line_total", "currency", "days_of_cover", "class"],
      ["Acme Goods", "2026-12-03", "TEE-1", "Studio Tee", "60", "8.00", "480.00", "USD", "5.0", "best_seller"],
      ["Borealis Textiles", "2026-12-24", "SOCK-L", "Wool Sock - Large", "120", "", "", "USD", "0.0", "steady"],
    ]);
  });

  it("writes one supplier's draft as a workbook with numbers in the number cells and as a Sheets paste", async () => {
    const x = await runPoExport(deps(), { apiKey: KEY_A, format: "xlsx", supplier: "Acme Goods" });
    expect(x.ok).toBe(true);
    if (!x.ok) return;
    expect(x.filename).toBe("purchase-orders-2026-11-24-acme-goods.xlsx");
    const book = await readXlsx(Buffer.from(x.base64, "base64"));
    expect(book.sheets["Purchase orders"]).toEqual([
      ["supplier", "expected_on", "sku", "description", "quantity", "unit_cost", "line_total", "currency", "days_of_cover", "class"],
      ["Acme Goods", "2026-12-03", "TEE-1", "Studio Tee", 60, 8, 480, "USD", 5, "best_seller"],
    ]);
    const s = await runPoExport(deps(), { apiKey: KEY_A, format: "sheets", supplier: "Borealis Textiles" });
    expect(s.ok).toBe(true);
    if (!s.ok) return;
    expect(s.filename).toBe("purchase-orders-2026-11-24-borealis-textiles.tsv");
    expect(Buffer.from(s.base64, "base64").toString("utf8").trimEnd().split("\r\n")[1]).toBe("Borealis Textiles\t2026-12-24\tSOCK-L\tWool Sock - Large\t120\t\t\tUSD\t0.0\tsteady");
  });

  it("says plainly when nothing needs ordering, and refuses a bad key or format", async () => {
    expect(await runPoExport(deps(), { apiKey: KEY_B, format: "csv" })).toMatchObject({ ok: false, code: "no_data" });
    expect(await runPoExport(deps(), { apiKey: KEY_A, format: "csv", supplier: "Nobody" })).toMatchObject({ ok: false, code: "no_data" });
    expect(await runPoExport(deps(), { apiKey: KEY_A, format: "pdf" })).toMatchObject({ ok: false, code: "invalid_input" });
    expect(await runPoExport(deps(), { apiKey: "wrong_key_wrong_key", format: "csv" })).toMatchObject({ ok: false, code: "unauthorized" });
  });

  it("a viewer key cannot enter a promotion; an operator key can, for its own SKU only", async () => {
    const base = { startsOn: "2026-12-10", endsOn: "2026-12-12", factor: 2 };
    expect(await addPromotion(deps(), { ...base, apiKey: KEY_A, sku: "TEE-1" })).toMatchObject({ ok: false, code: "forbidden" });
    expect(await addPromotion(deps(), { ...base, apiKey: KEY_B, sku: "TEE-1" })).toMatchObject({ ok: false, code: "invalid_input" }); // tenant A's SKU is not visible to tenant B
    const saved = await addPromotion(deps(), { ...base, apiKey: KEY_B, sku: "LOAF-1", note: "launch" });
    expect(saved.ok).toBe(true);
    const rows = await admin.query("select tenant_id, variant_id, factor, note, entered_by, to_char(starts_on, 'YYYY-MM-DD') as s from inventory_promo where note = 'launch'");
    expect(rows.rows).toEqual([{ tenant_id: TENANT_B, variant_id: V.loaf, factor: 2, note: "launch", entered_by: expect.stringMatching(/^api key [0-9a-f]{8}$/), s: "2026-12-10" }]);
    const all = await addPromotion(deps(), { ...base, apiKey: KEY_B, note: "everything" });
    expect(all.ok).toBe(true);
    const wide = await admin.query("select variant_id from inventory_promo where note = 'everything'");
    expect(wide.rows).toEqual([{ variant_id: null }]);
    await admin.query("delete from inventory_promo where note in ('launch', 'everything')");
  });

  it("a promotion with a bad factor or dates never reaches the database", async () => {
    const before = (await admin.query("select count(*)::int as n from inventory_promo")).rows[0].n;
    for (const bad of [{ factor: 0 }, { factor: 99 }, { endsOn: "2026-11-01" }, { startsOn: "soon" }]) {
      const r = await addPromotion(deps(), { startsOn: "2026-12-10", endsOn: "2026-12-12", factor: 2, apiKey: KEY_B, ...bad });
      expect(r).toMatchObject({ ok: false, code: "invalid_input" });
    }
    expect((await admin.query("select count(*)::int as n from inventory_promo")).rows[0].n).toBe(before);
  });
});
