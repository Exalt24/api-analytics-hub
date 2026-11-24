/**
 * The rows the database tests run against. Written out as literals, and the expected
 * totals in the tests are literals too, worked out by hand from these, so a test does
 * not use the code under test to say what the answer is.
 */
import { createHash } from "node:crypto";
import type pg from "pg";
import { embed, literal, normalise } from "@/lib/server/search";

export const TENANT_A = "aaaaaaaa-0000-4000-8000-00000000000a";
export const TENANT_B = "bbbbbbbb-0000-4000-8000-00000000000b";
export const TENANT_EMPTY = "cccccccc-0000-4000-8000-00000000000c";

export const CONN_A1 = "a1a1a1a1-0000-4000-8000-0000000000a1";
export const CONN_A2 = "a2a2a2a2-0000-4000-8000-0000000000a2";
export const CONN_B1 = "b1b1b1b1-0000-4000-8000-0000000000b1";

export const KEY_A = "aah_test_alpha_viewer_key_0001";
export const KEY_A_REVOKED = "aah_test_alpha_revoked_key_0002";
export const KEY_B = "aah_test_bravo_operator_key_0003";
export const KEY_A_OPERATOR = "aah_test_alpha_operator_key_0004";

export const sha256 = (s: string): string => createHash("sha256").update(s).digest("hex");

type Snap = [conn: string, tenant: string, metric: string, day: string, value: number, currency: string | null];

const SNAPSHOTS: Snap[] = [
  // tenant A, October 2026, connection 1
  [CONN_A1, TENANT_A, "orders", "2026-10-01", 12, null],
  [CONN_A1, TENANT_A, "orders", "2026-10-02", 8, null],
  [CONN_A1, TENANT_A, "orders", "2026-10-03", 15, null],
  [CONN_A1, TENANT_A, "gross_sales", "2026-10-01", 123456, "USD"],
  [CONN_A1, TENANT_A, "gross_sales", "2026-10-02", 98765, "USD"],
  [CONN_A1, TENANT_A, "gross_sales", "2026-10-03", 150000, "USD"],
  [CONN_A1, TENANT_A, "refunds", "2026-10-01", 1000, "USD"],
  [CONN_A1, TENANT_A, "refunds", "2026-10-02", 0, "USD"],
  [CONN_A1, TENANT_A, "refunds", "2026-10-03", 2550, "USD"],
  // a gauge and an average: not exported
  [CONN_A1, TENANT_A, "customers", "2026-10-01", 9, null],
  [CONN_A1, TENANT_A, "avg_order_value", "2026-10-01", 10288, "USD"],
  // a second connection reports orders on the same day: the day folds to 12 + 3
  [CONN_A2, TENANT_A, "orders", "2026-10-01", 3, null],
  // the days either side of the month: not in October
  [CONN_A1, TENANT_A, "orders", "2026-09-30", 99, null],
  [CONN_A1, TENANT_A, "orders", "2026-11-01", 98, null],
  // tenant A, September, so a September export has data and an August one has none
  [CONN_A1, TENANT_A, "orders", "2026-09-15", 21, null],
  // tenant B, October 2026: figures nobody else has
  [CONN_B1, TENANT_B, "orders", "2026-10-01", 777, null],
  [CONN_B1, TENANT_B, "orders", "2026-10-02", 555, null],
  [CONN_B1, TENANT_B, "gross_sales", "2026-10-01", 7770000, "EUR"],
];

export const QUERIES_A = ["red running shoes", "running shoes red"];
export const QUERIES_B = ["red running shoes", "red running shoes for kids", "wedding cake delivery"];

export async function seed(client: pg.Client): Promise<void> {
  for (const [id, slug, name] of [
    [TENANT_A, "alpha", "Alpha Outfitters"],
    [TENANT_B, "bravo", "Bravo Bakery"],
    [TENANT_EMPTY, "empty", "Empty Co"],
  ] as const) {
    await client.query("insert into tenant (id, slug, name) values ($1, $2, $3)", [id, slug, name]);
  }
  for (const [id, tenant, account] of [
    [CONN_A1, TENANT_A, "alpha-one.invalid"],
    [CONN_A2, TENANT_A, "alpha-two.invalid"],
    [CONN_B1, TENANT_B, "bravo-one.invalid"],
  ] as const) {
    await client.query(
      "insert into platform_connection (id, tenant_id, platform, external_account, credentials_enc) values ($1, $2, 'shopify', $3, decode('00', 'hex'))",
      [id, tenant, account],
    );
  }
  for (const [conn, tenant, metric, day, value, currency] of SNAPSHOTS) {
    await client.query(
      "insert into metric_snapshot (tenant_id, connection_id, metric_key, observed_on, value_numeric, currency) values ($1, $2, $3, $4, $5, $6)",
      [tenant, conn, metric, day, value, currency],
    );
  }
  for (const [key, tenant, role, revoked] of [
    [KEY_A, TENANT_A, "viewer", false],
    [KEY_A_REVOKED, TENANT_A, "admin", true],
    [KEY_B, TENANT_B, "operator", false],
    [KEY_A_OPERATOR, TENANT_A, "operator", false],
  ] as const) {
    await client.query(
      "insert into api_key (tenant_id, key_hash, label, role, revoked_at) values ($1, $2, $3, $4, $5)",
      [tenant, sha256(key), `test-${role}`, role, revoked ? new Date("2026-01-01T00:00:00Z") : null],
    );
  }
  for (const [tenant, queries] of [
    [TENANT_A, QUERIES_A],
    [TENANT_B, QUERIES_B],
  ] as const) {
    for (const q of queries) {
      await client.query(
        "insert into query_embedding (tenant_id, query_text, embedding) values ($1, $2, $3::text::vector)",
        [tenant, normalise(q), literal(embed(q))],
      );
    }
  }
  await seedInventory(client);
}

/**
 * The inventory plan the web layer reads, as literals (the Python side works a plan out; the web side only reads and exports it).
 * Tenant A: a plan for 2026-11-24 with a best seller short on cover, a variant out of stock, a variant with no supplier, a slow mover and a
 * rising one, two suppliers, two raised alerts and one promotion. Tenant B: one variant and one plan line of its own.
 */
export const PLAN_DAY = "2026-11-24";
export const V = {
  tee: "d0000001-0000-4000-8000-000000000001",
  mug: "d0000001-0000-4000-8000-000000000002",
  hat: "d0000001-0000-4000-8000-000000000003",
  sock: "d0000001-0000-4000-8000-000000000004",
  cap: "d0000001-0000-4000-8000-000000000005",
  loaf: "d0000002-0000-4000-8000-000000000001",
};
const SUP_ACME = "e0000001-0000-4000-8000-000000000001";
const SUP_BOREALIS = "e0000001-0000-4000-8000-000000000002";

type PlanRow = {
  v: string;
  title: string;
  sku: string | null;
  supplier: [id: string, name: string] | null;
  status: string;
  available: number;
  velocity: number;
  counted: number;
  out: number;
  unitsRecent: number;
  unitsPrior: number;
  revenue: number;
  cover: number | null;
  forecastCover: number | null;
  stockout: string | null;
  lead: number | null;
  safety: number | null;
  rop: number | null;
  needs: boolean;
  qty: number;
  cost: number | null;
  abc: string;
  label: string;
  shortage: string | null;
};

const PLAN_A: PlanRow[] = [
  { v: V.tee, title: "Studio Tee", sku: "TEE-1", supplier: [SUP_ACME, "Acme Goods"], status: "ok", available: 10, velocity: 2, counted: 20, out: 8, unitsRecent: 40, unitsPrior: 10, revenue: 100000, cover: 5, forecastCover: 5, stockout: "2026-11-29", lead: 9, safety: 0, rop: 18, needs: true, qty: 60, cost: 800, abc: "A", label: "best_seller", shortage: "cover_below_lead_time" },
  { v: V.mug, title: "Enamel Mug", sku: "MUG-1", supplier: null, status: "no_supplier", available: 50, velocity: 0.5, counted: 28, out: 0, unitsRecent: 14, unitsPrior: 14, revenue: 21000, cover: 100, forecastCover: null, stockout: null, lead: null, safety: null, rop: null, needs: false, qty: 0, cost: null, abc: "B", label: "steady", shortage: null },
  { v: V.hat, title: "Wool Hat", sku: "HAT-1", supplier: [SUP_ACME, "Acme Goods"], status: "ok", available: 30, velocity: 0, counted: 28, out: 0, unitsRecent: 0, unitsPrior: 1, revenue: 900, cover: null, forecastCover: null, stockout: null, lead: 9, safety: 0, rop: 0, needs: false, qty: 0, cost: 450, abc: "C", label: "slow_mover", shortage: null },
  { v: V.sock, title: "Wool Sock - Large", sku: "SOCK-L", supplier: [SUP_BOREALIS, "Borealis Textiles"], status: "ok", available: 0, velocity: 1.2, counted: 24, out: 4, unitsRecent: 29, unitsPrior: 20, revenue: 43500, cover: 0, forecastCover: 0, stockout: "2026-11-24", lead: 30, safety: 8, rop: 44, needs: true, qty: 120, cost: null, abc: "B", label: "steady", shortage: "out_of_stock" },
  { v: V.cap, title: "Cap - Red", sku: "CAP-R", supplier: [SUP_BOREALIS, "Borealis Textiles"], status: "ok", available: 90, velocity: 1.5, counted: 28, out: 0, unitsRecent: 42, unitsPrior: 14, revenue: 63000, cover: 60, forecastCover: 60, stockout: "2027-01-23", lead: 30, safety: 6, rop: 51, needs: false, qty: 0, cost: 600, abc: "A", label: "rising", shortage: null },
];

async function insertPlan(client: pg.Client, tenant: string, rows: PlanRow[], existingRun?: string): Promise<void> {
  const runId =
    existingRun ??
    ((await client.query("insert into inventory_plan_run (tenant_id, as_of, params, variants) values ($1, $2, '{}'::jsonb, $3) returning id", [tenant, PLAN_DAY, rows.length])).rows[0].id as string);
  for (const r of rows) {
    await client.query(
      `insert into inventory_variant (id, tenant_id, variant_gid, product_title, sku, supplier_id)
       values ($1, $2, $3, $4, $5, $6) on conflict do nothing`,
      [r.v, tenant, `gid://shopify/ProductVariant/${r.v.slice(-4)}${tenant.slice(0, 2)}`, r.title, r.sku, r.supplier?.[0] ?? null],
    );
    await client.query(
      `insert into inventory_plan_line (tenant_id, run_id, variant_id, product_title, variant_title, sku, supplier_id, supplier_name, status, stock_read_on, available, incoming,
         velocity, velocity_sd, counted_days, out_days, unknown_days, window_days, units_recent, units_prior, units_all, trend_ratio, revenue_minor, currency, cover_days,
         forecast_cover_days, stockout_date, promo_factor, lead_time_days, lead_time_sd_days, safety_stock, reorder_point, needs_reorder, suggested_qty, unit_cost_minor,
         abc_class, label, shortage)
       values ($1, $2, $3, $4, 'Default Title', $5, $6, $7, $8, $9, $10, 0, $11, 0, $12, $13, 0, 28, $14, $15, $16, null, $17, 'USD', $18, $19, $20, 1, $21, 0, $22, $23, $24, $25, $26, $27, $28, $29)`,
      [tenant, runId, r.v, r.title, r.sku, r.supplier?.[0] ?? null, r.supplier?.[1] ?? null, r.status, PLAN_DAY, r.available, r.velocity, r.counted, r.out, r.unitsRecent, r.unitsPrior,
        r.unitsRecent + r.unitsPrior, r.revenue, r.cover, r.forecastCover, r.stockout, r.lead, r.safety, r.rop, r.needs, r.qty, r.cost, r.abc, r.label, r.shortage],
    );
  }
}

async function seedInventory(client: pg.Client): Promise<void> {
  for (const [id, tenant, name, lead] of [
    [SUP_ACME, TENANT_A, "Acme Goods", 9],
    [SUP_BOREALIS, TENANT_A, "Borealis Textiles", 30],
  ] as const) {
    await client.query("insert into inventory_supplier (id, tenant_id, name, lead_time_days) values ($1, $2, $3, $4)", [id, tenant, name, lead]);
  }
  // an older run for tenant A: the page must read the latest run and never mix the two
  const [older] = PLAN_A;
  if (older) {
    const run = await client.query("insert into inventory_plan_run (tenant_id, as_of, params, variants) values ($1, '2026-11-17', '{}'::jsonb, 1) returning id", [TENANT_A]);
    await insertPlan(client, TENANT_A, [{ ...older, available: 80, cover: 40, forecastCover: 40, shortage: null, needs: false, qty: 0 }], run.rows[0].id as string);
  }
  await insertPlan(client, TENANT_A, PLAN_A);
  await insertPlan(client, TENANT_B, [
    { v: V.loaf, title: "Bravo Loaf", sku: "LOAF-1", supplier: null, status: "no_supplier", available: 5, velocity: 3, counted: 28, out: 0, unitsRecent: 84, unitsPrior: 80, revenue: 99900, cover: 1.7, forecastCover: null, stockout: null, lead: null, safety: null, rop: null, needs: false, qty: 0, cost: null, abc: "A", label: "best_seller", shortage: null },
  ]);
  await client.query(
    "insert into inventory_alert (tenant_id, variant_id, kind, event_ref, as_of, detail) values ($1, $2, 'cover_below_lead_time', '2026-11-14', $4, '{}'::jsonb), ($1, $3, 'out_of_stock', 'start', $4, '{}'::jsonb)",
    [TENANT_A, V.tee, V.sock, PLAN_DAY],
  );
  await client.query("insert into inventory_promo (tenant_id, variant_id, starts_on, ends_on, factor, entered_by) values ($1, $2, '2026-12-01', '2026-12-07', 1.5, 'fixture')", [TENANT_A, V.tee]);
}
