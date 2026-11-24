// OBSERVE-OK: a Playwright test; every action is followed by an assertion on what the page shows or what the download holds, and each test ends with a screenshot.
/**
 * Drives the built dashboard in Chromium: open Inventory, sign in with a key, read the exceptions and the planning table, filter it, download a purchase order draft
 * and read it back, and meet the refusal a viewer key gets when it tries to enter a promotion. The expected figures are literals from tests/fixtures.ts (the stored plan).
 */
import { readFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { KEY_A, KEY_A_OPERATOR, KEY_B } from "../tests/fixtures";
import { readCsv, readXlsx } from "../tests/readback";

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
    await page.getByLabel("Factor", { exact: true }).fill("0");
    await page.getByRole("button", { name: "Save promotion" }).click();
    await expect(page.locator('.error')).toContainText("The factor must be above 0");
    await page.getByLabel("Factor", { exact: true }).fill("2");
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

  test("an operator makes an order from the draft, places it, receives it in parts, is refused an over-receipt and finishes it", async ({ page }) => {
    await openInventory(page, KEY_A_OPERATOR);
    await page.getByTestId("po-Acme Goods").getByRole("button", { name: "Create order for Acme Goods" }).click();
    await expect(page.getByRole("status")).toContainText("Draft order made for Acme Goods");
    const orders = page.getByRole("region", { name: "Purchase orders" });
    const card = orders.locator(".inv-order").filter({ hasText: "Acme Goods" }).filter({ hasText: "Draft" });
    await expect(card).toContainText("Not placed yet");
    await expect(card).toContainText("$480.00");
    await expect(card.getByRole("row", { name: /Studio Tee/ })).toContainText("60");
    await expect(card.getByRole("button", { name: "Place order" })).toBeVisible();
    await expect(card.getByRole("button", { name: "Record receipt" })).toHaveCount(0); // nothing is received on a draft

    await card.getByRole("button", { name: "Place order" }).click();
    const placed = orders.locator(".inv-order").filter({ hasText: "Acme Goods" }).filter({ hasText: "Ordered" });
    await expect(placed).toContainText(/Ordered \d{4}-\d{2}-\d{2}, expected \d{4}-\d{2}-\d{2}/);
    await expect(placed.getByRole("button", { name: "Place order" })).toHaveCount(0);
    await expect(placed.getByRole("button", { name: "Mark shipped" })).toBeVisible();

    await placed.getByLabel("Expected date for Acme Goods").fill("2030-01-15");
    await placed.getByRole("button", { name: "Save date" }).click();
    await expect(placed).toContainText("expected 2030-01-15");

    await placed.getByLabel("Received for TEE-1").fill("25");
    await placed.getByRole("button", { name: "Record receipt" }).click();
    const partly = orders.locator(".inv-order").filter({ hasText: "Partly received" });
    await expect(partly.getByRole("row", { name: /Studio Tee/ })).toContainText("25");
    await expect(partly.getByRole("row", { name: /Studio Tee/ })).toContainText("35"); // still open

    await partly.getByLabel("Received for TEE-1").fill("36");
    await partly.getByRole("button", { name: "Record receipt" }).click();
    await expect(page.locator(".error")).toContainText("more than was ordered");
    await expect(partly.getByRole("row", { name: /Studio Tee/ })).toContainText("35"); // the refusal recorded nothing

    await partly.getByLabel("Accept more than ordered").check();
    await partly.getByRole("button", { name: "Record receipt" }).click();
    const finished = orders.locator(".inv-order").filter({ hasText: "Received" });
    await expect(finished.getByRole("row", { name: /Studio Tee/ })).toContainText("accepted over");
    await expect(finished.getByRole("button", { name: "Record receipt" })).toHaveCount(0); // a received order takes no more
    await expect(finished.getByRole("button", { name: "Cancel order" })).toHaveCount(0);
  });

  test("a viewer key is refused an order with its reason", async ({ page }) => {
    await openInventory(page, KEY_A);
    await page.getByTestId("po-Borealis Textiles").getByRole("button", { name: "Create order for Borealis Textiles" }).click();
    await expect(page.locator(".error")).toContainText("A viewer key cannot change a purchase order.");
  });

  test("downloads the plan workbook through the backend with the key and the promotion settings", async ({ page, request }) => {
    await openInventory(page, KEY_A);
    await page.getByLabel("Promotion factor").fill("1.5");
    await page.getByLabel("Promotion from").fill("2026-12-01");
    await page.getByLabel("Promotion to").fill("2026-12-07");
    const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download workbook" }).click()]);
    expect(download.suggestedFilename()).toBe("inventory-plan-2026-11-24.xlsx");
    const book = await readXlsx(readFileSync(await download.path()));
    expect(book.sheets["Plan"]?.[0]).toEqual(["SKU", "Daily velocity", "Doubled"]);
    await expect(page.getByRole("status")).toContainText("Saved inventory-plan-2026-11-24.xlsx");
    const log = (await (await request.get("http://127.0.0.1:4011/__log")).json()) as { url: string; authorization: string }[];
    const last = log[log.length - 1];
    expect(last?.url).toBe("/api/inventory/workbook?promo_factor=1.5&promo_from=2026-12-01&promo_to=2026-12-07");
    expect(last?.authorization).toBe(`Bearer ${KEY_A}`);

    await page.getByLabel("Promotion factor").fill("0");
    await page.getByRole("button", { name: "Download workbook" }).click();
    await expect(page.locator(".error")).toContainText("The promotion factor must be above 0");
  });
});
