/** Words and figures for the Inventory page, kept out of the component so they are tested on their own. */
import type { Label, LineStatus, Shortage } from "@/lib/inventory/data";

export const LABEL_TEXT: Record<Label, string> = {
  best_seller: "Best seller",
  rising: "Rising",
  slow_mover: "Slow mover",
  steady: "Steady",
};

export const SHORTAGE_TEXT: Record<Shortage, string> = {
  out_of_stock: "Out of stock",
  cover_below_lead_time: "Cover under lead time",
  stockout_projected: "Stockout projected",
};

export const STATUS_TEXT: Record<LineStatus, string> = {
  ok: "",
  no_supplier: "No supplier set",
  untracked: "Stock not tracked",
  no_stock_reading: "No stock reading yet",
};

/** Days of cover to one decimal; a variant that does not sell has no cover to state. */
export function coverText(days: number | null): string {
  return days === null ? "no sales" : days.toFixed(1);
}

export function velocityText(perDay: number): string {
  return perDay.toFixed(2);
}

/** The trend against the prior window: a ratio such as 2.0x, or "new" when nothing sold before. */
export function trendText(ratio: number | null, label: Label): string {
  if (ratio === null) return label === "rising" ? "new" : "-";
  return `${ratio.toFixed(1)}x`;
}

export type LabelFilter = Label | "all" | "short" | "reorder";

export const FILTERS: { value: LabelFilter; text: string }[] = [
  { value: "all", text: "Everything" },
  { value: "short", text: "Shortages" },
  { value: "reorder", text: "To reorder" },
  { value: "best_seller", text: "Best sellers" },
  { value: "rising", text: "Rising" },
  { value: "slow_mover", text: "Slow movers" },
  { value: "steady", text: "Steady" },
];

export function matchesFilter(line: { label: Label; shortage: Shortage | null; needsReorder: boolean; suggestedQty: number }, filter: LabelFilter): boolean {
  if (filter === "all") return true;
  if (filter === "short") return line.shortage !== null;
  if (filter === "reorder") return line.needsReorder && line.suggestedQty > 0;
  return line.label === filter;
}

/** What the sales rate rests on, for the table's tooltip: days counted of the window, with the days it could not sell and the days assumed. */
export function basisText(counted: number, window: number, out: number, unknown: number): string {
  return `${counted} of ${window} days counted; ${out} out of stock, ${unknown} assumed on sale`;
}
