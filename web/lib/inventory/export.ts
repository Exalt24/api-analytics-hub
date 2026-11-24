/**
 * The three things the Inventory page does, start to finish and with no Next.js in them so the tests can call them directly:
 * read the stored plan, enter a promotion, and write the purchase order drafts as CSV, Excel or a paste for Google Sheets.
 * The Server Actions in app/inventory/actions.ts are thin wrappers. The files reuse the Reports page's own code: the same
 * CSV cell quoting (`csvCell`, which also defuses a formula), the same money text and the same ExcelJS workbook style, and the
 * same result type the download helper already understands.
 */
import ExcelJS from "exceljs";
import { AuthError, resolvePrincipal } from "@/lib/server/auth";
import { withTenant, type Db } from "@/lib/server/db";
import { NoPlanError, readPlan } from "@/lib/inventory/data";
import { PO_EXTENSION, PO_MIME, planRequestSchema, poRequestSchema, promoRequestSchema, type PoFormat } from "@/lib/inventory/input";
import { buildPurchaseOrders, poRows } from "@/lib/inventory/po";
import { toView, type PlanView } from "@/lib/inventory/view";
import { csvCell } from "@/lib/report/csv";
import type { ExportActionResult } from "@/lib/report/action-types";

export type InventoryCode = "invalid_input" | "unauthorized" | "forbidden" | "no_plan" | "failed";
export type InventoryFailure = { ok: false; code: InventoryCode; message: string; fieldErrors?: Record<string, string[]> };
export type PlanResult = { ok: true; plan: PlanView } | InventoryFailure;
export type PromoResult = { ok: true; promoId: string } | InventoryFailure;
export type InventoryDeps = { db: Db };

function fieldErrorsOf(issues: readonly { path: readonly PropertyKey[]; message: string }[]): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const issue of issues) (out[issue.path.map(String).join(".") || "request"] ??= []).push(issue.message);
  return out;
}

function invalid(issues: readonly { path: readonly PropertyKey[]; message: string }[]): InventoryFailure {
  return { ok: false, code: "invalid_input", message: "The request is not valid.", fieldErrors: fieldErrorsOf(issues) };
}

function failureOf(err: unknown): InventoryFailure {
  if (err instanceof AuthError) return { ok: false, code: err.status === 403 ? "forbidden" : "unauthorized", message: err.message };
  if (err instanceof NoPlanError) return { ok: false, code: "no_plan", message: "No inventory plan is stored yet. Run a sync, then the plan." };
  return { ok: false, code: "failed", message: "The request failed. Try again." };
}

/** The latest stored plan for the key's tenant, shaped for the page. */
export async function loadPlan(deps: InventoryDeps, raw: unknown): Promise<PlanResult> {
  const parsed = planRequestSchema.safeParse(raw);
  if (!parsed.success) return invalid(parsed.error.issues);
  try {
    const principal = await resolvePrincipal(deps.db, parsed.data.apiKey);
    const plan = await withTenant(deps.db, principal.tenantId, (trx) => readPlan(trx));
    return { ok: true, plan: toView(plan) };
  } catch (err) {
    return failureOf(err);
  }
}

/** Enter a dated promotion factor. A viewer key may read the plan but not change what it assumes. */
export async function addPromotion(deps: InventoryDeps, raw: unknown): Promise<PromoResult> {
  const parsed = promoRequestSchema.safeParse(raw);
  if (!parsed.success) return invalid(parsed.error.issues);
  const req = parsed.data;
  try {
    const principal = await resolvePrincipal(deps.db, req.apiKey);
    if (principal.role === "viewer") return { ok: false, code: "forbidden", message: "A viewer key cannot enter a promotion." };
    const promoId = await withTenant(deps.db, principal.tenantId, async (trx) => {
      let variantId: string | null = null;
      if (req.sku) {
        const found = await trx.selectFrom("inventory_variant").select("id").where("sku", "=", req.sku).execute();
        if (found.length !== 1 || !found[0]) return null;
        variantId = found[0].id;
      }
      const row = await trx
        .insertInto("inventory_promo")
        .values({
          tenant_id: principal.tenantId,
          variant_id: variantId,
          starts_on: req.startsOn,
          ends_on: req.endsOn,
          factor: req.factor,
          note: req.note ? req.note : null,
          entered_by: `api key ${principal.keyId.slice(0, 8)}`,
        })
        .returning("id")
        .executeTakeFirstOrThrow();
      return row.id;
    });
    if (promoId === null) return { ok: false, code: "invalid_input", message: "No single variant has that SKU.", fieldErrors: { sku: ["No single variant has that SKU."] } };
    return { ok: true, promoId };
  } catch (err) {
    return failureOf(err);
  }
}

const TSV_BAD = /[\t\r\n]/g;

/** A paste for Google Sheets: tab separated, one row a line. A tab or a line break inside a cell becomes a space; a leading = + - @ is defused as in the CSV. */
export function buildSheetsPaste(rows: readonly string[][]): string {
  const cell = (c: string, i: number, numeric: boolean): string => {
    const clean = c.replace(TSV_BAD, " ");
    return !numeric && /^[=+\-@]/.test(clean) ? `'${clean}` : clean;
  };
  return rows.map((r, n) => r.map((c, i) => cell(c, i, n > 0 && (i === 4 || i === 5 || i === 6 || i === 8))).join("\t")).join("\r\n") + "\r\n";
}

export function buildPoCsv(rows: readonly string[][]): string {
  return rows.map((r, n) => r.map((c, i) => csvCell(c, n > 0 && (i === 4 || i === 5 || i === 6 || i === 8))).join(",")).join("\r\n") + "\r\n";
}

export async function buildPoXlsx(rows: readonly string[][]): Promise<Buffer> {
  const wb = new ExcelJS.Workbook();
  wb.creator = "api-analytics-hub";
  const sheet = wb.addWorksheet("Purchase orders");
  const [header, ...body] = rows;
  sheet.columns = (header ?? []).map((h, i) => ({ header: h, key: `c${i}`, width: i === 3 ? 40 : 16 }));
  sheet.getRow(1).font = { bold: true };
  for (const r of body) {
    const row = sheet.addRow(r.map((c) => c));
    for (const i of [4, 5, 6, 8]) {
      const cellRef = row.getCell(i + 1);
      if (cellRef.value !== "" && cellRef.value !== null) cellRef.value = Number(cellRef.value);
    }
    row.getCell(5).numFmt = "#,##0";
    row.getCell(6).numFmt = "#,##0.00";
    row.getCell(7).numFmt = "#,##0.00";
  }
  return Buffer.from(await wb.xlsx.writeBuffer());
}

export function poFilename(asOf: string, format: PoFormat, supplier?: string): string {
  const who = supplier ? `-${supplier.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}` : "";
  return `purchase-orders-${asOf}${who}.${PO_EXTENSION[format]}`;
}

/** The purchase order drafts of the stored plan, as one file: a CSV, a workbook, or a tab separated paste for Sheets. */
export async function runPoExport(deps: InventoryDeps, raw: unknown): Promise<ExportActionResult> {
  const parsed = poRequestSchema.safeParse(raw);
  if (!parsed.success) return { ok: false, code: "invalid_input", message: "The request is not valid.", fieldErrors: fieldErrorsOf(parsed.error.issues) };
  const req = parsed.data;
  try {
    const principal = await resolvePrincipal(deps.db, req.apiKey);
    const plan = await withTenant(deps.db, principal.tenantId, (trx) => readPlan(trx));
    const orders = buildPurchaseOrders(plan.lines, plan.asOf, req.supplier);
    if (orders.length === 0) {
      return { ok: false, code: "no_data", message: req.supplier ? `Nothing to order from ${req.supplier}.` : "Nothing needs ordering from a supplier on this plan." };
    }
    const rows = poRows(orders);
    const bytes = req.format === "csv" ? Buffer.from(buildPoCsv(rows), "utf8") : req.format === "sheets" ? Buffer.from(buildSheetsPaste(rows), "utf8") : await buildPoXlsx(rows);
    return { ok: true, filename: poFilename(plan.asOf, req.format, req.supplier), mimeType: PO_MIME[req.format], base64: bytes.toString("base64") };
  } catch (err) {
    if (err instanceof AuthError) return { ok: false, code: err.status === 403 ? "forbidden" : "unauthorized", message: err.message };
    if (err instanceof NoPlanError) return { ok: false, code: "no_data", message: "No inventory plan is stored yet. Run a sync, then the plan." };
    return { ok: false, code: "failed", message: "The export failed. Try again." };
  }
}
