import { describe, expect, it } from "vitest";
import { buildCsv, csvCell } from "@/lib/report/csv";
import { computeTotals, foldDaily, type DailyRow, type ReportData } from "@/lib/report/data";
import { displayAmount, monthBounds, monthName, plainAmount } from "@/lib/report/format";
import { buildPdf, pdfSafe } from "@/lib/report/pdf";
import { buildXlsx } from "@/lib/report/xlsx";
import { readCsv, readPdf, readXlsx } from "./readback";
import { sampleData } from "./report-data";

describe("money in integers", () => {
  it("formats minor units without a float", () => {
    expect(plainAmount(123456n, "minor_currency", "USD")).toBe("1234.56");
    expect(plainAmount(5n, "minor_currency", "USD")).toBe("0.05");
    expect(plainAmount(0n, "minor_currency", "USD")).toBe("0.00");
    expect(plainAmount(-1999n, "minor_currency", "USD")).toBe("-19.99");
    expect(plainAmount(12345n, "minor_currency", "JPY")).toBe("12345"); // no minor unit
    expect(plainAmount(38n, "count", null)).toBe("38");
  });

  it("is exact past 2^53, where a float is not", () => {
    const big = 9007199254740993n; // 2^53 + 1
    expect(plainAmount(big, "minor_currency", "USD")).toBe("90071992547409.93");
    expect(String(Number(big))).not.toBe("9007199254740993"); // the float would have lost the last digit
  });

  it("groups the reader form and prefixes the currency", () => {
    expect(displayAmount(372221n, "minor_currency", "USD")).toBe("$3,722.21");
    expect(displayAmount(123456789n, "minor_currency", "EUR")).toBe("EUR 1,234,567.89");
    expect(displayAmount(-250000n, "minor_currency", "USD")).toBe("-$2,500.00");
    expect(displayAmount(1234567n, "count", null)).toBe("1,234,567");
  });

  it("names months and bounds them, December into January included", () => {
    expect(monthName("2026-10")).toBe("October 2026");
    expect(monthBounds("2026-10")).toEqual({ start: "2026-10-01", end: "2026-11-01" });
    expect(monthBounds("2026-12")).toEqual({ start: "2026-12-01", end: "2027-01-01" });
    expect(() => monthBounds("2026-13")).toThrow();
    expect(() => monthBounds("2026-1")).toThrow();
  });
});

describe("csv cells", () => {
  it("quotes a comma, a double quote and a line break, and doubles the quote", () => {
    expect(csvCell("plain")).toBe("plain");
    expect(csvCell("a,b")).toBe('"a,b"');
    expect(csvCell('say "hi"')).toBe('"say ""hi"""');
    expect(csvCell("two\nlines")).toBe('"two\nlines"');
  });

  it("defuses a text cell a spreadsheet would run as a formula, but not a signed number", () => {
    for (const evil of ["=SUM(A1:A9)", "+1+1", "-2+3", "@cmd", "\tcmd"]) expect(csvCell(evil).replace(/^"/, "").startsWith("'")).toBe(true);
    expect(csvCell("-19.99", true)).toBe("-19.99");
  });

  it("round trips a hostile label through a CSV parser", () => {
    const data = sampleData();
    const row = data.daily[0]!;
    const hostile: ReportData = { ...data, daily: [{ ...row, label: '=HYPERLINK("http://x","y"),"q"' }], totals: data.totals };
    const parsed = readCsv(buildCsv(hostile));
    expect(parsed[1]?.[2]).toBe(`'=HYPERLINK("http://x","y"),"q"`);
  });
});

describe("the three formats hold the same data", () => {
  const data = sampleData();

  it("csv: a header, one line per day and metric, one per total", () => {
    const rows = readCsv(buildCsv(data));
    expect(rows[0]).toEqual(["period", "metric_key", "metric", "value", "currency"]);
    expect(rows.slice(1, 4)).toEqual([
      ["2026-10-01", "gross_sales", "Gross sales", "1234.56", "USD"],
      ["2026-10-01", "orders", "Orders", "15", ""],
      ["2026-10-01", "refunds", "Refunds", "10.00", "USD"],
    ]);
    expect(rows.filter((r) => r[0] === "total")).toEqual([
      ["total", "gross_sales", "Gross sales", "3722.21", "USD"],
      ["total", "orders", "Orders", "38", ""],
      ["total", "refunds", "Refunds", "35.50", "USD"],
    ]);
    expect(rows.length).toBe(1 + data.daily.length + data.totals.length);
  });

  it("xlsx: three sheets, numbers as numbers, money with two decimals shown", async () => {
    const { sheets, formats } = await readXlsx(await buildXlsx(data));
    expect(Object.keys(sheets)).toEqual(["Summary", "Totals", "Daily"]);
    expect(sheets.Totals).toEqual([
      ["Metric key", "Metric", "Total", "Currency", "Days with data"],
      ["gross_sales", "Gross sales", 3722.21, "USD", 3],
      ["orders", "Orders", 38, "", 3],
      ["refunds", "Refunds", 35.5, "USD", 2],
    ]);
    expect(sheets.Daily?.[1]).toEqual(["2026-10-01", "gross_sales", "Gross sales", 1234.56, "USD"]);
    expect(sheets.Daily?.length).toBe(1 + data.daily.length);
    expect(formats.Totals).toContain("#,##0.00");
  });

  it("pdf: the title, every total and every daily line come back as text", async () => {
    const pdf = await readPdf(await buildPdf(data));
    expect(pdf.text).toContain("Alpha Outfitters");
    expect(pdf.text).toContain("October 2026");
    const flat = pdf.lines.join("\n");
    expect(flat).toMatch(/Gross sales\s+\$3,722\.21\s+3 days/);
    expect(flat).toMatch(/Orders\s+38\s+3 days/);
    expect(flat).toMatch(/Refunds\s+\$35\.50\s+2 days/);
    for (const r of data.daily) {
      const value = displayAmount(r.value, r.unit, r.currency);
      expect(flat).toContain(`${r.date} ${r.label}`);
      expect(flat.replace(/ +/g, " ")).toContain(value);
    }
  });

  it("pdf: a long report runs onto more pages and keeps its last row", async () => {
    const rows: DailyRow[] = [];
    for (let d = 1; d <= 28; d += 1) {
      for (const k of ["orders", "payments", "invoices", "work_orders", "ad_leads"]) {
        rows.push({ date: `2026-10-${String(d).padStart(2, "0")}`, metricKey: k, label: k.replace("_", " "), unit: "count", currency: null, value: BigInt(d * 7) });
      }
    }
    const daily = foldDaily(rows);
    const big: ReportData = { tenantName: "Many Rows", month: "2026-10", daily, totals: computeTotals(daily) };
    const pdf = await readPdf(await buildPdf(big));
    expect(pdf.pages).toBeGreaterThan(2);
    expect(pdf.lines.join("\n")).toContain("2026-10-28 work orders");
    expect(pdf.lines.join("\n")).toContain("196"); // 28 * 7
  });

  it("pdf: a tenant name outside Latin-1 does not throw", async () => {
    expect(pdfSafe("Café 日本")).toBe("Café ??");
    const pdf = await readPdf(await buildPdf(sampleData({ tenantName: "日本の店" })));
    expect(pdf.text).toContain("????");
  });

  it("every format carries the summary, its source and the reason for a fallback", async () => {
    const summary = { source: "template" as const, headline: "Stored figures for October 2026", highlights: ["Orders totalled 38 over 3 days with data."], fallbackReason: "no model key is configured" };
    const csv = readCsv(buildCsv(data, summary));
    expect(csv).toContainEqual(["summary_source", "template", "no model key is configured", "", ""]);
    expect(csv).toContainEqual(["summary", "highlight", "Orders totalled 38 over 3 days with data.", "", ""]);
    const { sheets } = await readXlsx(await buildXlsx(data, summary));
    expect(sheets.Summary).toContainEqual(["Summary source", "template: no model key is configured"]);
    const pdf = await readPdf(await buildPdf(data, summary));
    expect(pdf.lines.join("\n")).toContain("AI summary not used: no model key is configured");
  });

  it("every format names the attached source document", async () => {
    const withDoc: ReportData = { ...data, attachment: { filename: "shopify-export.pdf", objectKey: "tenants/t/reports/2026-10/id-shopify-export.pdf" } };
    expect(readCsv(buildCsv(withDoc))).toContainEqual(["source_document", "tenants/t/reports/2026-10/id-shopify-export.pdf", "shopify-export.pdf", "", ""]);
    expect((await readXlsx(await buildXlsx(withDoc))).sheets.Summary).toContainEqual(["Source document", "shopify-export.pdf"]);
    expect((await readPdf(await buildPdf(withDoc))).lines.join("\n")).toContain("shopify-export.pdf");
  });
});
