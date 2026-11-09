import type { ReportData } from "@/lib/report/data";
import { plainAmount } from "@/lib/report/format";
import type { ReportSummary } from "@/lib/report/summary-types";

export const CSV_HEADER = ["period", "metric_key", "metric", "value", "currency"] as const;

const Q = String.fromCharCode(34);

// Quote a cell holding a comma, a double quote or a line break. Text starting with = + - or @
// gets a leading apostrophe so a spreadsheet does not run it as a formula. `numeric` cells are
// numbers formatted here, where a leading minus is a sign.
export function csvCell(value: string, numeric = false): string {
  let cell = value;
  if (!numeric && /^[=+\-@\t\r]/.test(cell)) cell = "'" + cell;
  if (!/[,\r\n\x22]/.test(cell)) return cell;
  return Q + cell.replace(/\x22/g, Q + Q) + Q;
}

function row(cells: string[], numericAt: number[] = []): string {
  return cells.map((c, i) => csvCell(c, numericAt.includes(i))).join(",");
}

// period is a date for a daily row and `total` for a month total. The rows named
// summary, summary_source and source_document carry the prose the PDF shows.
export function buildCsv(data: ReportData, summary?: ReportSummary): string {
  const lines: string[] = [row([...CSV_HEADER])];
  for (const r of data.daily) {
    lines.push(row([r.date, r.metricKey, r.label, plainAmount(r.value, r.unit, r.currency), r.currency ?? ""], [3]));
  }
  for (const t of data.totals) {
    lines.push(row(["total", t.metricKey, t.label, plainAmount(t.total, t.unit, t.currency), t.currency ?? ""], [3]));
  }
  if (summary) {
    lines.push(row(["summary", "headline", summary.headline, "", ""]));
    for (const h of summary.highlights) lines.push(row(["summary", "highlight", h, "", ""]));
    lines.push(row(["summary_source", summary.source, summary.fallbackReason ?? "", "", ""]));
  }
  if (data.attachment) {
    lines.push(row(["source_document", data.attachment.objectKey, data.attachment.filename, "", ""]));
  }
  return lines.join("\r\n") + "\r\n";
}
