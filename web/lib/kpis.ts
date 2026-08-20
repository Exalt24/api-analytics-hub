/**
 * Turning the API's long list of points into the handful of numbers a dashboard
 * shows, kept out of the components on purpose.
 *
 * WHY THE AGGREGATION RULE DEPENDS ON THE METRIC. Summing orders across a window
 * is right; summing average order value across a window is nonsense, because an
 * average of averages is not the average. The same trap applies to any
 * point-in-time gauge such as a follower count, where the meaningful figure is
 * the latest value or the delta, never the total. So each metric declares how it
 * combines and the reducer obeys that, rather than every caller remembering.
 */

import type { KpiPoint } from "./api";

export type Aggregation = "sum" | "latest" | "derived_aov" | "daily_average";

export type MetricSpec = {
  key: string;
  label: string;
  aggregation: Aggregation;
  kind: "money" | "count";
};

/**
 * The five the dashboard shows. Order is display order.
 *
 * avg_order_value is DERIVED here rather than summed, even though the connector
 * also stores it per day: gross over orders for the whole window is the correct
 * window figure, and adding up daily averages is the classic wrong answer that
 * still renders a plausible number.
 */
export const METRICS: MetricSpec[] = [
  { key: "orders", label: "Orders", aggregation: "sum", kind: "count" },
  { key: "gross_sales", label: "Gross sales", aggregation: "sum", kind: "money" },
  { key: "refunds", label: "Refunds", aggregation: "sum", kind: "money" },
  { key: "net_sales", label: "Net sales", aggregation: "sum", kind: "money" },
  {
    key: "avg_order_value",
    label: "Avg order value",
    aggregation: "derived_aov",
    kind: "money",
  },
  /**
   * A DISTINCT COUNT IS NOT ADDITIVE, and this one shipped wrong before it was
   * caught by checking the rendered number against the store.
   *
   * The card summed the daily distinct-buyer counts and showed 9. The real number
   * of distinct buyers across the same window is 5, because the same person
   * ordered on more than one day and got counted once per day. Nothing in the
   * response looks wrong: every daily figure is correct, and the total is a
   * plausible number that is simply not the thing the label claims.
   *
   * A window-distinct CANNOT be derived from daily distincts at all. Getting it
   * needs either the identities kept per day, or an aggregate computed over the
   * window at source. Neither is free, so the card shows the honest figure the
   * stored data does support, buyers per day, and the exact daily values stay in
   * the table below it.
   */
  {
    key: "customers",
    label: "Buyers per day",
    aggregation: "daily_average",
    kind: "count",
  },
];

export type Totals = Record<
  string,
  { value: number | null; currency: string | null }
>;

export function totals(points: KpiPoint[]): Totals {
  const byMetric = new Map<string, KpiPoint[]>();
  for (const p of points) {
    const list = byMetric.get(p.metric_key) ?? [];
    list.push(p);
    byMetric.set(p.metric_key, list);
  }

  const out: Totals = {};
  for (const spec of METRICS) {
    const rows = byMetric.get(spec.key);
    if (!rows || rows.length === 0) {
      // null, never zero. A metric the connection is not permitted to read must
      // not render as a real figure of zero, which is indistinguishable from a
      // genuinely quiet week.
      out[spec.key] = { value: null, currency: null };
      continue;
    }
    const currency = rows.find((r) => r.currency)?.currency ?? null;

    if (spec.aggregation === "sum") {
      out[spec.key] = {
        value: rows.reduce((a, r) => a + r.value_numeric, 0),
        currency,
      };
    } else if (spec.aggregation === "daily_average") {
      // Mean over the days that HAVE a value, not over the window length. A day
      // with no data is unknown, not zero, and dividing by the window would drag
      // the average down every time a sync was skipped.
      const total = rows.reduce((a, r) => a + r.value_numeric, 0);
      out[spec.key] = { value: Math.round(total / rows.length), currency: null };
    } else if (spec.aggregation === "latest") {
      const newest = [...rows].sort((a, b) =>
        a.observed_on < b.observed_on ? 1 : -1,
      )[0];
      out[spec.key] = { value: newest.value_numeric, currency };
    } else {
      const gross = (byMetric.get("gross_sales") ?? []).reduce(
        (a, r) => a + r.value_numeric,
        0,
      );
      const orders = (byMetric.get("orders") ?? []).reduce(
        (a, r) => a + r.value_numeric,
        0,
      );
      out[spec.key] = {
        // Integer division, matching the connector: an average of minor units is
        // still minor units, and carrying a fraction of a cent into the UI would
        // disagree with the stored figure for no benefit.
        value: orders > 0 ? Math.floor(gross / orders) : null,
        currency: currency ?? (byMetric.get("gross_sales") ?? [])[0]?.currency ?? null,
      };
    }
  }
  return out;
}

/** One row per day, for the table under the cards. */
export function byDay(points: KpiPoint[]): {
  day: string;
  values: Record<string, KpiPoint>;
}[] {
  const days = new Map<string, Record<string, KpiPoint>>();
  for (const p of points) {
    const row = days.get(p.observed_on) ?? {};
    row[p.metric_key] = p;
    days.set(p.observed_on, row);
  }
  return [...days.entries()]
    .sort((a, b) => (a[0] < b[0] ? 1 : -1))
    .map(([day, values]) => ({ day, values }));
}
