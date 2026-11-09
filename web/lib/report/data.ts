/**
 * The data one export is built from: a tenant's stored daily snapshots for one month
 * and the totals computed from them. Every number in the CSV, the workbook, the PDF
 * and the summary comes from this object, so the three formats cannot disagree.
 */
import { sql } from "kysely";
import type { TenantTx } from "@/lib/server/db";
import { monthBounds, type Unit } from "@/lib/report/format";

export type DailyRow = {
  date: string;
  metricKey: string;
  label: string;
  unit: Unit;
  currency: string | null;
  value: bigint;
};

export type TotalRow = {
  metricKey: string;
  label: string;
  unit: Unit;
  currency: string | null;
  total: bigint;
  daysWithData: number;
};

export type Attachment = { filename: string; objectKey: string };

export type ReportData = {
  tenantName: string;
  month: string;
  daily: DailyRow[];
  totals: TotalRow[];
  attachment?: Attachment;
};

/** Only these units are exported: a count or an amount of money, both integers. Ratios, seconds and milli counts are not mixed in. */
const EXPORT_UNITS = ["count", "minor_currency"] as const;

export class NoDataError extends Error {
  constructor(month: string) {
    super(`no stored data for ${month}`);
    this.name = "NoDataError";
  }
}

/** Sum rows that share a date, a metric and a currency (two connections can report the same metric the same day). */
export function foldDaily(rows: DailyRow[]): DailyRow[] {
  const byKey = new Map<string, DailyRow>();
  for (const r of rows) {
    const key = `${r.date}|${r.metricKey}|${r.currency ?? ""}`;
    const seen = byKey.get(key);
    if (seen) seen.value += r.value;
    else byKey.set(key, { ...r });
  }
  return [...byKey.values()].sort(
    (a, b) => a.date.localeCompare(b.date) || a.metricKey.localeCompare(b.metricKey) || (a.currency ?? "").localeCompare(b.currency ?? ""),
  );
}

export function computeTotals(daily: DailyRow[]): TotalRow[] {
  const byKey = new Map<string, TotalRow>();
  for (const r of daily) {
    const key = `${r.metricKey}|${r.currency ?? ""}`;
    const seen = byKey.get(key);
    if (seen) {
      seen.total += r.value;
      seen.daysWithData += 1;
    } else {
      byKey.set(key, {
        metricKey: r.metricKey,
        label: r.label,
        unit: r.unit,
        currency: r.currency,
        total: r.value,
        daysWithData: 1,
      });
    }
  }
  return [...byKey.values()].sort(
    (a, b) => a.metricKey.localeCompare(b.metricKey) || (a.currency ?? "").localeCompare(b.currency ?? ""),
  );
}

/**
 * Read one month for the tenant `trx` is scoped to. There is no tenant filter in the
 * query: `trx` came from `withTenant`, so row level security returns only that
 * tenant's rows, and `tenant` returns only that tenant's own row.
 */
export async function readMonthlySnapshot(trx: TenantTx, month: string): Promise<ReportData> {
  const { start, end } = monthBounds(month);
  const tenant = await trx.selectFrom("tenant").select("name").executeTakeFirst();
  if (!tenant) throw new Error("tenant not visible to this transaction");

  const rows = await trx
    .selectFrom("metric_snapshot as s")
    .innerJoin("metric as m", "m.key", "s.metric_key")
    .select(["s.metric_key as metricKey", "m.label as label", "m.unit as unit", "s.currency as currency", "s.value_numeric as value"])
    .select(sql<string>`to_char(s.observed_on, 'YYYY-MM-DD')`.as("date"))
    .where("s.observed_on", ">=", start)
    .where("s.observed_on", "<", end)
    .where("m.kind", "=", "delta")
    .where("m.unit", "in", [...EXPORT_UNITS])
    .execute();

  const daily = foldDaily(
    rows.map((r) => ({
      date: r.date,
      metricKey: r.metricKey,
      label: r.label,
      unit: r.unit === "count" ? ("count" as const) : ("minor_currency" as const),
      currency: r.currency,
      value: BigInt(r.value),
    })),
  );
  if (daily.length === 0) throw new NoDataError(month);
  return { tenantName: tenant.name, month, daily, totals: computeTotals(daily) };
}
