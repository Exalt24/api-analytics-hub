// OBSERVE-OK: a Playwright test; every action is followed by an assertion on what the page shows or what the download holds, and each test ends with a screenshot.
/**
 * Drives the built dashboard in Chromium: open Reports, export a month as CSV, Excel and PDF,
 * and read each downloaded file back (parse the CSV, open the workbook, extract the PDF text)
 * to check it holds the stored figures. The expected figures are literals worked out by hand
 * from tests/fixtures.ts, so the test does not ask the code to say what the answer is.
 */
import { readFileSync } from "node:fs";
import { GetObjectCommand } from "@aws-sdk/client-s3";
import { expect, test, type Download, type Page } from "@playwright/test";
import { makeS3Client } from "../lib/s3/presign";
import { KEY_A, KEY_B } from "../tests/fixtures";
import { readCsv, readPdf, readXlsx } from "../tests/readback";

const MODEL = "http://127.0.0.1:4010";

async function openReports(page: Page, key: string | undefined): Promise<void> {
  await page.goto("/");
  await page.getByRole("link", { name: "Reports" }).click();
  await expect(page.getByRole("heading", { name: "Reports" })).toBeVisible();
  if (key !== undefined) {
    await page.getByLabel("API key").fill(key);
    await page.getByRole("button", { name: "Use key" }).click();
    await expect(page.getByText("Signed in with an API key.")).toBeVisible();
  }
  await expect(page.getByRole("button", { name: "Export report" })).toBeEnabled();
}

async function chooseFormat(page: Page, label: "CSV" | "Excel (.xlsx)" | "PDF"): Promise<void> {
  await page.getByRole("combobox", { name: "Format" }).click();
  await page.getByRole("option", { name: label }).click();
  await expect(page.getByRole("combobox", { name: "Format" })).toContainText(label);
}

async function exportAndRead(page: Page): Promise<{ download: Download; bytes: Buffer }> {
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Export report" }).click()]);
  const path = await download.path();
  return { download, bytes: readFileSync(path) };
}

const DAILY_A = [
  ["2026-10-01", "gross_sales", "Gross sales", "1234.56", "USD"],
  ["2026-10-01", "orders", "Orders", "15", ""],
  ["2026-10-01", "refunds", "Refunds", "10.00", "USD"],
  ["2026-10-02", "gross_sales", "Gross sales", "987.65", "USD"],
  ["2026-10-02", "orders", "Orders", "8", ""],
  ["2026-10-02", "refunds", "Refunds", "0.00", "USD"],
  ["2026-10-03", "gross_sales", "Gross sales", "1500.00", "USD"],
  ["2026-10-03", "orders", "Orders", "15", ""],
  ["2026-10-03", "refunds", "Refunds", "25.50", "USD"],
];
const TOTALS_A = [
  ["total", "gross_sales", "Gross sales", "3722.21", "USD"],
  ["total", "orders", "Orders", "38", ""],
  ["total", "refunds", "Refunds", "35.50", "USD"],
];

test.describe("Reports page on the built dashboard", () => {
  test.beforeEach(async ({ request }) => {
    await request.delete(`${MODEL}/__log`);
  });

  test.afterEach(async ({ page }, testInfo) => {
    await page.screenshot({ path: testInfo.outputPath("final.png"), fullPage: true });
  });

  test("the dashboard links to Reports, and an export with no key says to add one", async ({ page }) => {
    await openReports(page, undefined);
    await page.getByRole("button", { name: "Export report" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "Add your key above first." })).toBeVisible();
  });

  test("CSV: the downloaded file holds tenant A's October, row for row", async ({ page }) => {
    await openReports(page, KEY_A);
    await page.getByLabel("Month").fill("2026-10");
    const { download, bytes } = await exportAndRead(page);
    expect(download.suggestedFilename()).toBe("report-2026-10.csv");
    expect(readCsv(bytes)).toEqual([["period", "metric_key", "metric", "value", "currency"], ...DAILY_A, ...TOTALS_A]);
    await expect(page.getByText("Saved report-2026-10.csv")).toBeVisible();
  });

  test("Excel: the downloaded workbook holds the same figures as numbers", async ({ page }) => {
    await openReports(page, KEY_A);
    await page.getByLabel("Month").fill("2026-10");
    await chooseFormat(page, "Excel (.xlsx)");
    const { download, bytes } = await exportAndRead(page);
    expect(download.suggestedFilename()).toBe("report-2026-10.xlsx");
    const { sheets } = await readXlsx(bytes);
    expect(sheets.Totals).toEqual([
      ["Metric key", "Metric", "Total", "Currency", "Days with data"],
      ["gross_sales", "Gross sales", 3722.21, "USD", 3],
      ["orders", "Orders", 38, "", 3],
      ["refunds", "Refunds", 35.5, "USD", 3],
    ]);
    expect(sheets.Daily?.slice(1).map((r) => [String(r[0]), String(r[1]), String(r[3])])).toEqual(
      DAILY_A.map((r) => [r[0] ?? "", r[1] ?? "", String(Number(r[3]))]),
    );
  });

  test("PDF: the text extracted from the downloaded file holds the tenant, totals and every day", async ({ page }) => {
    await openReports(page, KEY_A);
    await page.getByLabel("Month").fill("2026-10");
    await chooseFormat(page, "PDF");
    const { download, bytes } = await exportAndRead(page);
    expect(download.suggestedFilename()).toBe("report-2026-10.pdf");
    const pdf = await readPdf(bytes);
    const flat = pdf.lines.join("\n");
    expect(flat).toContain("Alpha Outfitters");
    expect(flat).toContain("October 2026");
    expect(flat).toMatch(/Gross sales\s+\$3,722\.21\s+3 days/);
    expect(flat).toMatch(/Orders\s+38\s+3 days/);
    expect(flat).toMatch(/Refunds\s+\$35\.50\s+3 days/);
    for (const [date, , label, value] of DAILY_A) {
      expect(flat).toContain(`${date} ${label}`);
      const shown = label === "Orders" ? String(Number(value)) : `$${Number(value).toLocaleString("en-US", { minimumFractionDigits: 2 })}`;
      expect(flat.replace(/ +/g, " ")).toContain(shown);
    }
  });

  test("another tenant's key gets its own figures and none of tenant A's", async ({ page }) => {
    await openReports(page, KEY_B);
    await page.getByLabel("Month").fill("2026-10");
    const { bytes } = await exportAndRead(page);
    const text = bytes.toString("utf8");
    expect(readCsv(bytes)).toContainEqual(["2026-10-01", "orders", "Orders", "777", ""]);
    expect(readCsv(bytes)).toContainEqual(["total", "gross_sales", "Gross sales", "77700.00", "EUR"]);
    for (const leak of ["123456", "1234.56", "Alpha", "USD"]) expect(text).not.toContain(leak);
  });

  test("AI summary on: the model is called with the data and its checked summary is in the file", async ({ page, request }) => {
    await openReports(page, KEY_A);
    await page.getByLabel("Month").fill("2026-10");
    await page.getByRole("switch", { name: "Include AI summary" }).click();
    const { bytes } = await exportAndRead(page);
    const rows = readCsv(bytes);
    expect(rows).toContainEqual(["summary_source", "ai", "", "", ""]);
    expect(rows).toContainEqual(["summary", "highlight", "Orders totalled 38 over 3 days.", "", ""]);
    expect(rows).toContainEqual(["summary", "highlight", "Gross sales totalled 3722.21 over 3 days.", "", ""]);
    await expect(page.getByText("Summary written by the model. Every figure matched the data.")).toBeVisible();
    const log = (await (await request.get(`${MODEL}/__log`)).json()) as Array<{ month: string; prompt: string }>;
    expect(log.length).toBe(1);
    expect(log[0]?.prompt).toContain("metricKey orders | Orders | total 38 | days with data 3");
    expect(log[0]?.prompt).not.toContain("Alpha"); // the tenant name is not sent to the model
  });

  test("AI summary on, and a model that invents a figure twice: the file says it fell back to the template", async ({ page, request }) => {
    await openReports(page, KEY_A);
    await page.getByLabel("Month").fill("2026-09");
    await page.getByRole("switch", { name: "Include AI summary" }).click();
    await chooseFormat(page, "PDF");
    const { bytes } = await exportAndRead(page);
    const pdf = await readPdf(bytes);
    const flat = pdf.lines.join("\n");
    expect(flat).toContain("Source: plain template");
    expect(flat).toContain("AI summary not used: the model's summary cited figures that are not in the data; tried 2 times");
    expect(flat).not.toContain("40%");
    expect(flat).not.toContain("61");
    expect(flat).toMatch(/Orders totalled 120 over 2 days with data\./);
    await expect(page.getByText(/Summary from a plain template\./)).toBeVisible();
    const log = (await (await request.get(`${MODEL}/__log`)).json()) as unknown[];
    expect(log.length).toBe(2); // the first try and the one retry, no more
  });

  test("an unknown key, and a month with no data, each show an error and no download", async ({ page }) => {
    await openReports(page, "aah_not_a_real_key_000000");
    await page.getByLabel("Month").fill("2026-10");
    await page.getByRole("button", { name: "Export report" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "invalid API key" })).toBeVisible();

    await page.getByRole("button", { name: "sign out" }).click();
    await page.getByLabel("API key").fill(KEY_A);
    await page.getByRole("button", { name: "Use key" }).click();
    await page.getByLabel("Month").fill("2025-01");
    await page.getByRole("button", { name: "Export report" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "No stored data for 2025-01" })).toBeVisible();
  });

  test("the export button shows a working state until the server answers", async ({ page }) => {
    await openReports(page, KEY_A);
    await page.getByLabel("Month").fill("2026-10");
    // hold the Server Action request until the test has looked at the page, then let it through
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route("**/reports", async (route) => {
      if (route.request().method() === "POST") await held;
      await route.continue();
    });
    const downloadDone = page.waitForEvent("download");
    await page.getByRole("button", { name: "Export report" }).click();
    await expect(page.getByRole("button", { name: "Exporting" })).toBeDisabled();
    await expect(page.getByRole("combobox", { name: "Format" })).toBeDisabled();
    release();
    await downloadDone;
    await expect(page.getByRole("button", { name: "Export report" })).toBeEnabled();
  });

  test("a document attached through a presigned URL is stored in S3 and named in the export", async ({ page }) => {
    test.skip(!process.env.E2E_S3_UP, "the S3 stand-in is not running (docker compose --profile s3 up -d --wait s3)");
    await openReports(page, KEY_A);
    await page.getByLabel("Month").fill("2026-10");
    const content = Buffer.from("date,amount\n2026-10-01,1234.56\n", "utf8");
    await page.getByLabel("Source document").setInputFiles({ name: "bank statement.csv", mimeType: "text/csv", buffer: content });
    await expect(page.getByText("Attached bank statement.csv.")).toBeVisible();
    const { bytes } = await exportAndRead(page);
    const row = readCsv(bytes).find((r) => r[0] === "source_document");
    expect(row?.[2]).toBe("bank statement.csv");
    const key = row?.[1] ?? "";
    expect(key).toMatch(/^tenants\/aaaaaaaa-0000-4000-8000-00000000000a\/reports\/2026-10\/[0-9a-f-]{36}-bank_statement\.csv$/);
    const client = makeS3Client({
      bucket: "report-documents",
      region: "us-east-1",
      endpoint: process.env.S3_TEST_ENDPOINT ?? "http://127.0.0.1:8333",
      accessKeyId: "webtestaccesskey",
      secretAccessKey: "webtest-secret-key-not-real",
    });
    const stored = await client.send(new GetObjectCommand({ Bucket: "report-documents", Key: key }));
    expect(Buffer.from(await stored.Body!.transformToByteArray()).equals(content)).toBe(true);
  });
});
