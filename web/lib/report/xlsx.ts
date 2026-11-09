import ExcelJS from "exceljs";
import type { ReportData } from "@/lib/report/data";
import { currencyExponent, monthName, plainAmount, type Unit } from "@/lib/report/format";
import type { ReportSummary } from "@/lib/report/summary-types";

/** A number cell and its display format. Money goes in as its exact decimal text parsed once, so 12,345.67 stays 12345.67. */
function cellValue(value: bigint, unit: Unit, currency: string | null): { value: number; numFmt: string } {
  const plain = plainAmount(value, unit, currency);
  if (unit === "count") return { value: Number(plain), numFmt: "#,##0" };
  const exponent = currencyExponent(currency ?? "USD");
  return { value: Number(plain), numFmt: exponent === 0 ? "#,##0" : "#,##0.00" };
}

export async function buildXlsx(data: ReportData, summary?: ReportSummary): Promise<Buffer> {
  const wb = new ExcelJS.Workbook();
  wb.creator = "api-analytics-hub";

  const info = wb.addWorksheet("Summary");
  info.columns = [
    { header: "Field", key: "field", width: 22 },
    { header: "Value", key: "value", width: 100 },
  ];
  info.addRow({ field: "Tenant", value: data.tenantName });
  info.addRow({ field: "Month", value: data.month });
  info.addRow({ field: "Month name", value: monthName(data.month) });
  if (summary) {
    info.addRow({ field: "Summary source", value: summary.source === "ai" ? "ai" : `template${summary.fallbackReason ? `: ${summary.fallbackReason}` : ""}` });
    info.addRow({ field: "Headline", value: summary.headline });
    summary.highlights.forEach((h, i) => info.addRow({ field: `Highlight ${i + 1}`, value: h }));
  }
  if (data.attachment) {
    info.addRow({ field: "Source document", value: data.attachment.filename });
    info.addRow({ field: "Source document key", value: data.attachment.objectKey });
  }
  info.getRow(1).font = { bold: true };

  const totals = wb.addWorksheet("Totals");
  totals.columns = [
    { header: "Metric key", key: "metricKey", width: 24 },
    { header: "Metric", key: "label", width: 28 },
    { header: "Total", key: "total", width: 18 },
    { header: "Currency", key: "currency", width: 10 },
    { header: "Days with data", key: "days", width: 16 },
  ];
  totals.getRow(1).font = { bold: true };
  for (const t of data.totals) {
    const row = totals.addRow({ metricKey: t.metricKey, label: t.label, total: 0, currency: t.currency ?? "", days: t.daysWithData });
    const c = cellValue(t.total, t.unit, t.currency);
    row.getCell("total").value = c.value;
    row.getCell("total").numFmt = c.numFmt;
  }

  const daily = wb.addWorksheet("Daily");
  daily.columns = [
    { header: "Date", key: "date", width: 14 },
    { header: "Metric key", key: "metricKey", width: 24 },
    { header: "Metric", key: "label", width: 28 },
    { header: "Value", key: "value", width: 18 },
    { header: "Currency", key: "currency", width: 10 },
  ];
  daily.getRow(1).font = { bold: true };
  for (const r of data.daily) {
    const row = daily.addRow({ date: r.date, metricKey: r.metricKey, label: r.label, value: 0, currency: r.currency ?? "" });
    const c = cellValue(r.value, r.unit, r.currency);
    row.getCell("value").value = c.value;
    row.getCell("value").numFmt = c.numFmt;
  }

  const out = await wb.xlsx.writeBuffer();
  return Buffer.from(out);
}
