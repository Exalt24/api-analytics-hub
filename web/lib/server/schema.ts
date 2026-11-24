import type { Generated } from "kysely";

/**
 * The columns this layer reads, written by hand from migrations/001, 003 and 010.
 * Not a copy of the whole schema: a table or column that is not listed here cannot
 * be queried through the typed builder, which keeps the read surface small.
 *
 * bigint columns arrive as strings from node-postgres (a JS number loses precision
 * past 2^53), and the code converts them with BigInt() where it does arithmetic.
 */
export interface TenantTable {
  id: string;
  slug: string;
  name: string;
}

export interface MetricTable {
  key: string;
  label: string;
  unit: "minor_currency" | "count" | "ratio" | "seconds" | "milli_count";
  kind: "gauge" | "delta";
}

export interface MetricSnapshotTable {
  id: Generated<string>;
  tenant_id: string;
  connection_id: string;
  metric_key: string;
  observed_on: string;
  value_numeric: string;
  currency: string | null;
}

export interface ApiKeyTable {
  id: string;
  tenant_id: string;
  key_hash: string;
  role: "viewer" | "operator" | "admin";
  revoked_at: Date | null;
}

export interface QueryEmbeddingTable {
  id: Generated<string>;
  tenant_id: string;
  query_text: string;
  embedding: string;
}

/** migrations/015: the per tenant version counter, the per browser last mutation id, and the saved presets. */
export interface ReplicacheSpaceTable {
  tenant_id: string;
  version: string;
}

export interface ReplicacheClientTable {
  tenant_id: string;
  client_id: string;
  client_group_id: string;
  last_mutation_id: string;
  version: string;
}

export interface ReportPresetTable {
  tenant_id: string;
  id: string;
  name: string;
  format: "csv" | "xlsx" | "pdf";
  include_summary: boolean;
  deleted: boolean;
  version: string;
}

/** migrations/020: the inventory plan a tenant's last run stored, the variant names, the suppliers and promotions a person enters, and the alerts raised. */
export interface InventoryPlanRunTable {
  id: string;
  tenant_id: string;
  as_of: string;
  variants: number;
  created_at: Date;
}

export interface InventoryPlanLineTable {
  id: Generated<string>;
  tenant_id: string;
  run_id: string;
  variant_id: string;
  product_title: string;
  variant_title: string;
  sku: string | null;
  supplier_name: string | null;
  status: "ok" | "no_supplier" | "untracked" | "no_stock_reading";
  stock_read_on: string | null;
  available: string;
  incoming: string;
  velocity: number;
  velocity_sd: number;
  counted_days: number;
  out_days: number;
  unknown_days: number;
  window_days: number;
  units_recent: number;
  units_prior: number;
  trend_ratio: number | null;
  revenue_minor: string;
  currency: string | null;
  cover_days: number | null;
  forecast_cover_days: number | null;
  stockout_date: string | null;
  lead_time_days: number | null;
  safety_stock: number | null;
  reorder_point: number | null;
  needs_reorder: boolean;
  suggested_qty: number;
  unit_cost_minor: string | null;
  abc_class: "A" | "B" | "C";
  label: "best_seller" | "rising" | "slow_mover" | "steady";
  shortage: "out_of_stock" | "cover_below_lead_time" | "stockout_projected" | null;
}

export interface InventoryVariantTable {
  id: Generated<string>;
  tenant_id: string;
  sku: string | null;
  product_title: string;
}

export interface InventoryPromoTable {
  id: Generated<string>;
  tenant_id: string;
  variant_id: string | null;
  starts_on: string;
  ends_on: string;
  factor: number;
  note: string | null;
  entered_by: string;
}

export interface InventoryAlertTable {
  id: Generated<string>;
  tenant_id: string;
  variant_id: string;
  kind: "out_of_stock" | "cover_below_lead_time" | "stockout_projected";
  event_ref: string;
  as_of: string;
  detail: Record<string, unknown>;
  created_at: Date;
}

export interface Database {
  inventory_plan_run: InventoryPlanRunTable;
  inventory_plan_line: InventoryPlanLineTable;
  inventory_variant: InventoryVariantTable;
  inventory_promo: InventoryPromoTable;
  inventory_alert: InventoryAlertTable;
  replicache_space: ReplicacheSpaceTable;
  replicache_client: ReplicacheClientTable;
  report_preset: ReportPresetTable;
  tenant: TenantTable;
  metric: MetricTable;
  metric_snapshot: MetricSnapshotTable;
  api_key: ApiKeyTable;
  query_embedding: QueryEmbeddingTable;
}
