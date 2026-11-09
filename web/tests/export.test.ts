import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import { MockLanguageModelV4 } from "ai/test";
import { runExport, type ExportFile, type ExportFailure } from "@/lib/report/export";
import { exportRequestSchema } from "@/lib/report/input";
import { createDb, pgDialect, type Db } from "@/lib/server/db";
import { KEY_A, KEY_A_REVOKED, KEY_B, TENANT_A, TENANT_B } from "./fixtures";
import { readCsv, readPdf, readXlsx } from "./readback";

const adminDsn = inject("adminDsn");
const run = describe.skipIf(!adminDsn);

function fakeModel(replies: object[]) {
  let call = 0;
  return new MockLanguageModelV4({
    doGenerate: async () => {
      const reply = replies[Math.min(call, replies.length - 1)];
      call += 1;
      return {
        content: [{ type: "text" as const, text: JSON.stringify(reply) }],
        finishReason: { unified: "stop" as const, raw: undefined },
        usage: {
          inputTokens: { total: 1, noCache: 1, cacheRead: undefined, cacheWrite: undefined },
          outputTokens: { total: 1, text: 1, reasoning: undefined },
        },
        warnings: [],
      };
    },
  });
}

const NOW = () => new Date("2026-11-09T00:00:00Z");
const base = { month: "2026-10", includeSummary: false } as const;

function ok(r: ExportFile | ExportFailure): ExportFile {
  if (!r.ok) throw new Error(`expected a file, got ${r.code}: ${r.message}`);
  return r;
}
function fail(r: ExportFile | ExportFailure): ExportFailure {
  if (r.ok) throw new Error("expected a failure, got a file");
  return r;
}

run("exporting a tenant's month through the real database layer", () => {
  let db: Db;
  beforeAll(() => {
    db = createDb(pgDialect(adminDsn, 4));
  });
  afterAll(async () => {
    await db.destroy();
  });
  const deps = (model: ReturnType<typeof fakeModel> | null = null) => ({ db, model, now: NOW });

  it("csv: tenant A's October, read back, equals the fixture rows and the hand-worked totals", async () => {
    const file = ok(await runExport(deps(), { ...base, apiKey: KEY_A, format: "csv" }));
    expect(file.filename).toBe("report-2026-10.csv");
    expect(file.mimeType).toBe("text/csv; charset=utf-8");
    expect(readCsv(file.bytes)).toEqual([
      ["period", "metric_key", "metric", "value", "currency"],
      ["2026-10-01", "gross_sales", "Gross sales", "1234.56", "USD"],
      ["2026-10-01", "orders", "Orders", "15", ""],
      ["2026-10-01", "refunds", "Refunds", "10.00", "USD"],
      ["2026-10-02", "gross_sales", "Gross sales", "987.65", "USD"],
      ["2026-10-02", "orders", "Orders", "8", ""],
      ["2026-10-02", "refunds", "Refunds", "0.00", "USD"],
      ["2026-10-03", "gross_sales", "Gross sales", "1500.00", "USD"],
      ["2026-10-03", "orders", "Orders", "15", ""],
      ["2026-10-03", "refunds", "Refunds", "25.50", "USD"],
      ["total", "gross_sales", "Gross sales", "3722.21", "USD"],
      ["total", "orders", "Orders", "38", ""],
      ["total", "refunds", "Refunds", "35.50", "USD"],
    ]);
  });

  it("xlsx: the workbook holds the same figures as numbers", async () => {
    const file = ok(await runExport(deps(), { ...base, apiKey: KEY_A, format: "xlsx" }));
    expect(file.mimeType).toBe("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
    expect(file.bytes.subarray(0, 2).toString("latin1")).toBe("PK"); // a zip, as an .xlsx is
    const { sheets } = await readXlsx(file.bytes);
    expect(sheets.Totals).toEqual([
      ["Metric key", "Metric", "Total", "Currency", "Days with data"],
      ["gross_sales", "Gross sales", 3722.21, "USD", 3],
      ["orders", "Orders", 38, "", 3],
      ["refunds", "Refunds", 35.5, "USD", 3],
    ]);
    expect(sheets.Daily?.slice(1).map((r) => [r[0], r[1], r[3]])).toEqual([
      ["2026-10-01", "gross_sales", 1234.56],
      ["2026-10-01", "orders", 15],
      ["2026-10-01", "refunds", 10],
      ["2026-10-02", "gross_sales", 987.65],
      ["2026-10-02", "orders", 8],
      ["2026-10-02", "refunds", 0],
      ["2026-10-03", "gross_sales", 1500],
      ["2026-10-03", "orders", 15],
      ["2026-10-03", "refunds", 25.5],
    ]);
  });

  it("pdf: the text extracted from the file has the tenant, the totals and every day", async () => {
    const file = ok(await runExport(deps(), { ...base, apiKey: KEY_A, format: "pdf" }));
    expect(file.bytes.subarray(0, 5).toString("latin1")).toBe("%PDF-");
    const pdf = await readPdf(file.bytes);
    const flat = pdf.lines.join("\n");
    expect(flat).toContain("Alpha Outfitters");
    expect(flat).toMatch(/Gross sales\s+\$3,722\.21\s+3 days/);
    expect(flat).toMatch(/Orders\s+38\s+3 days/);
    expect(flat).toMatch(/Refunds\s+\$35\.50\s+3 days/);
    expect(flat).toContain("2026-10-01 Gross sales $1,234.56");
    expect(flat).toContain("2026-10-03 Refunds $25.50");
  });

  it("never puts the other tenant's figures or name in any format", async () => {
    for (const format of ["csv", "xlsx", "pdf"] as const) {
      const file = ok(await runExport(deps(), { ...base, apiKey: KEY_A, format }));
      const text =
        format === "csv"
          ? file.bytes.toString("utf8")
          : format === "xlsx"
            ? JSON.stringify((await readXlsx(file.bytes)).sheets)
            : (await readPdf(file.bytes)).text;
      for (const leak of ["777", "555", "7770000", "77,700", "EUR", "Bravo"]) expect(text, `${format} ${leak}`).not.toContain(leak);
    }
    const b = ok(await runExport(deps(), { ...base, apiKey: KEY_B, format: "csv" }));
    expect(readCsv(b.bytes)).toContainEqual(["2026-10-01", "orders", "Orders", "777", ""]);
    expect(b.bytes.toString("utf8")).not.toContain("123456");
  });

  it("holds a request that skips the form to the same Zod rules: nothing reaches the database", async () => {
    const bad: Array<[string, unknown, string]> = [
      ["month 13", { ...base, apiKey: KEY_A, format: "csv", month: "2026-13" }, "month"],
      ["month 1", { ...base, apiKey: KEY_A, format: "csv", month: "2026-1" }, "month"],
      ["sql in month", { ...base, apiKey: KEY_A, format: "csv", month: "2026-10'; drop table tenant;--" }, "month"],
      ["unknown format", { ...base, apiKey: KEY_A, format: "docx" }, "format"],
      ["short key", { ...base, apiKey: "x", format: "csv" }, "apiKey"],
      ["summary as a string", { ...base, apiKey: KEY_A, format: "csv", includeSummary: "yes" }, "includeSummary"],
      ["no body", undefined, "request"],
      ["bad attachment key", { ...base, apiKey: KEY_A, format: "csv", attachment: { objectKey: "../../etc/passwd", filename: "x" } }, "attachment.objectKey"],
    ];
    for (const [name, body, field] of bad) {
      const r = fail(await runExport(deps(), body));
      expect(r.code, name).toBe("invalid_input");
      expect(Object.keys(r.fieldErrors ?? {}), name).toContain(field);
    }
  });

  it("refuses a month that has not started, an unknown key and a revoked key", async () => {
    expect(fail(await runExport(deps(), { ...base, apiKey: KEY_A, format: "csv", month: "2026-12" })).code).toBe("invalid_input");
    expect(fail(await runExport(deps(), { ...base, apiKey: "aah_not_a_real_key_000000", format: "csv" })).code).toBe("unauthorized");
    expect(fail(await runExport(deps(), { ...base, apiKey: KEY_A_REVOKED, format: "csv" })).code).toBe("unauthorized");
  });

  it("says there is no data for a month with none, instead of sending an empty file", async () => {
    const r = fail(await runExport(deps(), { ...base, apiKey: KEY_A, format: "csv", month: "2026-08" }));
    expect(r.code).toBe("no_data");
    expect(r.message).toContain("2026-08");
  });

  it("holds each action to its own format", async () => {
    const r = fail(await runExport(deps(), { ...base, apiKey: KEY_A, format: "pdf" }, "csv"));
    expect(r.code).toBe("invalid_input");
    expect(Object.keys(r.fieldErrors ?? {})).toContain("format");
  });

  it("accepts a document under the tenant's own prefix and refuses one under another's", async () => {
    const own = `tenants/${TENANT_A}/reports/2026-10/11111111-1111-4111-8111-111111111111-source.pdf`;
    const file = ok(await runExport(deps(), { ...base, apiKey: KEY_A, format: "csv", attachment: { objectKey: own, filename: "source.pdf" } }));
    expect(readCsv(file.bytes)).toContainEqual(["source_document", own, "source.pdf", "", ""]);
    const theirs = `tenants/${TENANT_B}/reports/2026-10/11111111-1111-4111-8111-111111111111-source.pdf`;
    const r = fail(await runExport(deps(), { ...base, apiKey: KEY_A, format: "csv", attachment: { objectKey: theirs, filename: "source.pdf" } }));
    expect(r.code).toBe("forbidden");
  });

  it("with the summary on and a clean model reply, the summary is in all three formats as AI-written", async () => {
    const reply = {
      headline: "October 2026 orders and sales",
      highlights: [{ metricKey: "orders", text: "Orders totalled 38 over 3 days." }],
    };
    for (const format of ["csv", "xlsx", "pdf"] as const) {
      const file = ok(await runExport(deps(fakeModel([reply])), { ...base, includeSummary: true, apiKey: KEY_A, format }));
      expect(file.summary?.source).toBe("ai");
      const text =
        format === "csv"
          ? JSON.stringify(readCsv(file.bytes))
          : format === "xlsx"
            ? JSON.stringify((await readXlsx(file.bytes)).sheets.Summary)
            : (await readPdf(file.bytes)).text;
      expect(text, format).toContain("Orders totalled 38 over 3 days.");
    }
  });

  it("with the summary on and a model that keeps inventing figures, every format says it used the template", async () => {
    const reply = { headline: "A great month", highlights: [{ metricKey: "orders", text: "Orders rose 31% to 52." }] };
    for (const format of ["csv", "xlsx", "pdf"] as const) {
      const file = ok(await runExport(deps(fakeModel([reply, reply])), { ...base, includeSummary: true, apiKey: KEY_A, format }));
      expect(file.summary?.source).toBe("template");
      const text =
        format === "csv"
          ? JSON.stringify(readCsv(file.bytes))
          : format === "xlsx"
            ? JSON.stringify((await readXlsx(file.bytes)).sheets.Summary)
            : (await readPdf(file.bytes)).text;
      expect(text, format).toContain("cited figures that are not in the data");
      expect(text, format).not.toContain("52");
    }
  });

  it("with no model key the export still works and says why there is no AI summary", async () => {
    const file = ok(await runExport(deps(null), { ...base, includeSummary: true, apiKey: KEY_A, format: "csv" }));
    expect(readCsv(file.bytes)).toContainEqual(["summary_source", "template", "no model key is configured", "", ""]);
  });
});

describe("the request schema", () => {
  it("accepts a well formed request and rejects the tenant id as an input", () => {
    const good = exportRequestSchema.safeParse({ apiKey: KEY_A, format: "xlsx", month: "2026-10", includeSummary: true });
    expect(good.success).toBe(true);
    // a tenant id in the body is ignored: the tenant comes from the key
    const withTenant = exportRequestSchema.safeParse({ apiKey: KEY_A, format: "xlsx", month: "2026-10", includeSummary: true, tenantId: TENANT_B });
    expect(withTenant.success && "tenantId" in withTenant.data).toBe(false);
  });
});
