"use server";

/**
 * The Server Actions behind the Inventory page. Each takes `unknown`, because a Server Action can be POSTed to directly and its
 * argument is whatever the caller sent; Zod (lib/inventory/input.ts) decides what it is. The key in the request names the tenant,
 * and no action accepts a tenant id.
 */
import type { ExportActionResult } from "@/lib/report/action-types";
import { addPromotion, loadPlan, runPoExport, type PlanResult, type PromoResult } from "@/lib/inventory/export";
import { createOrder, downloadWorkbook, loadOrders, moveOrder, receiveOrder, setExpectedDate, type OrdersResult } from "@/lib/inventory/orders";
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

const ORDERS_NOT_SET_UP = { ok: false, code: "failed", message: "Purchase orders are not set up on this server." } as const;

export async function loadOrdersAction(raw: unknown): Promise<OrdersResult> {
  let db;
  try {
    db = await getDb();
  } catch {
    return ORDERS_NOT_SET_UP;
  }
  return loadOrders({ db }, raw);
}

export async function createOrderAction(raw: unknown): Promise<OrdersResult> {
  let db;
  try {
    db = await getDb();
  } catch {
    return ORDERS_NOT_SET_UP;
  }
  return createOrder({ db }, raw);
}

export async function moveOrderAction(raw: unknown): Promise<OrdersResult> {
  let db;
  try {
    db = await getDb();
  } catch {
    return ORDERS_NOT_SET_UP;
  }
  return moveOrder({ db }, raw);
}

export async function setExpectedDateAction(raw: unknown): Promise<OrdersResult> {
  let db;
  try {
    db = await getDb();
  } catch {
    return ORDERS_NOT_SET_UP;
  }
  return setExpectedDate({ db }, raw);
}

export async function receiveOrderAction(raw: unknown): Promise<OrdersResult> {
  let db;
  try {
    db = await getDb();
  } catch {
    return ORDERS_NOT_SET_UP;
  }
  return receiveOrder({ db }, raw);
}

/** The plan workbook comes from the backend, which authenticates the key itself, so no database is needed here. */
export async function downloadWorkbookAction(raw: unknown): Promise<ExportActionResult> {
  return downloadWorkbook({}, raw);
}
