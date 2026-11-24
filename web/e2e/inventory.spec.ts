// OBSERVE-OK: a Playwright test; every action is followed by an assertion on what the page shows or what the download holds, and each test ends with a screenshot.
/**
 * Drives the built dashboard in Chromium: open Inventory, sign in with a key, read the exceptions and the planning table, filter it, download a purchase order draft
 * and read it back, and meet the refusal a viewer key gets when it tries to enter a promotion. The expected figures are literals from tests/fixtures.ts (the stored plan).
 */
import { readFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { KEY_A, KEY_B } from "../tests/fixtures";
import { readCsv } from "../tests/readback";

async function openInventory(page: Page, key: string): Promise<void> {
  await page.goto("/");
  await page.getByRole("link", { name: "Inventory" }).click();
  await expect(page.getByRole("heading", { name: "Inventory", level: 1 })).toBeVisible();
  await page.getByLabel("API key").fill(key);
  await page.getByRole("button", { name: "Use key" }).click();
  await expect(page.getByText("Signed in with an API key.")).toBeVisible();
  await expect(page.getByTestId("plan-date")).toBeVisible();
}

test.describe("Inventory page on the built dashboard", () => {
  test.afterEach(async ({ page }, testInfo) => {
    await page.screenshot({ path: testInfo.outputPath("final.png"), fullPage: true });
  });

  test("shows the stored plan: summary, exceptions with their kind, and the planning table", async ({ page }) => {
    await openInventory(page, KEY_A);
    await expect(page.getByTestId("plan-date")).toContainText("Plan for 2026-11-24, Alpha Outfitters. 2 alerts raised so far.");
    const summary = page.getByRole("region", { name: "Summary" });
    await expect(summary).toContainText("Variants");
    await expect(summary.locator(".card").filter({ hasText: "Shortages" })).toContainText("2");
    await expect(summary.locator(".card").filter({ hasText: "To reorder" })).toContainText("2");

    const exceptions = page.getByRole("region", { name: "Exceptions" });
    await expect(exceptions.getByRole("row")).toHaveCount(3); // the header and two shortages
    await expect(exceptions.getByRole("row", { name: /Wool Sock - Large/ })).toContainText("Out of stock");
    await expect(exceptions.getByRole("row", { name: /Studio Tee/ })).toContainText("Cover under lead time");
    await expect(exceptions.getByRole("row", { name: /Studio Tee/ })).toContainText("9 days");
    await expect(exceptions.getByRole("row", { name: /Studio Tee/ })).toContainText("60");

    const table = page.getByRole("region", { name: "Planning table" });
    const tee = table.getByRole("row", { name: /Studio Tee/ });
    await expect(tee).toContainText("Best seller");
    await expect(tee).toContainText("$1,000.00");
    await expect(tee.getByTitle("20 of 28 days counted; 8 out of stock, 0 assumed on sale")).toHaveText("2.00");
    await expect(table.getByRole("row", { name: /Enamel Mug/ })).toContainText("No supplier set");
    await expect(page.getByText("Bravo Loaf")).toHaveCount(0); // another tenant's variant never appears
  });

  test("the filter narrows the table to best sellers, rising and slow movers", async ({ page }) => {
    await openInventory(page, KEY_A);
    const table = page.getByRole("region", { name: "Planning table" });
    await page.getByLabel("Show").selectOption("rising");
    await expect(table.getByRole("row")).toHaveCount(2); // header and the rising cap
    await expect(table.getByRole("row", { name: /Cap - Red/ })).toContainText("Rising");
    await page.getByLabel("Show").selectOption("slow_mover");
    await expect(table.getByRole("row", { name: /Wool Hat/ })).toContainText("Slow mover");
    await page.getByLabel("Show").selectOption("short");
    await expect(table.getByRole("row")).toHaveCount(3);
    await page.getByLabel("Show").selectOption("all");
    await expect(table.getByRole("row")).toHaveCount(6);
  });

  test("downloads one supplier's purchase order draft and reads back the file", async ({ page }) => {
    await openInventory(page, KEY_A);
    const card = page.getByTestId("po-Acme Goods");
    await expect(card).toContainText("Lead time 9 days, expected 2026-12-03, $480.00");
    await expect(card).toContainText("60 x Studio Tee (TEE-1)");
    const [download] = await Promise.all([page.waitForEvent("download"), card.getByRole("button", { name: "CSV for Acme Goods" }).click()]);
    expect(download.suggestedFilename()).toBe("purchase-orders-2026-11-24-acme-goods.csv");
    expect(readCsv(readFileSync(await download.path()))).toEqual([
      ["supplier", "expected_on", "sku", "description", "quantity", "unit_cost", "line_total", "currency", "days_of_cover", "class"],
      ["Acme Goods", "2026-12-03", "TEE-1", "Studio Tee", "60", "8.00", "480.00", "USD", "5.0", "best_seller"],
    ]);
    await expect(page.getByRole("status")).toContainText("Saved purchase-orders-2026-11-24-acme-goods.csv");

    const borealis = page.getByTestId("po-Borealis Textiles");
    await expect(borealis).toContainText("1 line(s) have no unit cost and are left out of the total.");
    const [paste] = await Promise.all([page.waitForEvent("download"), borealis.getByRole("button", { name: "Sheets paste for Borealis Textiles" }).click()]);
    expect(paste.suggestedFilename()).toBe("purchase-orders-2026-11-24-borealis-textiles.tsv");
    expect(readFileSync(await paste.path(), "utf8").trimEnd().split("\r\n")[1]).toBe("Borealis Textiles\t2026-12-24\tSOCK-L\tWool Sock - Large\t120\t\t\tUSD\t0.0\tsteady");
  });

  test("a viewer key is refused a promotion with its reason, and a bad factor is caught before it is sent", async ({ page }) => {
    await openInventory(page, KEY_A);
    await page.getByLabel("From", { exact: true }).fill("2026-12-10");
    await page.getByLabel("To", { exact: true }).fill("2026-12-12");
    await page.getByLabel("Factor").fill("0");
    await page.getByRole("button", { name: "Save promotion" }).click();
    await expect(page.locator('.error')).toContainText("The factor must be above 0");
    await page.getByLabel("Factor").fill("2");
    await page.getByRole("button", { name: "Save promotion" }).click();
    await expect(page.locator('.error')).toContainText("A viewer key cannot enter a promotion.");
  });

  test("a key with nothing to order says so, and another tenant's page shows only its own plan", async ({ page }) => {
    await openInventory(page, KEY_B);
    await expect(page.getByTestId("plan-date")).toContainText("Bravo Bakery");
    await expect(page.getByText("Nothing needs ordering from a supplier.")).toBeVisible();
    await expect(page.getByRole("region", { name: "Planning table" }).getByRole("row", { name: /Bravo Loaf/ })).toBeVisible();
    await expect(page.getByText("Studio Tee")).toHaveCount(0);
  });
});
