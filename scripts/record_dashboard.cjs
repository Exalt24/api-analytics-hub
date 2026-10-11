// Records the dashboard against a LOCAL stack (docker compose Postgres, seed_local.py, uvicorn on :8000, the built dashboard on :3100) for the README GIF.
//   node scripts/record_dashboard.cjs <admin key> <viewer key>        writes docs/demo.webm
// Needs Playwright installed globally (or PLAYWRIGHT_PATH). The page is opened at http://localhost:3100 (not 127.0.0.1: the API's CORS allows that origin).
// OBSERVE-OK: every wait polls the page every 250 ms for at most 8 seconds and the holds are reading time for a caption that is already on screen.
const path = require("path");
const fs = require("fs");
const { chromium } = require(process.env.PLAYWRIGHT_PATH || "playwright");
const [admin, viewer] = process.argv.slice(2);
if (!admin || !viewer) { console.error("usage: node scripts/record_dashboard.cjs <admin key> <viewer key>"); process.exit(2); }
const OUT = path.join(__dirname, "..", "docs");
fs.mkdirSync(OUT, { recursive: true });
const TMP = path.join(OUT, ".rec");
fs.rmSync(TMP, { recursive: true, force: true });

async function until(page, fn, arg, tries = 32) {
  for (let i = 0; i < tries; i++) { if (await page.evaluate(fn, arg).catch(() => false)) return true; await page.waitForTimeout(250); }
  return false;
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 760 }, colorScheme: "dark", recordVideo: { dir: TMP, size: { width: 1100, height: 760 } } });
  const page = await ctx.newPage();
  const caption = (t) => page.evaluate((text) => {
    let d = document.getElementById("demo-cap");
    if (!d) { d = document.createElement("div"); d.id = "demo-cap"; d.style.cssText = "position:fixed;left:0;right:0;bottom:0;padding:14px 20px;background:rgba(10,12,16,.92);color:#fff;font:600 20px system-ui,sans-serif;text-align:center;z-index:99999;border-top:1px solid #2a3340"; document.body.appendChild(d); }
    d.textContent = text;
  }, t);
  await page.goto("http://localhost:3100/", { waitUntil: "domcontentloaded" });
  if (!(await until(page, () => !!document.querySelector(".keygate")))) throw new Error("the key gate never appeared");
  await caption("A dashboard that needs an API key: the key decides the tenant and the role");
  await page.waitForTimeout(2200);

  await page.locator("input[aria-label='API key']").click();
  await page.keyboard.type(admin, { delay: 12 });
  await caption("Pasting the admin key");
  await page.waitForTimeout(900);
  await page.getByRole("button", { name: "Use key" }).click();
  if (!(await until(page, () => /Signed in with an API key/.test(document.body.innerText)))) throw new Error("the key was not accepted");
  await caption("Signed in as admin. The default window is the last 7 days");
  await page.waitForTimeout(1800);
  await page.getByRole("button", { name: "90 days" }).click();
  if (!(await until(page, () => !/ORDERS\s*not available/i.test(document.body.innerText)))) throw new Error("no KPI numbers appeared for 90 days");
  await caption("90 days: orders, gross, refunds, net and buyers per day, read from stored Shopify data");
  await page.waitForTimeout(3600);
  await page.getByRole("button", { name: "30 days" }).click();
  await page.waitForTimeout(1500);
  await page.getByRole("button", { name: "90 days" }).click();
  await page.waitForTimeout(1500);

  await page.getByRole("button", { name: "Sync now" }).click();
  const queued = await until(page, () => /Sync queued/i.test(document.body.innerText), null, 32);
  if (!queued) throw new Error("the admin's Sync now was not accepted");
  await caption("An admin can queue a sync: the server accepts it");
  await page.waitForTimeout(3000);

  await page.getByRole("button", { name: "sign out" }).click();
  if (!(await until(page, () => !!document.querySelector(".keygate")))) throw new Error("the key gate did not come back after sign out");
  await page.reload({ waitUntil: "domcontentloaded" });   // the admin's queued sync leaves the Sync button busy until it finishes: a fresh page state for the viewer
  if (!(await until(page, () => !!document.querySelector(".keygate")))) throw new Error("the key gate did not render after the reload");
  await page.waitForTimeout(1200);
  await page.locator("input[aria-label='API key']").click();
  await page.keyboard.type(viewer, { delay: 12 });
  await caption("Now the viewer key: the same page with a lower role");
  await page.waitForTimeout(900);
  await page.getByRole("button", { name: "Use key" }).click();
  if (!(await until(page, () => /Signed in with an API key/.test(document.body.innerText)))) throw new Error("the viewer key was not accepted");
  await page.getByRole("button", { name: "90 days" }).click();
  if (!(await until(page, () => !/ORDERS\s*not available/i.test(document.body.innerText)))) throw new Error("the viewer saw no numbers for 90 days");
  await page.waitForTimeout(2200);
  await page.getByRole("button", { name: "Sync now" }).click();
  const refused = await until(page, () => /may not write:sync/i.test(document.body.innerText), null, 32);
  if (!refused) throw new Error("the viewer's Sync now was not refused with the role message");
  await caption("A viewer can read but cannot sync: the server refuses, and says why");
  await page.waitForTimeout(4200);

  const video = page.video();
  await ctx.close();
  await browser.close();
  const src = await video.path();
  fs.copyFileSync(src, path.join(OUT, "demo.webm"));
  fs.rmSync(TMP, { recursive: true, force: true });
  console.log("wrote docs/demo.webm");
})().catch((e) => { console.error("FAILED:", String(e).split("\n")[0]); process.exit(1); });
