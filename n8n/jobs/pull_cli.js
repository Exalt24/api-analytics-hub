// LIFESPAN: permanent
// Runs pull.js and normalize.js outside n8n, over real HTTP with fetch, for the tests and for a quick look at one vendor.
//   echo '{"baseUrl":"http://127.0.0.1:9000","vendors":{"theirstack":{"apiKey":"..."}}}' | node n8n/jobs/pull_cli.js
// Prints one JSON object: {vendor: {received, invalid, jobs}} or {vendor: {error}} when a pull raised.
const { pullVendor } = require('./pull.js');
const { normalizeBatch } = require('./normalize.js');

async function http(opts) {
  const u = new URL(opts.url);
  for (const [k, v] of Object.entries(opts.qs || {})) if (v !== undefined && v !== null) u.searchParams.set(k, String(v));
  const hasBody = opts.body !== undefined;
  const res = await fetch(u, { method: opts.method || 'GET', headers: { ...(hasBody ? { 'Content-Type': 'application/json' } : {}), ...(opts.headers || {}) }, body: hasBody ? JSON.stringify(opts.body) : undefined });
  const text = await res.text();
  let body = text;
  try { body = JSON.parse(text); } catch (e) { /* keep the text */ }
  const headers = {};
  res.headers.forEach((v, k) => { headers[k.toLowerCase()] = v; });
  return { status: res.status, headers, body };
}

async function main() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const out = {};
  for (const [vendor, cfg] of Object.entries(input.vendors)) {
    try {
      const body = await pullVendor(vendor, { baseUrl: input.baseUrl, titles: ['Engineer'], pollMs: 20, ...cfg }, http, (ms) => new Promise((r) => setTimeout(r, Math.min(ms, 50))));
      const n = normalizeBatch(vendor, body);
      out[vendor] = { received: n.received, invalid: n.invalid, jobs: n.jobs };
    } catch (e) {
      out[vendor] = { error: String(e.message || e) };
    }
  }
  process.stdout.write(JSON.stringify(out));
}

main();
