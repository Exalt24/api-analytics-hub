/**
 * The plan as the page receives it: plain JSON values (a Server Action returns only what can be serialised, and money
 * is shown as text built by the same integer arithmetic the exports use), with the purchase order drafts beside it.
 */
import { displayName, summarise, type Label, type LineStatus, type PlanAlert, type PlanData, type PlanSummary, type Shortage } from "@/lib/inventory/data";
import { buildPurchaseOrders } from "@/lib/inventory/po";
import { displayAmount } from "@/lib/report/format";

export type LineView = {
  variantId: string;
  name: string;
  sku: string | null;
  supplier: string | null;
  status: LineStatus;
  stockReadOn: string | null;
  available: number;
  incoming: number;
  velocity: number;
  velocitySd: number;
  countedDays: number;
  outDays: number;
  unknownDays: number;
  windowDays: number;
  trendRatio: number | null;
  revenue: string;
  coverDays: number | null;
  forecastCoverDays: number | null;
  stockoutDate: string | null;
  leadTimeDays: number | null;
  safetyStock: number | null;
  reorderPoint: number | null;
  needsReorder: boolean;
  suggestedQty: number;
  abcClass: "A" | "B" | "C";
  label: Label;
  shortage: Shortage | null;
  inboundSource: "orders" | "shopify" | "none";
  inboundExpectedOn: string | null;
  inboundLate: boolean;
};

export type OrderView = {
  supplier: string;
  leadTimeDays: number;
  expectedOn: string;
  lines: { sku: string; description: string; quantity: number }[];
  total: string | null;
  linesWithoutCost: number;
};

export type PlanView = {
  tenantName: string;
  asOf: string;
  summary: PlanSummary;
  lines: LineView[];
  alerts: PlanAlert[];
  orders: OrderView[];
};

export function toView(plan: PlanData): PlanView {
  const lines: LineView[] = plan.lines.map((l) => ({
    variantId: l.variantId,
    name: displayName(l),
    sku: l.sku,
    supplier: l.supplier,
    status: l.status,
    stockReadOn: l.stockReadOn,
    available: l.available,
    incoming: l.incoming,
    velocity: l.velocity,
    velocitySd: l.velocitySd,
    countedDays: l.countedDays,
    outDays: l.outDays,
    unknownDays: l.unknownDays,
    windowDays: l.windowDays,
    trendRatio: l.trendRatio,
    revenue: displayAmount(l.revenueMinor, "minor_currency", l.currency),
    coverDays: l.coverDays,
    forecastCoverDays: l.forecastCoverDays,
    stockoutDate: l.stockoutDate,
    leadTimeDays: l.leadTimeDays,
    safetyStock: l.safetyStock,
    reorderPoint: l.reorderPoint,
    needsReorder: l.needsReorder,
    suggestedQty: l.suggestedQty,
    abcClass: l.abcClass,
    label: l.label,
    shortage: l.shortage,
    inboundSource: l.inboundSource,
    inboundExpectedOn: l.inboundExpectedOn,
    inboundLate: l.inboundLate,
  }));
  const orders: OrderView[] = buildPurchaseOrders(plan.lines, plan.asOf).map((o) => ({
    supplier: o.supplier,
    leadTimeDays: o.leadTimeDays,
    expectedOn: o.expectedOn,
    lines: o.lines.map((l) => ({ sku: l.sku, description: l.description, quantity: l.quantity })),
    total: o.totalMinor === null ? null : displayAmount(o.totalMinor, "minor_currency", o.currency),
    linesWithoutCost: o.linesWithoutCost,
  }));
  return { tenantName: plan.tenantName, asOf: plan.asOf, summary: summarise(plan.lines), lines, alerts: plan.alerts, orders };
}
