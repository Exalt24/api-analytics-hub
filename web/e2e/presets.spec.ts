// OBSERVE-OK: a Playwright test; every action is followed by an assertion on what the page shows, and each test ends with a screenshot.
/**
 * Saved report presets in real Chromium on the built dashboard: the browser's IndexedDB holds a preset made
 * while the sync endpoints are unreachable, it survives a page reload, it is sent when the endpoints come
 * back, and a second browser profile (a new context has its own IndexedDB) receives it by pull. A third
 * profile on another tenant's key sees nothing. "Offline" here is the two /api/replicache routes aborted,
 * which is what the page experiences when the network is down for sync; the page itself stays loaded.
 */
import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { KEY_A, KEY_B } from "../tests/fixtures";

async function openReports(page: Page, key: string): Promise<void> {
  await page.goto("/reports");
  await expect(page.getByRole("heading", { name: "Reports", exact: true })).toBeVisible();
  await page.getByLabel("API key").fill(key);
  await page.getByRole("button", { name: "Use key" }).click();
  await expect(page.getByText("Signed in with an API key.")).toBeVisible();
  await expect(page.getByLabel("Preset name")).toBeVisible();
}

const blockSync = (context: BrowserContext) => context.route("**/api/replicache/**", (route) => route.abort("internetdisconnected"));
const status = (page: Page) => page.getByTestId("sync-status");

test.describe("Saved presets, local first", () => {
  test.afterEach(async ({ page }, testInfo) => {
    await page.screenshot({ path: testInfo.outputPath("final.png"), fullPage: true });
  });

  test("a preset saved with sync unreachable survives a reload, then syncs to a second browser profile and not to another tenant", async ({ page, browser }) => {
    await blockSync(page.context());
    await openReports(page, KEY_A);

    await page.getByRole("combobox", { name: "Format" }).click();
    await page.getByRole("option", { name: "PDF" }).click();
    await page.getByRole("switch", { name: "Include AI summary" }).click();
    await page.getByLabel("Preset name").fill("Board pack");
    await page.getByRole("button", { name: "Save preset" }).click();

    const list = page.getByRole("list", { name: "Saved presets" });
    await expect(list).toContainText("Board pack");
    await expect(list).toContainText("PDF, summary");
    await expect(status(page)).toHaveText(/Offline|1 change waiting to sync/);

    // IndexedDB keeps it: reload the page (sync is still blocked) and it is still there with its change still queued.
    await page.reload();
    await expect(page.getByLabel("Preset name")).toBeVisible();
    await expect(page.getByRole("list", { name: "Saved presets" })).toContainText("Board pack");
    await expect(status(page)).toHaveText(/Offline|1 change waiting to sync/);

    // Back online: the queued change is pushed and the status settles.
    await page.context().unroute("**/api/replicache/**");
    await expect(status(page)).toHaveText("Everything is synced.", { timeout: 60_000 });

    // A second browser profile with the same key receives it by pull.
    const second = await browser.newContext();
    const otherDevice = await second.newPage();
    await openReports(otherDevice, KEY_A);
    await expect(otherDevice.getByRole("list", { name: "Saved presets" })).toContainText("Board pack", { timeout: 30_000 });
    await expect(otherDevice.getByRole("list", { name: "Saved presets" })).toContainText("PDF, summary");
    await otherDevice.screenshot({ path: test.info().outputPath("second-device.png"), fullPage: true });

    // Another tenant's key sees none of it.
    const third = await browser.newContext();
    const bravo = await third.newPage();
    await openReports(bravo, KEY_B);
    await expect(status(bravo)).toHaveText("Everything is synced.", { timeout: 30_000 });
    await expect(bravo.getByRole("list", { name: "Saved presets" })).toContainText("No presets yet.");
    await expect(bravo.getByRole("list", { name: "Saved presets" })).not.toContainText("Board pack");
    await bravo.screenshot({ path: test.info().outputPath("other-tenant.png"), fullPage: true });

    // Use applies the preset to the export form.
    await expect(otherDevice.getByRole("combobox", { name: "Format" })).toContainText("CSV");
    await otherDevice.getByRole("button", { name: "Use Board pack" }).click();
    await expect(otherDevice.getByRole("combobox", { name: "Format" })).toContainText("PDF");
    await expect(otherDevice.getByRole("switch", { name: "Include AI summary" })).toBeChecked();

    await second.close();
    await third.close();
  });

  test("a delete reaches the other profile and an empty name is refused", async ({ page, browser }) => {
    await openReports(page, KEY_A);
    await page.getByRole("button", { name: "Save preset" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "Give the preset a name." })).toBeVisible();

    await page.getByLabel("Preset name").fill("Weekly sheet");
    await page.getByRole("button", { name: "Save preset" }).click();
    await expect(status(page)).toHaveText("Everything is synced.", { timeout: 60_000 });

    const second = await browser.newContext();
    const other = await second.newPage();
    await openReports(other, KEY_A);
    await expect(other.getByRole("list", { name: "Saved presets" })).toContainText("Weekly sheet", { timeout: 30_000 });

    await other.getByRole("button", { name: "Delete Weekly sheet" }).click();
    await expect(other.getByRole("list", { name: "Saved presets" })).not.toContainText("Weekly sheet");
    await expect(status(other)).toHaveText("Everything is synced.", { timeout: 60_000 });

    // The first profile learns of it on its next pull (every 10 seconds, or when the page comes back online).
    await expect(page.getByRole("list", { name: "Saved presets" })).not.toContainText("Weekly sheet", { timeout: 30_000 });
    await second.close();
  });
});
