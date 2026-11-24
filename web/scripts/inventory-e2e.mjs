// LIFESPAN: permanent
// OBSERVE-OK: every action is followed by a wait on what the page shows, and the run ends with a screenshot that is read.
// Drives the BUILT dashboard's Inventory page in Chromium against a database that holds a stored plan (the live run's), signs in with a key,
// reads the page, downloads the purchase order drafts and writes a full page screenshot.
//
//   DATABASE_URL=<the database as the app role> npx next start -p 3100
//   node scripts/inventory-e2e.mjs http://127.0.0.1:3100 <api key> <output folder>
//
// With INVENTORY_E2E_SERVE=1 and DATABASE_URL set it starts `next start` on the port of the address itself and stops it at the end.
// With INVENTORY_E2E_ORDERS=1 it also makes a purchase order from the first draft, places it, receives part of it, meets the refusal of an over-receipt and downloads the plan
// workbook; that needs INVENTORY_API_BASE to point at the running backend (the workbook is built there), and it changes the database it runs against.
// Prints what the page showed and what the downloaded file held. Exit 0 only when every check passed.
import { spawn } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import ExcelJS from "exceljs";
import { chromium } from "@playwright/test";

const [base = "http://127.0.0.1:3100", key, outDir = "inventory-run"] = process.argv.slice(2);
if (!key) {
  console.error("usage: node scripts/inventory-e2e.mjs <base url> <api key> [output folder]");
  process.exit(2);
}
mkdirSync(outDir, { recursive: true });

let server;
if (process.env.INVENTORY_E2E_SERVE === "1") {
  const port = new URL(base).port || "3100";
  server = spawn(process.execPath, ["node_modules/next/dist/bin/next", "start", "-p", port], { env: process.env, stdio: "ignore" });
  let up = false;
  for (let i = 0; i < 60 && !up; i += 1) {
    await new Promise((r) => setTimeout(r, 300));
    up = await fetch(`${base}/inventory`).then((r) => r.ok, () => false);
  }
  if (!up) {
    server.kill();
    console.error("FAIL   the server did not come up");
    process.exit(1);
  }
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
let failed = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok ? "" : `  ${detail}`}`);
  if (!ok) failed += 1;
};

try {
  await page.goto(`${base}/`);
  await page.getByRole("link", { name: "Inventory" }).click();
  await page.getByRole("heading", { name: "Inventory", level: 1 }).waitFor({ timeout: 8000 });
  await page.getByLabel("API key").fill(key);
  await page.getByRole("button", { name: "Use key" }).click();
  await page.getByTestId("plan-date").waitFor({ timeout: 8000 });
  console.log("page:", (await page.getByTestId("plan-date").innerText()).trim());

  if (process.env.INVENTORY_E2E_LOOK === "1") {
    // a look at the purchase orders already stored: what the page says about each one, and a screenshot of the section
    const section = page.getByRole("region", { name: "Purchase orders" });
    await section.waitFor({ timeout: 8000 });
    for (const card of await section.locator(".inv-order").all()) console.log("order:", (await card.innerText()).replace(/\s+/g, " ").slice(0, 300));
    const late = await section.locator(".inv-order").filter({ hasText: "Late" }).count();
    check("an open order past its expected date is marked late on the page", late > 0, String(late));
    const planning = page.getByRole("region", { name: "Planning table" });
    console.log("planning rows with inbound units:", (await planning.getByRole("row").filter({ hasText: "inbound" }).allInnerTexts()).map((r) => r.replace(/\s+/g, " ")).join(" | "));
    await section.screenshot({ path: path.join(outDir, "inventory-orders-late.png") });
    await browser.close();
    server?.kill();
    process.exit(failed === 0 ? 0 : 1);
  }

  const cards = await page.getByRole("region", { name: "Summary" }).innerText();
  console.log("summary:", cards.replace(/\s+/g, " "));
  const exceptions = page.getByRole("region", { name: "Exceptions" });
  const exceptionRows = await exceptions.getByRole("row").allInnerTexts();
  console.log("exceptions:");
  for (const r of exceptionRows) console.log("  " + r.replace(/\s+/g, " "));
  check("the page lists at least one exception with a kind", exceptionRows.length > 1 && /Out of stock|Cover under lead time|Stockout projected/.test(exceptionRows.join(" ")));

  const table = page.getByRole("region", { name: "Planning table" });
  const rows = await table.getByRole("row").allInnerTexts();
  console.log(`planning table: ${rows.length - 1} variants`);
  check("the planning table names a best seller, a rising and a slow mover", ["Best seller", "Rising", "Slow mover"].every((w) => rows.join(" ").includes(w)));

  await page.getByLabel("Show").selectOption("rising");
  const rising = await table.getByRole("row").allInnerTexts();
  console.log("filter Rising:", rising.slice(1).map((r) => r.replace(/\s+/g, " ")).join(" | "));
  check("the Rising filter shows only rising variants", rising.length > 1 && rising.slice(1).every((r) => r.includes("Rising")));
  await page.getByLabel("Show").selectOption("all");

  const cardsHtml = page.locator('.inv-order[data-testid^="po-"]');
  const n = await cardsHtml.count();
  check("a purchase order draft is shown for at least one supplier", n > 0, String(n));
  for (let i = 0; i < n; i += 1) {
    const card = cardsHtml.nth(i);
    const title = (await card.locator("h3").innerText()).trim();
    console.log(`purchase order draft, ${title}:`, (await card.innerText()).replace(/\s+/g, " ").slice(0, 300));
    for (const kind of ["CSV", "Excel", "Sheets paste"]) {
      const [download] = await Promise.all([page.waitForEvent("download"), card.getByRole("button", { name: `${kind} for ${title}` }).click()]);
      const target = path.join(outDir, download.suggestedFilename());
      await download.saveAs(target);
      console.log(`  saved ${download.suggestedFilename()}`);
      if (kind === "CSV") console.log(readFileSync(target, "utf8").trimEnd().split(/\r?\n/).map((l) => "    " + l).join("\n"));
    }
  }
  // purchase orders: make one from the first supplier draft, place it, receive part of it, then download the plan workbook through the backend
  if (n > 0 && process.env.INVENTORY_E2E_ORDERS === "1") {
    const draftCard = page.locator('.inv-order[data-testid^="po-"]').first();
    const supplier = (await draftCard.locator("h3").innerText()).trim();
    await draftCard.getByRole("button", { name: `Create order for ${supplier}` }).click();
    const orders = page.getByRole("region", { name: "Purchase orders" });
    const draft = orders.locator(".inv-order").filter({ hasText: supplier }).filter({ hasText: "Draft" }).first();
    await draft.waitFor({ timeout: 8000 });
    console.log("order made from the draft:", (await draft.innerText()).replace(/\s+/g, " ").slice(0, 220));
    check("the order starts as a draft that is not placed", (await draft.innerText()).includes("Not placed yet"));
    await draft.getByRole("button", { name: "Place order" }).click();
    const placed = orders.locator(".inv-order").filter({ hasText: supplier }).filter({ has: page.getByRole("button", { name: "Mark shipped" }) }).first();
    await placed.waitFor({ timeout: 8000 });
    console.log("order placed:", (await placed.innerText()).replace(/\s+/g, " ").slice(0, 220));
    const qty = placed.getByLabel(/^Received for /).first();
    await qty.fill("5");
    await placed.getByRole("button", { name: "Record receipt" }).click();
    const partly = orders.locator(".inv-order").filter({ hasText: "Partly received" }).first();
    await partly.waitFor({ timeout: 8000 });
    console.log("after a receipt of 5:", (await partly.innerText()).replace(/\s+/g, " ").slice(0, 260));
    check("a partial receipt leaves the order partly received", (await partly.innerText()).includes("Partly received"));
    await partly.getByLabel(/^Received for /).first().fill("9999");
    await partly.getByRole("button", { name: "Record receipt" }).click();
    const refusal = await page.locator(".error").innerText();
    console.log("an over-receipt is refused:", refusal);
    check("an over-receipt is refused with its reason", refusal.includes("more than was ordered"));
    const lateCards = await orders.locator(".inv-order").filter({ hasText: "Late" }).count();
    console.log(`orders marked late on the page: ${lateCards}`);
    await orders.screenshot({ path: path.join(outDir, "inventory-orders.png") });
    const [book] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download workbook" }).click()]);
    const bookPath = path.join(outDir, book.suggestedFilename());
    await book.saveAs(bookPath);
    const wb = new ExcelJS.Workbook();
    await wb.xlsx.readFile(bookPath);
    const plan = wb.getWorksheet("Plan");
    const formula = plan?.getCell("T2").value;
    console.log(`workbook downloaded: ${book.suggestedFilename()}, sheets ${wb.worksheets.map((w) => w.name).join(", ")}, Plan!T2 ${JSON.stringify(formula)}`);
    check("the workbook has its four sheets and a live formula", wb.worksheets.length === 4 && typeof formula === "object" && formula !== null && "formula" in formula);
  }
  await page.screenshot({ path: path.join(outDir, "inventory.png"), fullPage: true });
  await page.screenshot({ path: path.join(outDir, "inventory-top.png"), fullPage: true, clip: { x: 0, y: 0, width: 1280, height: 1010 } });
  console.log("screenshot:", path.join(outDir, "inventory.png"));
} catch (err) {
  failed += 1;
  console.error("FAIL  ", String(err).split("\n")[0]);
  await page.screenshot({ path: path.join(outDir, "failure.png"), fullPage: true }).catch(() => undefined);
} finally {
  await browser.close();
  server?.kill();
}
process.exit(failed === 0 ? 0 : 1);
