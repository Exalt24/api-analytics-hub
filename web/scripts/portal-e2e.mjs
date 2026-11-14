// LIFESPAN: permanent
// Drives a RUNNING web server (npm run start) over real HTTP with different Host headers, so the
// proxy, the portal page and the key check are exercised together.
//
//   PORTAL_BASE_DOMAIN=portal.example.com DATABASE_URL=<the scratch database as web_test_login> npm run start
//   node scripts/portal-e2e.mjs http://127.0.0.1:3000
//
// The keys are the fixture keys of tests/fixtures.ts, so run it against the Vitest scratch database
// (analytics_web_test). Exit 0 only when every check passed.
import http from "node:http";

const target = new URL(process.argv[2] ?? "http://127.0.0.1:3000");
const BASE = process.env.PORTAL_BASE_DOMAIN ?? "portal.example.com";
const KEY_A = "aah_test_alpha_viewer_key_0001";
const KEY_B = "aah_test_bravo_operator_key_0003";

function get(host, path, cookie) {
  return new Promise((resolve, reject) => {
    const headers = { host };
    if (cookie) headers.cookie = cookie;
    const req = http.request(
      { host: target.hostname, port: target.port, path, method: "GET", headers },
      (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (c) => (body += c));
        // React puts <!-- --> between adjacent text nodes; a check reads the text, not the markers
        res.on("end", () => resolve({ status: res.statusCode, body: body.replaceAll("<!-- -->", "") }));
      },
    );
    req.on("error", reject);
    req.end();
  });
}

let failed = 0;
function check(name, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok ? "" : `  ${detail}`}`);
  if (!ok) failed++;
}
const has = (r, s) => r.body.includes(s);

const alphaHost = `alpha.${BASE}`;
const bravoHost = `bravo.${BASE}`;

const anonA = await get(alphaHost, "/");
check("alpha subdomain, no key: the sign-in form and no tenant name", anonA.status === 200 && has(anonA, "Sign in with your viewer key") && !has(anonA, "Alpha Outfitters"), anonA.status);

const a = await get(alphaHost, "/", `portal_key=${KEY_A}`);
check("alpha key at alpha: Alpha's name and its 15 figures", a.status === 200 && has(a, "Alpha Outfitters") && has(a, "15 stored figures"), a.status);
check("alpha key at alpha: nothing of Bravo's", !has(a, "Bravo Bakery") && !has(a, "777"));

const cross = await get(bravoHost, "/", `portal_key=${KEY_A}`);
check("alpha key at bravo: refused, shows the form", cross.status === 200 && has(cross, "does not open this portal") && !has(cross, "Alpha Outfitters") && !has(cross, "Bravo Bakery") && !has(cross, "stored figures"), cross.status);

const b = await get(bravoHost, "/", `portal_key=${KEY_B}`);
check("bravo key at bravo: Bravo's name and its 3 figures", b.status === 200 && has(b, "Bravo Bakery") && has(b, "3 stored figures"), b.status);

const crossB = await get(alphaHost, "/", `portal_key=${KEY_B}`);
check("bravo key at alpha: refused", has(crossB, "does not open this portal") && !has(crossB, "Bravo Bakery") && !has(crossB, "Alpha Outfitters"));

const bad = await get(alphaHost, "/", "portal_key=not-a-key");
check("a wrong key: refused", has(bad, "does not open this portal") && !has(bad, "stored figures"));

const nobody = await get(`nobody.${BASE}`, "/", `portal_key=${KEY_A}`);
check("a subdomain no tenant owns, with a valid key: refused the same way", has(nobody, "does not open this portal") && !has(nobody, "stored figures"));

for (const host of [alphaHost, bravoHost, "localhost", BASE]) {
  const direct = await get(host, "/portal/bravo", `portal_key=${KEY_B}`);
  check(`/portal/bravo asked for directly on ${host}: 404`, direct.status === 404 && !has(direct, "Bravo Bakery"), direct.status);
}

const apex = await get(BASE, "/");
check("the base domain itself is the ordinary dashboard, not a portal", apex.status === 200 && !has(apex, "Sign in with your viewer key"), apex.status);

const lookalike = await get(`${alphaHost}.evil.test`, "/", `portal_key=${KEY_A}`);
check("a lookalike host is not a portal", lookalike.status === 200 && !has(lookalike, "Alpha Outfitters") && !has(lookalike, "Sign in with your viewer key"), lookalike.status);

process.exit(failed ? 1 : 0);
