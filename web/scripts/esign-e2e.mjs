// LIFESPAN: permanent
// Drives the built portal in a real browser against a RUNNING backend: a firm creates a signing request over the API, the client signs in at their subdomain, reads the page,
// is refused with a wrong name, signs, and the audit log and the other tenant's key are checked. Chromium maps every *.portal.example.com name to 127.0.0.1, so no DNS is used.
//
//   backend: DATABASE_URL=<the analytics database> ESIGN_PII_KEY=<any text> ESIGN_TRUST_FORWARDED=1 uvicorn app.api.main:app --port 8100
//   portal:  PORTAL_BASE_DOMAIN=portal.example.com DATABASE_URL=<same database> ESIGN_API_BASE=http://127.0.0.1:8100 npx next start -p 3200
//   run:     ESIGN_E2E_OPERATOR_KEY=<alpha operator key> ESIGN_E2E_ALPHA_VIEWER_KEY=<alpha viewer key> ESIGN_E2E_BRAVO_VIEWER_KEY=<bravo viewer key> \
//            node scripts/esign-e2e.mjs http://127.0.0.1:3200 http://127.0.0.1:8100
//
// The database needs migrations 001 to 003 and 016, tenants with the slugs alpha and bravo, and one operator and one viewer key for alpha and a viewer key for bravo.
// Exit 0 only when every check passed.
import http from "node:http";
import { chromium } from "@playwright/test";

const web = new URL(process.argv[2] ?? "http://127.0.0.1:3200");
const api = (process.argv[3] ?? "http://127.0.0.1:8100").replace(/\/+$/, "");
const BASE = process.env.PORTAL_BASE_DOMAIN ?? "portal.example.com";
const need = (name) => {
  const v = process.env[name];
  if (!v) throw new Error(`${name} is not set`);
  return v;
};
const OPERATOR = need("ESIGN_E2E_OPERATOR_KEY");
const ALPHA_VIEWER = need("ESIGN_E2E_ALPHA_VIEWER_KEY");
const BRAVO_VIEWER = need("ESIGN_E2E_BRAVO_VIEWER_KEY");
const alphaUrl = (p) => `http://alpha.${BASE}:${web.port}${p}`;
const bravoUrl = (p) => `http://bravo.${BASE}:${web.port}${p}`;
const DOCUMENT = Buffer.from("%PDF-1.4\nPortal browser run, DOCUMENT-MARKER-9917\n%%EOF\n");

let failed = 0;
function check(name, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok ? "" : `  ${detail}`}`);
  if (!ok) failed++;
}

async function call(method, path, key, body) {
  const res = await fetch(`${api}${path}`, {
    method,
    headers: { Authorization: `Bearer ${key}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  return { status: res.status, json: await res.json().catch(() => null) };
}

const created = await call("POST", "/api/esign/requests", OPERATOR, {
  document_b64: DOCUMENT.toString("base64"),
  document_name: "Engagement letter.pdf",
  signer_name: "Dana Whitfield",
  signer_email: "dana@example.com",
  expires_in_hours: 24,
});
check("a firm user creates the request over the API", created.status === 201 && created.json?.status === "pending", JSON.stringify(created));
const id = created.json.id;
const hash = created.json.document_sha256;

const browser = await chromium.launch({ args: [`--host-resolver-rules=MAP *.${BASE} 127.0.0.1`] });
const context = await browser.newContext();
const page = await context.newPage();

async function signIn(urlOf, key) {
  await page.goto(urlOf("/"));
  await page.fill('input[name="key"]', key);
  await page.click('button[type="submit"]');
  await page.waitForTimeout(1000);
  await page.goto(urlOf("/"));                    // a fresh load: the portal's sign-in action redirects through a render that does not see the host rewrite
  await page.waitForSelector("text=Signed in as");
}

try {
  await signIn(alphaUrl, ALPHA_VIEWER);
  const home = await page.textContent("main");
  check("the client signs in at alpha and sees the document to sign", /Documents to sign/.test(home) && /Engagement letter\.pdf/.test(home), home.slice(0, 200));

  await page.click(`a[href="/sign/${id}"]`);
  await page.waitForURL(alphaUrl(`/sign/${id}`));
  const text = await page.textContent("main");
  check("the page shows the document's SHA-256", text.includes(hash), text.slice(0, 200));
  check("the page shows the consent text", /Consent to sign electronically/.test(text) && /Intent to sign/.test(text) && /Retention/.test(text));
  await page.screenshot({ path: process.env.ESIGN_E2E_SHOT_BEFORE ?? "esign-before.png", fullPage: true });

  const inPage = (pg, url) =>
    pg.evaluate(async (u) => {
      const r = await fetch(u);
      return { status: r.status, sha: r.headers.get("x-document-sha256"), bytes: Array.from(new Uint8Array(await r.arrayBuffer())) };
    }, url);
  const doc = await inPage(page, alphaUrl(`/sign/${id}/document`));
  check("the document downloads through the portal with the key kept on the server", doc.status === 200 && Buffer.from(doc.bytes).equals(DOCUMENT) && doc.sha === hash, doc.status);
  const anon = await new Promise((resolve) => {
    const req = http.request({ host: web.hostname, port: web.port, path: `/sign/${id}/document`, headers: { host: `alpha.${BASE}:${web.port}` } }, (res) => {
      res.resume();
      resolve(res.statusCode);
    });
    req.on("error", () => resolve(0));
    req.end();
  });
  check("the same address without the sign-in cookie is a 404", anon === 404, anon);

  await page.fill('input[name="typed_name"]', "Eve Mallory");
  await page.check('input[name="consent"]');
  await Promise.all([page.waitForURL(/error=/), page.click('button[name="intent"][value="sign"]')]);
  const refused = await page.textContent("main");
  check("a wrong name is refused with words on the page", /typed name/i.test(refused) && !/Signed by/.test(refused), refused.slice(0, 300));

  await page.fill('input[name="typed_name"]', "  dana WHITFIELD ");
  await page.check('input[name="consent"]');
  await Promise.all([page.waitForURL(/done=signed/), page.click('button[name="intent"][value="sign"]')]);
  const signed = await page.textContent("main");
  check("the right name and the tick sign it", /Signed\. Your signature is recorded/.test(signed) && /Signed by dana WHITFIELD/.test(signed) && /Status: signed/.test(signed), signed.slice(0, 400));
  check("a signed request offers no form to sign again", (await page.locator('input[name="typed_name"]').count()) === 0);
  await page.screenshot({ path: process.env.ESIGN_E2E_SHOT_AFTER ?? "esign-after.png", fullPage: true });

  const report = await call("POST", `/api/esign/requests/${id}/verify`, ALPHA_VIEWER);
  check("verify is true for the record the portal made", report.status === 200 && report.json.ok === true && report.json.problems.length === 0, JSON.stringify(report.json));
  const cert = await call("GET", `/api/esign/requests/${id}/certificate`, OPERATOR);
  const kinds = cert.json?.chain?.events?.map((e) => e.kind).join(",");
  check("the certificate lists created, viewed, consented and signed", cert.status === 200 && /^created,viewed(,viewed)*,consented,signed$/.test(kinds ?? ""), kinds);
  check("the certificate holds no raw user agent and no address", !/HeadlessChrome|Mozilla|127\.0\.0\.1/.test(JSON.stringify(cert.json)));

  // the other portal
  const bravo = await browser.newContext();
  const bravoPage = await bravo.newPage();
  await bravoPage.goto(bravoUrl("/"));
  await bravoPage.fill('input[name="key"]', BRAVO_VIEWER);
  await bravoPage.click('button[type="submit"]');
  await bravoPage.waitForTimeout(1000);
  await bravoPage.goto(bravoUrl("/"));
  await bravoPage.waitForSelector("text=Signed in as");
  await bravoPage.goto(bravoUrl(`/sign/${id}`));
  const theirs = await bravoPage.textContent("main");
  check("bravo's key at bravo's portal cannot open alpha's request", /not found/i.test(theirs) && !theirs.includes(hash) && !/Engagement letter/.test(theirs), theirs.slice(0, 300));
  const theirDoc = await inPage(bravoPage, bravoUrl(`/sign/${id}/document`));
  check("nor download its document", theirDoc.status === 404, theirDoc.status);
  const crossApi = await call("POST", `/api/esign/requests/${id}/verify`, BRAVO_VIEWER);
  check("nor verify it over the API: the same 404 as for an id nobody has", crossApi.status === 404 && crossApi.json?.detail === "signing request not found", JSON.stringify(crossApi));
  await bravo.close();

  const alphaKeyAtBravo = await browser.newContext();
  const p3 = await alphaKeyAtBravo.newPage();
  await p3.goto(bravoUrl("/"));
  await p3.fill('input[name="key"]', ALPHA_VIEWER);
  await p3.click('button[type="submit"]');
  await p3.waitForTimeout(1000);
  await p3.goto(bravoUrl("/"));
  const refusedKey = (await p3.textContent("main")) ?? "";
  check("alpha's key at bravo's portal is refused: still the sign-in form", /Sign in with your viewer key/.test(refusedKey) && !/Signed in as/.test(refusedKey), refusedKey.slice(0, 200));
  await alphaKeyAtBravo.close();
} finally {
  await browser.close();
}

if (failed) {
  console.log(`${failed} check(s) failed`);
  process.exit(1);
}
console.log("every check passed");
