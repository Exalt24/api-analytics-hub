/**
 * What the Inventory page shows: the plan the Python side stored for a tenant (migrations/020, app/inventory),
 * read as plain values. The page never works a plan out and never calls Shopify: velocity, cover, reorder
 * points, classes and shortages were decided when the plan was stored, so the page, the alerts and the purchase
 * order file all read the same numbers.
 */
import { sql } from "kysely";
import type { TenantTx } from "@/lib/server/db";

export type Label = "best_seller" | "rising" | "slow_mover" | "steady";
export type Shortage = "out_of_stock" | "cover_below_lead_time" | "stockout_projected";
export type LineStatus = "ok" | "no_supplier" | "untracked" | "no_stock_reading";

export type PlanLine = {
  variantId: string;
  product: string;
  variant: string;
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
  unitsRecent: number;
  unitsPrior: number;
  trendRatio: number | null;
  revenueMinor: bigint;
  currency: string | null;
  coverDays: number | null;
  forecastCoverDays: number | null;
  stockoutDate: string | null;
  leadTimeDays: number | null;
  safetyStock: number | null;
  reorderPoint: number | null;
  needsReorder: boolean;
  suggestedQty: number;
  unitCostMinor: bigint | null;
  abcClass: "A" | "B" | "C";
  label: Label;
  shortage: Shortage | null;
  /** Where `incoming` came from (the open purchase orders, Shopify, or neither), the earliest date an open order is expected, and whether one is past its date. */
  inboundSource: "orders" | "shopify" | "none";
  inboundExpectedOn: string | null;
  inboundLate: boolean;
};

export type PlanAlert = { kind: Shortage; eventRef: string; asOf: string; sku: string | null; product: string; raisedAt: string };

export type PlanData = {
  tenantName: string;
  asOf: string;
  lines: PlanLine[];
  alerts: PlanAlert[];
};

export class NoPlanError extends Error {
  constructor() {
    super("no stored inventory plan");
    this.name = "NoPlanError";
  }
}

/** The name a person sees for a variant: the product, and the variant when it is not the only one. */
export function displayName(line: Pick<PlanLine, "product" | "variant">): string {
  return line.variant === "" || line.variant === "Default Title" ? line.product : `${line.product} - ${line.variant}`;
}

/**
 * Read the latest stored plan for the tenant `trx` is scoped to. There is no tenant filter in the query: `trx`
 * came from `withTenant`, so row level security returns only that tenant's rows.
 */
export async function readPlan(trx: TenantTx): Promise<PlanData> {
  const tenant = await trx.selectFrom("tenant").select("name").executeTakeFirst();
  if (!tenant) throw new Error("tenant not visible to this transaction");
  const run = await trx
    .selectFrom("inventory_plan_run")
    .select(["id"])
    .select(sql<string>`to_char(as_of, 'YYYY-MM-DD')`.as("asOf"))
    .orderBy("as_of", "desc")
    .limit(1)
    .executeTakeFirst();
  if (!run) throw new NoPlanError();

  const rows = await trx
    .selectFrom("inventory_plan_line as l")
    .selectAll("l")
    .select([
      sql<string | null>`to_char(l.stock_read_on, 'YYYY-MM-DD')`.as("stockReadOn"),
      sql<string | null>`to_char(l.stockout_date, 'YYYY-MM-DD')`.as("stockoutDate"),
      sql<string | null>`to_char(l.inbound_expected_on, 'YYYY-MM-DD')`.as("inboundExpectedOn"),
    ])
    .where("l.run_id", "=", run.id)
    .execute();

  const lines: PlanLine[] = rows.map((r) => ({
    variantId: r.variant_id,
    product: r.product_title,
    variant: r.variant_title,
    sku: r.sku,
    supplier: r.supplier_name,
    status: r.status,
    stockReadOn: r.stockReadOn,
    available: Number(r.available),
    incoming: Number(r.incoming),
    velocity: r.velocity,
    velocitySd: r.velocity_sd,
    countedDays: r.counted_days,
    outDays: r.out_days,
    unknownDays: r.unknown_days,
    windowDays: r.window_days,
    unitsRecent: r.units_recent,
    unitsPrior: r.units_prior,
    trendRatio: r.trend_ratio,
    revenueMinor: BigInt(r.revenue_minor),
    currency: r.currency === null ? null : r.currency.trim(),
    coverDays: r.cover_days,
    forecastCoverDays: r.forecast_cover_days,
    stockoutDate: r.stockoutDate,
    leadTimeDays: r.lead_time_days,
    safetyStock: r.safety_stock,
    reorderPoint: r.reorder_point,
    needsReorder: r.needs_reorder,
    suggestedQty: r.suggested_qty,
    unitCostMinor: r.unit_cost_minor === null ? null : BigInt(r.unit_cost_minor),
    abcClass: r.abc_class,
    label: r.label,
    shortage: r.shortage,
    inboundSource: r.inbound_source,
    inboundExpectedOn: r.inboundExpectedOn,
    inboundLate: r.inbound_late,
  }));
  lines.sort(compareLines);

  const alertRows = await trx
    .selectFrom("inventory_alert as a")
    .innerJoin("inventory_variant as v", "v.id", "a.variant_id")
    .select(["a.kind as kind", "a.event_ref as eventRef", "v.sku as sku", "v.product_title as product"])
    .select([
      sql<string>`to_char(a.as_of, 'YYYY-MM-DD')`.as("asOf"),
      sql<string>`to_char(a.created_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')`.as("raisedAt"),
    ])
    .orderBy("a.id", "desc")
    .limit(50)
    .execute();

  return { tenantName: tenant.name, asOf: run.asOf, lines, alerts: alertRows };
}

const SHORTAGE_RANK: Record<Shortage, number> = { out_of_stock: 0, cover_below_lead_time: 1, stockout_projected: 2 };

/** The order a planner reads the table in: shortages first (most urgent kind first), then what needs ordering, then the least cover, then by name. */
export function compareLines(a: PlanLine, b: PlanLine): number {
  const sa = a.shortage === null ? 9 : SHORTAGE_RANK[a.shortage];
  const sb = b.shortage === null ? 9 : SHORTAGE_RANK[b.shortage];
  if (sa !== sb) return sa - sb;
  if (a.needsReorder !== b.needsReorder) return a.needsReorder ? -1 : 1;
  const ca = a.coverDays ?? Number.POSITIVE_INFINITY;
  const cb = b.coverDays ?? Number.POSITIVE_INFINITY;
  if (ca !== cb) return ca - cb;
  return displayName(a).localeCompare(displayName(b)) || a.variantId.localeCompare(b.variantId);
}

export type PlanSummary = {
  variants: number;
  shortages: number;
  toReorder: number;
  bestSellers: number;
  rising: number;
  slowMovers: number;
};

export function summarise(lines: readonly PlanLine[]): PlanSummary {
  return {
    variants: lines.length,
    shortages: lines.filter((l) => l.shortage !== null).length,
    toReorder: lines.filter((l) => l.needsReorder && l.suggestedQty > 0).length,
    bestSellers: lines.filter((l) => l.label === "best_seller").length,
    rising: lines.filter((l) => l.label === "rising").length,
    slowMovers: lines.filter((l) => l.label === "slow_mover").length,
  };
}
