/**
 * Purchase order drafts: the variants that need an order, grouped by supplier, one draft for each supplier.
 * Pure (no database), so the rules are tested on their own. A draft is a proposal for a person to approve and
 * send; nothing here contacts a supplier. Money is an integer in minor units with its currency beside it, as
 * everywhere in the hub, and a line with no unit cost is written with a blank cost, never a zero.
 */
import { displayName, type PlanLine } from "@/lib/inventory/data";
import { plainAmount } from "@/lib/report/format";

export type PoLine = {
  sku: string;
  description: string;
  quantity: number;
  unitCostMinor: bigint | null;
  lineTotalMinor: bigint | null;
  currency: string | null;
  coverDays: number | null;
  label: PlanLine["label"];
};

export type PurchaseOrder = {
  supplier: string;
  leadTimeDays: number;
  /** The plan's date plus the supplier's lead time: when an order placed on the plan's date is expected. */
  expectedOn: string;
  lines: PoLine[];
  /** Null when no line has a cost; otherwise the sum of the lines that do (lines without a cost are listed, not guessed). */
  totalMinor: bigint | null;
  currency: string | null;
  linesWithoutCost: number;
};

function addDays(day: string, days: number): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/** The lines an order is due for: tracked, with a supplier, at or under the reorder point, with a quantity. */
export function orderable(lines: readonly PlanLine[]): PlanLine[] {
  return lines.filter((l) => l.status === "ok" && l.needsReorder && l.suggestedQty > 0 && l.supplier !== null && l.leadTimeDays !== null);
}

export function buildPurchaseOrders(lines: readonly PlanLine[], asOf: string, onlySupplier?: string): PurchaseOrder[] {
  const bySupplier = new Map<string, PlanLine[]>();
  for (const l of orderable(lines)) {
    const name = l.supplier as string;
    if (onlySupplier !== undefined && name !== onlySupplier) continue;
    const group = bySupplier.get(name);
    if (group) group.push(l);
    else bySupplier.set(name, [l]);
  }
  const orders: PurchaseOrder[] = [];
  for (const [supplier, group] of [...bySupplier.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    const poLines: PoLine[] = group
      .slice()
      .sort((a, b) => (a.coverDays ?? Infinity) - (b.coverDays ?? Infinity) || displayName(a).localeCompare(displayName(b)) || a.variantId.localeCompare(b.variantId))
      .map((l) => ({
        sku: l.sku ?? "",
        description: displayName(l),
        quantity: l.suggestedQty,
        unitCostMinor: l.unitCostMinor,
        lineTotalMinor: l.unitCostMinor === null ? null : l.unitCostMinor * BigInt(l.suggestedQty),
        currency: l.currency,
        coverDays: l.coverDays,
        label: l.label,
      }));
    const costed = poLines.filter((p) => p.lineTotalMinor !== null);
    const lead = group[0]?.leadTimeDays ?? 0;
    orders.push({
      supplier,
      leadTimeDays: lead,
      expectedOn: addDays(asOf, lead),
      lines: poLines,
      totalMinor: costed.length === 0 ? null : costed.reduce((sum, p) => sum + (p.lineTotalMinor ?? 0n), 0n),
      currency: poLines.find((p) => p.currency !== null)?.currency ?? null,
      linesWithoutCost: poLines.length - costed.length,
    });
  }
  return orders;
}

export const PO_HEADER = ["supplier", "expected_on", "sku", "description", "quantity", "unit_cost", "line_total", "currency", "days_of_cover", "class"] as const;

/** The draft as rows of text cells, a header first: one row for each order line, in supplier order. The same rows feed the CSV, the workbook and the paste for Sheets. */
export function poRows(orders: readonly PurchaseOrder[]): string[][] {
  const rows: string[][] = [[...PO_HEADER]];
  for (const o of orders) {
    for (const l of o.lines) {
      const cur = l.currency ?? o.currency;
      rows.push([
        o.supplier,
        o.expectedOn,
        l.sku,
        l.description,
        String(l.quantity),
        l.unitCostMinor === null ? "" : plainAmount(l.unitCostMinor, "minor_currency", cur),
        l.lineTotalMinor === null ? "" : plainAmount(l.lineTotalMinor, "minor_currency", cur),
        cur ?? "",
        l.coverDays === null ? "" : l.coverDays.toFixed(1),
        l.label,
      ]);
    }
  }
  return rows;
}
