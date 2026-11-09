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

export interface Database {
  replicache_space: ReplicacheSpaceTable;
  replicache_client: ReplicacheClientTable;
  report_preset: ReportPresetTable;
  tenant: TenantTable;
  metric: MetricTable;
  metric_snapshot: MetricSnapshotTable;
  api_key: ApiKeyTable;
  query_embedding: QueryEmbeddingTable;
}
