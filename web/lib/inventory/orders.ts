/**
 * Purchase orders on the Inventory page, start to finish and with no Next.js in them so the tests can call them directly: list the orders, make a draft from the plan's supplier
 * draft, place, confirm, ship or cancel one, change its expected date, record a receipt, and download the plan workbook.
 *
 * THE RULES ARE NOT HERE. A move that the table does not allow, a receipt over what was ordered, a draft with nothing due: the database refuses each of them (migrations/021, four SQL
 * functions and two triggers), and this file passes the refusal's own text on. The same functions are what app/inventory/order_store.py calls, so the page, the API and the command
 * line are held to one set of rules. `NEXT_STATUSES` below is only which buttons to draw, and tests/inventory-orders.test.ts checks it against the database's transition table.
 *
 * AUTH. The API key names the tenant and every query runs inside `withTenant`. A viewer key may read; it may not change an order. A write never accepts a tenant id.
 *
 * THE WORKBOOK is built by the backend (app/inventory/workbook.py), because its formulas are checked there against the Python plan by recalculating them in LibreOffice. This file asks
 * the backend for it with the user's own key as a bearer token, so the backend's auth and tenant rules apply, and hands the bytes on.
 */
import { sql } from "kysely";
import { AuthError, resolvePrincipal } from "@/lib/server/auth";
import { withTenant, type Db, type TenantTx } from "@/lib/server/db";
import type { PurchaseOrderStatus } from "@/lib/server/schema";
import {
  WORKBOOK_MIME,
  createOrderSchema,
  expectedDateSchema,
  moveOrderSchema,
  orderListSchema,
  receiptSchema,
  workbookRequestSchema,
  type OrderStatus,
} from "@/lib/inventory/input";
import { displayAmount } from "@/lib/report/format";
import type { ExportActionResult } from "@/lib/report/action-types";

export type OrderCode = "invalid_input" | "unauthorized" | "forbidden" | "not_found" | "conflict" | "failed";
export type OrderFailure = { ok: false; code: OrderCode; message: string; fieldErrors?: Record<string, string[]> };

export type OrderLineView = {
  id: number;
  sku: string | null;
  description: string;
  ordered: number;
  received: number;
  open: number;
  overReceived: boolean;
  unitCost: string | null;
};

export type OrderView = {
  id: string;
  supplier: string;
  status: OrderStatus;
  currency: string | null;
  leadTimeDays: number | null;
  planAsOf: string | null;
  orderedOn: string | null;
  expectedOn: string | null;
  confirmedOn: string | null;
  shippedOn: string | null;
  closedOn: string | null;
  note: string | null;
  createdBy: string;
  lines: OrderLineView[];
  openUnits: number;
  total: string | null;
  linesWithoutCost: number;
  late: boolean;
  nextStatuses: OrderStatus[];
  canReceive: boolean;
};

export type OrdersResult = { ok: true; orders: OrderView[] } | OrderFailure;
export type WorkbookDeps = { fetchImpl?: typeof fetch; apiBase?: string };
export type OrdersDeps = WorkbookDeps & { db: Db; today?: () => string };

export const OPEN_STATUSES: readonly OrderStatus[] = ["ordered", "confirmed", "shipped", "partly_received"];

/** The moves a person can make from each status. The database's table allows the same, less the two moves only a receipt makes (checked in tests/inventory-orders.test.ts). */
export const NEXT_STATUSES: Record<OrderStatus, OrderStatus[]> = {
  draft: ["ordered", "cancelled"],
  ordered: ["confirmed", "shipped", "cancelled"],
  confirmed: ["shipped", "cancelled"],
  shipped: [],
  partly_received: ["cancelled"],
  received: [],
  cancelled: [],
};

const ORDER_LIMIT = 200;

function utcToday(): string {
  return new Date().toISOString().slice(0, 10);
}

function fieldErrorsOf(issues: readonly { path: readonly PropertyKey[]; message: string }[]): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const issue of issues) (out[issue.path.map(String).join(".") || "request"] ??= []).push(issue.message);
  return out;
}

function invalid(issues: readonly { path: readonly PropertyKey[]; message: string }[]): OrderFailure {
  return { ok: false, code: "invalid_input", message: issues[0]?.message ?? "The request is not valid.", fieldErrors: fieldErrorsOf(issues) };
}

/** The refusal of a purchase order operation. The database's own message is written for a person (a sku, a quantity, a status), so it is passed on. */
export function failureOf(err: unknown): OrderFailure {
  if (err instanceof AuthError) return { ok: false, code: err.status === 403 ? "forbidden" : "unauthorized", message: err.message };
  const code = typeof err === "object" && err !== null && "code" in err ? String((err as { code: unknown }).code) : "";
  const message = err instanceof Error ? err.message : "";
  if (code === "PO404") return { ok: false, code: "not_found", message };
  if (code === "PO006") return { ok: false, code: "invalid_input", message };
  if (/^PO00[1-5]$/.test(code)) return { ok: false, code: "conflict", message };
  return { ok: false, code: "failed", message: "The request failed. Try again." };
}

const DATE = (col: string, as: string) => sql<string | null>`to_char(${sql.ref(col)}, 'YYYY-MM-DD')`.as(as);

/** The tenant's orders, newest first, each with its lines, what is still to arrive, whether it is late on `today`, and the moves a person can make from its status. */
export async function readOrders(trx: TenantTx, today: string, statuses?: readonly PurchaseOrderStatus[]): Promise<OrderView[]> {
  let q = trx
    .selectFrom("inventory_purchase_order as o")
    .select(["o.id", "o.supplier_name", "o.status", "o.currency", "o.lead_time_days", "o.note", "o.created_by"])
    .select([DATE("o.plan_as_of", "planAsOf"), DATE("o.ordered_on", "orderedOn"), DATE("o.expected_on", "expectedOn"), DATE("o.confirmed_on", "confirmedOn"), DATE("o.shipped_on", "shippedOn"), DATE("o.closed_on", "closedOn")])
    .orderBy("o.created_at", "desc")
    .orderBy("o.id")
    .limit(ORDER_LIMIT);
  if (statuses && statuses.length > 0) q = q.where("o.status", "in", [...statuses]);
  const orders = await q.execute();
  if (orders.length === 0) return [];
  const lines = await trx
    .selectFrom("inventory_purchase_order_line as l")
    .selectAll("l")
    .where(
      "l.order_id",
      "in",
      orders.map((o) => o.id),
    )
    .orderBy("l.sku")
    .orderBy("l.id")
    .execute();
  return orders.map((o) => {
    const own = lines.filter((l) => l.order_id === o.id);
    const currency = o.currency === null ? null : o.currency.trim();
    const lineViews: OrderLineView[] = own.map((l) => ({
      id: Number(l.id),
      sku: l.sku,
      description: l.description,
      ordered: l.quantity_ordered,
      received: l.quantity_received,
      open: Math.max(l.quantity_ordered - l.quantity_received, 0),
      overReceived: l.over_received,
      unitCost: l.unit_cost_minor === null ? null : displayAmount(BigInt(l.unit_cost_minor), "minor_currency", l.currency === null ? currency : l.currency.trim()),
    }));
    const costed = own.filter((l) => l.unit_cost_minor !== null);
    const openUnits = lineViews.reduce((sum, l) => sum + l.open, 0);
    const status = o.status as OrderStatus;
    return {
      id: o.id,
      supplier: o.supplier_name,
      status,
      currency,
      leadTimeDays: o.lead_time_days,
      planAsOf: o.planAsOf,
      orderedOn: o.orderedOn,
      expectedOn: o.expectedOn,
      confirmedOn: o.confirmedOn,
      shippedOn: o.shippedOn,
      closedOn: o.closedOn,
      note: o.note,
      createdBy: o.created_by,
      lines: lineViews,
      openUnits,
      total: costed.length === 0 ? null : displayAmount(costed.reduce((sum, l) => sum + BigInt(l.unit_cost_minor ?? "0") * BigInt(l.quantity_ordered), 0n), "minor_currency", currency),
      linesWithoutCost: own.length - costed.length,
      late: OPEN_STATUSES.includes(status) && o.expectedOn !== null && o.expectedOn < today && openUnits > 0,
      nextStatuses: NEXT_STATUSES[status],
      canReceive: OPEN_STATUSES.includes(status),
    };
  });
}

const OPEN_FILTER: readonly PurchaseOrderStatus[] = ["ordered", "confirmed", "shipped", "partly_received"];

export async function loadOrders(deps: OrdersDeps, raw: unknown): Promise<OrdersResult> {
  const parsed = orderListSchema.safeParse(raw);
  if (!parsed.success) return invalid(parsed.error.issues);
  try {
    const principal = await resolvePrincipal(deps.db, parsed.data.apiKey);
    const wanted = parsed.data.status === undefined ? undefined : parsed.data.status === "open" ? OPEN_FILTER : [parsed.data.status];
    const orders = await withTenant(deps.db, principal.tenantId, (trx) => readOrders(trx, (deps.today ?? utcToday)(), wanted));
    return { ok: true, orders };
  } catch (err) {
    return failureOf(err);
  }
}

/** Run a change for a key that may write, then read the orders back. A viewer key is refused with its reason before anything is sent to the database. */
async function change(deps: OrdersDeps, apiKey: string, run: (trx: TenantTx, by: string) => Promise<void>): Promise<OrdersResult> {
  try {
    const principal = await resolvePrincipal(deps.db, apiKey);
    if (principal.role === "viewer") return { ok: false, code: "forbidden", message: "A viewer key cannot change a purchase order." };
    const by = `api key ${principal.keyId.slice(0, 8)}`;
    const orders = await withTenant(deps.db, principal.tenantId, async (trx) => {
      await run(trx, by);
      return readOrders(trx, (deps.today ?? utcToday)());
    });
    return { ok: true, orders };
  } catch (err) {
    return failureOf(err);
  }
}

/** A draft order for one supplier from the stored plan: the lines, quantities and costs are the plan's. */
export async function createOrder(deps: OrdersDeps, raw: unknown): Promise<OrdersResult> {
  const parsed = createOrderSchema.safeParse(raw);
  if (!parsed.success) return invalid(parsed.error.issues);
  return change(deps, parsed.data.apiKey, async (trx, by) => {
    await sql`select (inventory_po_create_from_plan(${parsed.data.supplier}, ${by})).id`.execute(trx);
  });
}

/** Place, confirm, ship or cancel an order. Placing it sets the expected date from the supplier's lead time unless one is given. */
export async function moveOrder(deps: OrdersDeps, raw: unknown): Promise<OrdersResult> {
  const parsed = moveOrderSchema.safeParse(raw);
  if (!parsed.success) return invalid(parsed.error.issues);
  const req = parsed.data;
  return change(deps, req.apiKey, async (trx, by) => {
    // COPY-OK: SQL, not screen text
    await sql`select (inventory_po_move(${req.orderId}::uuid, ${req.to}, ${by}, ${req.on ?? null}::date, ${req.expectedOn ?? null}::date, ${null}::text)).id`.execute(trx);
  });
}

export async function setExpectedDate(deps: OrdersDeps, raw: unknown): Promise<OrdersResult> {
  const parsed = expectedDateSchema.safeParse(raw);
  if (!parsed.success) return invalid(parsed.error.issues);
  const req = parsed.data;
  return change(deps, req.apiKey, async (trx, by) => {
    await sql`select (inventory_po_set_expected(${req.orderId}::uuid, ${req.expectedOn}::date, ${by})).id`.execute(trx);
  });
}

/** Record units received on one or more lines, all or none (one transaction). More than was ordered is refused unless `allowOver`, and then the line is flagged. */
export async function receiveOrder(deps: OrdersDeps, raw: unknown): Promise<OrdersResult> {
  const parsed = receiptSchema.safeParse(raw);
  if (!parsed.success) return invalid(parsed.error.issues);
  const req = parsed.data;
  return change(deps, req.apiKey, async (trx, by) => {
    for (const l of req.lines) {
      // COPY-OK: SQL, not screen text
      await sql`select (inventory_po_receive(${req.orderId}::uuid, ${l.lineId}, ${l.quantity}, ${req.allowOver}, ${by}, ${req.on ?? null}::date)).id`.execute(trx);
    }
  });
}

// ---------------------------------------------------------------------------------------------------------------- the workbook

export function apiBase(deps: WorkbookDeps): string {
  return (deps.apiBase ?? process.env.INVENTORY_API_BASE ?? process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
}

function filenameOf(header: string | null): string {
  const found = header?.match(/filename="([^"\\/]+\.xlsx)"/);
  return found?.[1] ?? "inventory-plan.xlsx";
}

/** The stored plan as an Excel workbook with live formulas, from the backend. The key travels as a bearer token and nothing names a tenant. */
export async function downloadWorkbook(deps: WorkbookDeps, raw: unknown): Promise<ExportActionResult> {
  const parsed = workbookRequestSchema.safeParse(raw);
  if (!parsed.success) return { ok: false, code: "invalid_input", message: parsed.error.issues[0]?.message ?? "The request is not valid.", fieldErrors: fieldErrorsOf(parsed.error.issues) };
  const req = parsed.data;
  const query = new URLSearchParams();
  if (req.promoFactor !== undefined) query.set("promo_factor", String(req.promoFactor));
  if (req.promoFrom !== undefined) query.set("promo_from", req.promoFrom);
  if (req.promoTo !== undefined) query.set("promo_to", req.promoTo);
  if (req.serviceLevel !== undefined) query.set("service_level", String(req.serviceLevel));
  if (req.reviewDays !== undefined) query.set("review_days", String(req.reviewDays));
  const url = `${apiBase(deps)}/api/inventory/workbook${query.size > 0 ? `?${query}` : ""}`;
  let response: Response;
  try {
    response = await (deps.fetchImpl ?? fetch)(url, { headers: { Authorization: `Bearer ${req.apiKey}` }, cache: "no-store", signal: AbortSignal.timeout(60_000) });
  } catch {
    return { ok: false, code: "failed", message: "The workbook service did not answer. Try again." };
  }
  // COPY-OK: the dashboard already calls it an API key
  if (response.status === 401) return { ok: false, code: "unauthorized", message: "The API key was not accepted." };
  if (response.status === 403) return { ok: false, code: "forbidden", message: "This key cannot read inventory." };
  if (response.status === 404) return { ok: false, code: "no_data", message: "No inventory plan is stored yet. Run a sync, then the plan." };
  if (response.status === 422) return { ok: false, code: "invalid_input", message: "The workbook settings were not accepted." };
  if (!response.ok) return { ok: false, code: "failed", message: "The workbook could not be made. Try again." };
  const bytes = Buffer.from(await response.arrayBuffer());
  return { ok: true, filename: filenameOf(response.headers.get("content-disposition")), mimeType: WORKBOOK_MIME, base64: bytes.toString("base64") };
}
