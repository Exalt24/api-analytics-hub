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
}
