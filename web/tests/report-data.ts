/** A small hand-built ReportData for the tests that need no database. Figures are literals. */
import { computeTotals, foldDaily, type DailyRow, type ReportData } from "@/lib/report/data";

export function sampleData(overrides: Partial<ReportData> = {}): ReportData {
  const rows: DailyRow[] = [
    { date: "2026-10-01", metricKey: "orders", label: "Orders", unit: "count", currency: null, value: 15n },
    { date: "2026-10-02", metricKey: "orders", label: "Orders", unit: "count", currency: null, value: 8n },
    { date: "2026-10-03", metricKey: "orders", label: "Orders", unit: "count", currency: null, value: 15n },
    { date: "2026-10-01", metricKey: "gross_sales", label: "Gross sales", unit: "minor_currency", currency: "USD", value: 123456n },
    { date: "2026-10-02", metricKey: "gross_sales", label: "Gross sales", unit: "minor_currency", currency: "USD", value: 98765n },
    { date: "2026-10-03", metricKey: "gross_sales", label: "Gross sales", unit: "minor_currency", currency: "USD", value: 150000n },
    { date: "2026-10-01", metricKey: "refunds", label: "Refunds", unit: "minor_currency", currency: "USD", value: 1000n },
    { date: "2026-10-03", metricKey: "refunds", label: "Refunds", unit: "minor_currency", currency: "USD", value: 2550n },
  ];
  const daily = foldDaily(rows);
  return { tenantName: "Alpha Outfitters", month: "2026-10", daily, totals: computeTotals(daily), ...overrides };
}

/** Totals of sampleData, worked out by hand: orders 38 over 3 days, gross sales 372221 minor (3,722.21 USD) over 3, refunds 3550 (35.50) over 2. */
export const SAMPLE_TOTALS = { orders: "38", gross: "3722.21", refunds: "35.5" };
