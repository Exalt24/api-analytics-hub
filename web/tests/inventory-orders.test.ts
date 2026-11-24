import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import pg from "pg";
import { ORDER_ACTION_TEXT, ORDER_STATUS_TEXT, inboundText } from "@/lib/inventory/format";
import { MANUAL_STATUSES, ORDER_STATUSES, createOrderSchema, moveOrderSchema, receiptSchema, workbookRequestSchema } from "@/lib/inventory/input";
import { NEXT_STATUSES, OPEN_STATUSES, createOrder, downloadWorkbook, failureOf, loadOrders, moveOrder, receiveOrder, setExpectedDate, type OrderView } from "@/lib/inventory/orders";
import { readPlan } from "@/lib/inventory/data";
import { createDb, pgDialect, withTenant, type Db } from "@/lib/server/db";
import { KEY_A, KEY_A_OPERATOR, KEY_A_REVOKED, KEY_B, TENANT_A } from "./fixtures";

const adminDsn = inject("adminDsn");
const run = describe.skipIf(!adminDsn);

const RECEIPT_ONLY = ["partly_received", "received"];

describe("the order request schemas", () => {
  it("refuse a bad id, a move a person cannot make, a zero quantity and a tenant id nobody asked for", () => {
    const id = "11111111-1111-4111-8111-111111111111";
    expect(moveOrderSchema.safeParse({ apiKey: KEY_A_OPERATOR, orderId: id, to: "ordered" }).success).toBe(true);
    expect(moveOrderSchema.safeParse({ apiKey: KEY_A_OPERATOR, orderId: "nope", to: "ordered" }).success).toBe(false);
    for (const to of ["received", "partly_received", "draft", "bogus"]) expect(moveOrderSchema.safeParse({ apiKey: KEY_A_OPERATOR, orderId: id, to }).success).toBe(false);
    expect([...MANUAL_STATUSES]).toEqual(["ordered", "confirmed", "shipped", "cancelled"]);
    const base = { apiKey: KEY_A_OPERATOR, orderId: id, lines: [{ lineId: 1, quantity: 5 }] };
    expect(receiptSchema.safeParse(base).success).toBe(true);
    expect(receiptSchema.parse(base).allowOver).toBe(false);
    expect(receiptSchema.safeParse({ ...base, lines: [{ lineId: 1, quantity: 0 }] }).success).toBe(false);
    expect(receiptSchema.safeParse({ ...base, lines: [{ lineId: 1, quantity: 1.5 }] }).success).toBe(false);
    expect(receiptSchema.safeParse({ ...base, lines: [] }).success).toBe(false);
    expect(createOrderSchema.safeParse({ apiKey: KEY_A_OPERATOR, supplier: "  " }).success).toBe(false);
    expect("tenantId" in createOrderSchema.parse({ apiKey: KEY_A_OPERATOR, supplier: "Acme Goods", tenantId: TENANT_A })).toBe(false);
  });

  it("bound the workbook settings the same way the backend does", () => {
    expect(workbookRequestSchema.safeParse({ apiKey: KEY_A }).success).toBe(true);
    for (const bad of [{ promoFactor: 0 }, { promoFactor: 21 }, { serviceLevel: 1 }, { serviceLevel: 0.4 }, { reviewDays: -1 }, { promoFrom: "tomorrow" }]) {
      expect(workbookRequestSchema.safeParse({ apiKey: KEY_A, ...bad }).success, JSON.stringify(bad)).toBe(false);
    }
  });
});

describe("the words and the inbound text", () => {
  it("names every status and gives the buttons a person can press and no more", () => {
    expect(Object.keys(ORDER_STATUS_TEXT).sort()).toEqual([...ORDER_STATUSES].sort());
    expect(NEXT_STATUSES.ordered.map((s) => ORDER_ACTION_TEXT[s])).toEqual(["Mark confirmed", "Mark shipped", "Cancel order"]);
    expect(NEXT_STATUSES.draft.map((s) => ORDER_ACTION_TEXT[s])).toEqual(["Place order", "Cancel order"]);
    for (const s of ORDER_STATUSES) for (const next of NEXT_STATUSES[s]) expect(MANUAL_STATUSES as readonly string[]).toContain(next);
    expect(NEXT_STATUSES.shipped).not.toContain("cancelled");                              // goods in transit are not cancelled
    expect([NEXT_STATUSES.received, NEXT_STATUSES.cancelled]).toEqual([[], []]);
  });

  it("says what is inbound, from where, and when", () => {
    const base = { incoming: 0, inboundSource: "none" as const, inboundExpectedOn: null, inboundLate: false };
    expect(inboundText(base)).toBe("");
    expect(inboundText({ ...base, incoming: 45, inboundSource: "shopify" })).toBe("+45 inbound");
    expect(inboundText({ ...base, incoming: 60, inboundSource: "orders", inboundExpectedOn: "2026-12-01" })).toBe("+60 inbound, due 2026-12-01");
    expect(inboundText({ ...base, incoming: 60, inboundSource: "orders", inboundExpectedOn: "2026-11-20", inboundLate: true })).toBe("+60 inbound, was due 2026-11-20");
    expect(inboundText({ ...base, incoming: 60, inboundSource: "orders" })).toBe("+60 inbound");
  });

  it("turns a database refusal into a code and keeps the database's own words", () => {
    expect(failureOf(Object.assign(new Error("a purchase order cannot move from ordered to draft"), { code: "PO001" }))).toEqual({ ok: false, code: "conflict", message: "a purchase order cannot move from ordered to draft" });
    expect(failureOf(Object.assign(new Error("no such purchase order"), { code: "PO404" }))).toMatchObject({ code: "not_found" });
    expect(failureOf(Object.assign(new Error("a received quantity must be above zero"), { code: "PO006" }))).toMatchObject({ code: "invalid_input" });
    expect(failureOf(Object.assign(new Error("connection string with secret"), { code: "08006" }))).toEqual({ ok: false, code: "failed", message: "The request failed. Try again." });
    expect(failureOf(new Error("boom"))).toMatchObject({ code: "failed" });
  });
});

describe("the workbook download", () => {
  const bytes = Buffer.from("PK-fake-xlsx-bytes");

  function stub(status: number, seen: { url?: string; auth?: string | null }) {
    return (async (url: string | URL | Request, init?: RequestInit) => {
      seen.url = String(url);
      seen.auth = new Headers(init?.headers).get("Authorization");
      return new Response(status === 200 ? bytes : "x", { status, headers: status === 200 ? { "content-disposition": 'attachment; filename="inventory-plan-2026-11-24.xlsx"' } : {} });
    }) as typeof fetch;
  }

  it("asks the backend with the key as a bearer token and no tenant, and hands the bytes on", async () => {
    const seen: { url?: string; auth?: string | null } = {};
    const r = await downloadWorkbook({ fetchImpl: stub(200, seen), apiBase: "http://backend.test/" }, { apiKey: KEY_A, promoFactor: 1.5, promoFrom: "2026-12-01", promoTo: "2026-12-07" });
    expect(r).toEqual({
      ok: true,
      filename: "inventory-plan-2026-11-24.xlsx",
      mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      base64: bytes.toString("base64"),
    });
    expect(seen.url).toBe("http://backend.test/api/inventory/workbook?promo_factor=1.5&promo_from=2026-12-01&promo_to=2026-12-07");
    expect(seen.auth).toBe(`Bearer ${KEY_A}`);
    expect(seen.url).not.toMatch(/tenant/i);
    const plain: { url?: string } = {};
    await downloadWorkbook({ fetchImpl: stub(200, plain), apiBase: "http://backend.test" }, { apiKey: KEY_A });
    expect(plain.url).toBe("http://backend.test/api/inventory/workbook");
  });

  it("meets each refusal of the backend with its own message and never sends a bad request", async () => {
    const call = (status: number) => downloadWorkbook({ fetchImpl: stub(status, {}), apiBase: "http://b.test" }, { apiKey: KEY_A });
    expect(await call(401)).toMatchObject({ ok: false, code: "unauthorized" });
    expect(await call(403)).toMatchObject({ ok: false, code: "forbidden" });
    expect(await call(404)).toMatchObject({ ok: false, code: "no_data" });
    expect(await call(422)).toMatchObject({ ok: false, code: "invalid_input" });
    expect(await call(500)).toMatchObject({ ok: false, code: "failed" });
    const down = (async () => {
      throw new Error("ECONNREFUSED http://secret-host");
    }) as typeof fetch;
    const r = await downloadWorkbook({ fetchImpl: down, apiBase: "http://b.test" }, { apiKey: KEY_A });
    expect(r).toMatchObject({ ok: false, code: "failed" });
    expect(JSON.stringify(r)).not.toContain("secret-host");
    let sent = false;
    const spy = (async () => {
      sent = true;
      return new Response("x");
    }) as typeof fetch;
    expect(await downloadWorkbook({ fetchImpl: spy }, { apiKey: KEY_A, promoFactor: 0 })).toMatchObject({ ok: false, code: "invalid_input" });
    expect(sent).toBe(false);
  });
});

run("purchase orders against Postgres, as the request role", () => {
  let db: Db;
  let admin: pg.Client;
  const deps = (today = "2026-11-24") => ({ db, today: () => today });

  beforeAll(async () => {
    db = createDb(pgDialect(adminDsn, 4));
    admin = new pg.Client({ connectionString: adminDsn });
    await admin.connect();
  });

  afterAll(async () => {
    await admin.end();
    await db.destroy();
  });

  it("the buttons drawn are the database's transition table less the two moves only a receipt makes", async () => {
    const rows = (await admin.query("select from_status, to_status from inventory_po_transition")).rows as { from_status: string; to_status: string }[];
    expect(rows).toHaveLength(15);
    for (const status of ORDER_STATUSES) {
      const legal = rows.filter((r) => r.from_status === status && !RECEIPT_ONLY.includes(r.to_status)).map((r) => r.to_status);
      expect([...NEXT_STATUSES[status]].sort(), status).toEqual(legal.sort());
    }
    expect([...OPEN_STATUSES].sort()).toEqual(["confirmed", "ordered", "partly_received", "shipped"]);
  });

  it("makes a draft from the plan, places it with the date the lead time gives, receives it in two parts and finishes it", async () => {
    const made = await createOrder(deps(), { apiKey: KEY_A_OPERATOR, supplier: "Acme Goods" });
    expect(made.ok).toBe(true);
    if (!made.ok) return;
    const draft = made.orders.find((o) => o.supplier === "Acme Goods" && o.status === "draft") as OrderView;
    expect(draft).toMatchObject({ currency: "USD", planAsOf: "2026-11-24", orderedOn: null, expectedOn: null, openUnits: 60, total: "$480.00", linesWithoutCost: 0, late: false, canReceive: false });
    expect(draft.lines.map((l) => [l.sku, l.description, l.ordered, l.received, l.unitCost])).toEqual([["TEE-1", "Studio Tee", 60, 0, "$8.00"]]);
    expect(draft.nextStatuses).toEqual(["ordered", "cancelled"]);

    const openNow = await loadOrders(deps(), { apiKey: KEY_A, status: "open" });
    expect(openNow.ok && openNow.orders.some((o) => o.id === draft.id)).toBe(false); // a draft is not placed, so it is not an open order

    const again = await createOrder(deps(), { apiKey: KEY_A_OPERATOR, supplier: "Acme Goods" });
    expect(again).toMatchObject({ ok: false, code: "conflict" });
    expect(again.ok === false && again.message).toContain("already open");

    const placed = await moveOrder(deps(), { apiKey: KEY_A_OPERATOR, orderId: draft.id, to: "ordered", on: "2026-11-24" });
    const order = (placed.ok ? placed.orders.find((o) => o.id === draft.id) : undefined) as OrderView;
    expect(order).toMatchObject({ status: "ordered", orderedOn: "2026-11-24", expectedOn: "2026-12-03", leadTimeDays: 9, canReceive: true });
    expect(order.nextStatuses).toEqual(["confirmed", "shipped", "cancelled"]);

    const late = await loadOrders(deps("2026-12-04"), { apiKey: KEY_A, status: "open" });
    expect(late.ok && late.orders.find((o) => o.id === draft.id)?.late).toBe(true);
    const notYet = await loadOrders(deps("2026-12-03"), { apiKey: KEY_A, status: "open" });
    expect(notYet.ok && notYet.orders.find((o) => o.id === draft.id)?.late).toBe(false);

    const moved = await setExpectedDate(deps(), { apiKey: KEY_A_OPERATOR, orderId: draft.id, expectedOn: "2026-12-10" });
    expect(moved.ok && moved.orders.find((o) => o.id === draft.id)?.expectedOn).toBe("2026-12-10");

    const lineId = order.lines[0]?.id as number;
    const part = await receiveOrder(deps(), { apiKey: KEY_A_OPERATOR, orderId: draft.id, lines: [{ lineId, quantity: 25 }] });
    expect(part.ok && part.orders.find((o) => o.id === draft.id)).toMatchObject({ status: "partly_received", openUnits: 35, nextStatuses: ["cancelled"] });

    const over = await receiveOrder(deps(), { apiKey: KEY_A_OPERATOR, orderId: draft.id, lines: [{ lineId, quantity: 36 }] });
    expect(over).toMatchObject({ ok: false, code: "conflict" });
    expect(over.ok === false && over.message).toContain("more than was ordered");
    const unchanged = await loadOrders(deps(), { apiKey: KEY_A, status: "partly_received" });
    expect(unchanged.ok && unchanged.orders.find((o) => o.id === draft.id)?.lines[0]?.received).toBe(25);

    const done = await receiveOrder(deps(), { apiKey: KEY_A_OPERATOR, orderId: draft.id, lines: [{ lineId, quantity: 38 }], allowOver: true });
    const finished = (done.ok ? done.orders.find((o) => o.id === draft.id) : undefined) as OrderView;
    expect(finished).toMatchObject({ status: "received", openUnits: 0, canReceive: false, nextStatuses: [], closedOn: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/) });
    expect(finished.lines[0]).toMatchObject({ received: 63, open: 0, overReceived: true });
    expect(await receiveOrder(deps(), { apiKey: KEY_A_OPERATOR, orderId: draft.id, lines: [{ lineId, quantity: 1 }], allowOver: true })).toMatchObject({ ok: false, code: "conflict" });
  });

  it("refuses an illegal move and a move a person cannot make, and cancelling frees the supplier for a new draft", async () => {
    const made = await createOrder(deps(), { apiKey: KEY_A_OPERATOR, supplier: "Borealis Textiles" });
    expect(made.ok).toBe(true);
    if (!made.ok) return;
    const draft = made.orders.find((o) => o.supplier === "Borealis Textiles" && o.status === "draft") as OrderView;
    expect(draft.total).toBeNull();                                                      // no unit cost on its line: no total is invented
    expect(draft.linesWithoutCost).toBe(1);
    for (const to of ["shipped", "confirmed"] as const) {
      const r = await moveOrder(deps(), { apiKey: KEY_A_OPERATOR, orderId: draft.id, to });
      expect(r, to).toMatchObject({ ok: false, code: "conflict" });                       // a draft is placed first
    }
    expect(await moveOrder(deps(), { apiKey: KEY_A_OPERATOR, orderId: draft.id, to: "received" })).toMatchObject({ ok: false, code: "invalid_input" });
    const cancelled = await moveOrder(deps(), { apiKey: KEY_A_OPERATOR, orderId: draft.id, to: "cancelled" });
    expect(cancelled.ok && cancelled.orders.find((o) => o.id === draft.id)).toMatchObject({ status: "cancelled", nextStatuses: [] });
    expect(await moveOrder(deps(), { apiKey: KEY_A_OPERATOR, orderId: draft.id, to: "ordered" })).toMatchObject({ ok: false, code: "conflict" });
    const next = await createOrder(deps(), { apiKey: KEY_A_OPERATOR, supplier: "Borealis Textiles" });
    expect(next.ok && next.orders.filter((o) => o.supplier === "Borealis Textiles" && o.status === "draft")).toHaveLength(1);
  });

  it("a viewer key reads and is refused every change with its reason, a revoked key is unauthorised, and a supplier with nothing to order says so", async () => {
    expect((await loadOrders(deps(), { apiKey: KEY_A })).ok).toBe(true);
    const id = "22222222-2222-4222-8222-222222222222";
    for (const r of [
      await createOrder(deps(), { apiKey: KEY_A, supplier: "Acme Goods" }),
      await moveOrder(deps(), { apiKey: KEY_A, orderId: id, to: "ordered" }),
      await setExpectedDate(deps(), { apiKey: KEY_A, orderId: id, expectedOn: "2026-12-01" }),
      await receiveOrder(deps(), { apiKey: KEY_A, orderId: id, lines: [{ lineId: 1, quantity: 1 }] }),
    ]) {
      expect(r).toMatchObject({ ok: false, code: "forbidden", message: "A viewer key cannot change a purchase order." });
    }
    expect(await loadOrders(deps(), { apiKey: KEY_A_REVOKED })).toMatchObject({ ok: false, code: "unauthorized" });
    expect(await createOrder(deps(), { apiKey: KEY_A_OPERATOR, supplier: "Nobody Ltd" })).toMatchObject({ ok: false, code: "not_found" });
    expect(await createOrder(deps(), { apiKey: KEY_B, supplier: "Acme Goods" })).toMatchObject({ ok: false, code: "not_found" });     // tenant B has no such supplier
  });

  it("another tenant's order cannot be read, moved or received, and answers as an order that is not there", async () => {
    const ordered = await createOrder(deps(), { apiKey: KEY_A_OPERATOR, supplier: "Acme Goods" });
    expect(ordered.ok).toBe(true);
    if (!ordered.ok) return;
    const draft = ordered.orders.find((o) => o.supplier === "Acme Goods" && o.status === "draft") as OrderView;
    const theirs = await loadOrders(deps(), { apiKey: KEY_B });
    expect(theirs).toEqual({ ok: true, orders: [] });
    const missing = "33333333-3333-4333-8333-333333333333";
    const a = await moveOrder(deps(), { apiKey: KEY_B, orderId: draft.id, to: "cancelled" });
    const b = await moveOrder(deps(), { apiKey: KEY_B, orderId: missing, to: "cancelled" });
    expect(a).toMatchObject({ ok: false, code: "not_found" });
    expect(a).toEqual(b);
    expect(await receiveOrder(deps(), { apiKey: KEY_B, orderId: draft.id, lines: [{ lineId: draft.lines[0]?.id ?? 1, quantity: 1 }] })).toMatchObject({ ok: false, code: "not_found" });
    expect(await withTenant(db, TENANT_A, async (trx) => (await trx.selectFrom("inventory_purchase_order").select("status").where("id", "=", draft.id).executeTakeFirst())?.status)).toBe("draft");
    await moveOrder(deps(), { apiKey: KEY_A_OPERATOR, orderId: draft.id, to: "cancelled" });
  });

  it("the stored plan carries where the inbound figure came from", async () => {
    const plan = await withTenant(db, TENANT_A, (trx) => readPlan(trx));
    expect(plan.lines.every((l) => l.inboundSource === "none" && l.inboundExpectedOn === null && l.inboundLate === false)).toBe(true);
    await admin.query("update inventory_plan_line set inbound_source = 'orders', inbound_expected_on = '2026-12-01', inbound_late = true, incoming = 60 where sku = 'CAP-R' and tenant_id = $1", [TENANT_A]);
    try {
      const after = await withTenant(db, TENANT_A, (trx) => readPlan(trx));
      expect(after.lines.find((l) => l.sku === "CAP-R")).toMatchObject({ inboundSource: "orders", inboundExpectedOn: "2026-12-01", inboundLate: true, incoming: 60 });
    } finally {
      await admin.query("update inventory_plan_line set inbound_source = 'none', inbound_expected_on = null, inbound_late = false, incoming = 0 where sku = 'CAP-R' and tenant_id = $1", [TENANT_A]);
    }
  });
});
