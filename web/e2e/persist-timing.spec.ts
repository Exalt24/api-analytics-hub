// LIFESPAN: permanent
// OBSERVE-OK: every action is followed by an assertion on what the page or IndexedDB holds.
/**
 * How long each browser engine takes to write a saved preset into IndexedDB while sync is unreachable.
 * Replicache shows a change on the page at once and persists it later; measured 2026-11-15 on the built
 * dashboard: Chromium about 120 ms, Firefox about 25 ms, WebKit about 1,350 ms. A tab closed or reloaded
 * inside that window loses the change, so the reload test waits for the write, and this test fails when an
 * engine takes longer than 10 seconds.
 */
import { expect, test } from "@playwright/test";
import { KEY_A } from "../tests/fixtures";
import { persistedAfter } from "./idb";

test("a saved preset reaches IndexedDB within 10 seconds in every engine", async ({ page, browserName }) => {
  await page.context().route("**/api/replicache/**", (route) => route.abort("internetdisconnected"));
  await page.goto("/reports");
  await page.getByLabel("API key").fill(KEY_A);
  await page.getByRole("button", { name: "Use key" }).click();
  await expect(page.getByLabel("Preset name")).toBeVisible();
  await page.getByLabel("Preset name").fill("TimingPack");
  await page.getByRole("button", { name: "Save preset" }).click();
  await expect(page.getByRole("list", { name: "Saved presets" })).toContainText("TimingPack");
  const ms = await persistedAfter(page, "TimingPack", 10_000);
  console.log(`persist timing ${browserName}: ${ms} ms`);
  expect(ms, "the preset never reached IndexedDB").toBeGreaterThanOrEqual(0);
  // leave the shared test tenant as it was found
  await page.getByRole("button", { name: "Delete TimingPack" }).click();
  await expect(page.getByRole("list", { name: "Saved presets" })).toContainText("No presets yet.");
});
