"use server";

/**
 * The Server Actions behind the Inventory page. Each takes `unknown`, because a Server Action can be POSTed to directly and its
 * argument is whatever the caller sent; Zod (lib/inventory/input.ts) decides what it is. The key in the request names the tenant,
 * and no action accepts a tenant id.
 */
import type { ExportActionResult } from "@/lib/report/action-types";
import { addPromotion, loadPlan, runPoExport, type PlanResult, type PromoResult } from "@/lib/inventory/export";
import { getDb } from "@/lib/server/db";

const NOT_SET_UP = { ok: false, code: "failed", message: "Inventory is not set up on this server." } as const;

export async function loadPlanAction(raw: unknown): Promise<PlanResult> {
  let db;
  try {
    db = await getDb();
  } catch {
    return NOT_SET_UP;
  }
  return loadPlan({ db }, raw);
}

export async function addPromoAction(raw: unknown): Promise<PromoResult> {
  let db;
  try {
    db = await getDb();
  } catch {
    return NOT_SET_UP;
  }
  return addPromotion({ db }, raw);
}

export async function exportPurchaseOrdersAction(raw: unknown): Promise<ExportActionResult> {
  let db;
  try {
    db = await getDb();
  } catch {
    return NOT_SET_UP;
  }
  return runPoExport({ db }, raw);
}
