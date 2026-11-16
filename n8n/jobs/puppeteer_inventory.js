// LIFESPAN: permanent
// The form inventory through Puppeteer: open a page, run app/jobs/inventory.js in it, print the fields as JSON.
//   node n8n/jobs/puppeteer_inventory.js <url> [--chrome <path to a Chromium or Chrome>]
// The Python filler runs the same file through Playwright, and tests/test_jobs_puppeteer.py checks that both drivers read the same fields from the same pages.
// puppeteer-core is installed by `npm install` in this folder; it downloads no browser, so a Chromium has to be named (--chrome or CHROME_PATH).
const fs = require('node:fs');
const path = require('node:path');

async function main() {
  const args = process.argv.slice(2);
  const url = args.find((a) => /^https?:\/\//.test(a));
  const i = args.indexOf('--chrome');
  const chrome = (i >= 0 && args[i + 1]) || process.env.CHROME_PATH;
  if (!url || !chrome) {
    process.stderr.write('usage: node puppeteer_inventory.js <url> --chrome <path>\n');
    process.exit(2);
  }
  const puppeteer = require('puppeteer-core');
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'app', 'jobs', 'inventory.js'), 'utf8');
  const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage();
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    // Puppeteer's evaluate takes a function or an expression; the file is a function, so it is wrapped and called
    const fields = await page.evaluate('(' + source + ')()');
    process.stdout.write(JSON.stringify(fields));
  } finally {
    await browser.close();
  }
}

main().catch((e) => { process.stderr.write(String(e && e.stack || e) + '\n'); process.exit(1); });
