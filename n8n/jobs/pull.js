// LIFESPAN: permanent
// One pull per vendor: the request shape, the paging and the answer, from each vendor's own documentation page (docs/job-vendors.md names the page
// for each). Like normalize.js it is one pure-ish file with two homes: the n8n Code node carries a copy (build_job_workflow.py inlines it), and
// `node --test n8n/jobs/pull.test.js` runs it on its own. It never reads the environment or the clock: the caller hands in `cfg` (base URL, keys,
// the window), `http` (n8n's this.helpers.httpRequest, or a fetch wrapper) and `sleep`, so the same code runs in n8n, in a test and in a script.
//
// `http(opts)` takes {method, url, headers, qs, body} and returns {status, headers, body} (headers lower-cased, body parsed). It must not throw on a
// non-2xx answer; `call` below decides what an answer means, retries a 429 once the vendor's own Retry-After has passed, and raises on the rest.

const MAX_RETRIES = 3;

async function call(http, sleep, opts) {
  let last;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    const res = await http(opts);
    if (res.status >= 200 && res.status < 300) return res;
    last = res;
    if (res.status === 429 || res.status >= 500) {
      const wait = Number(res.headers && res.headers['retry-after']);
      await sleep(Number.isFinite(wait) && wait > 0 ? wait * 1000 : 250 * 2 ** attempt);
      continue;
    }
    break;
  }
  const detail = typeof last.body === 'string' ? last.body : JSON.stringify(last.body || '');
  throw new Error(`${opts.method || 'GET'} ${opts.url} answered ${last.status}: ${detail.slice(0, 200)}`);
}

const trim = (u) => String(u || '').replace(/\/+$/, '');

// ------------------------------------------------------------------ pulls: each returns the body normalize.js reads for that vendor

async function theirstack(cfg, http, sleep) {
  // POST /v1/jobs/search, Bearer auth, `limit` and `page`. A filter on how recently it was posted is mandatory (the page says so).
  const data = [];
  for (let page = 0; page < (cfg.maxPages || 10); page++) {
    const r = await call(http, sleep, { method: 'POST', url: trim(cfg.baseUrl) + '/v1/jobs/search', headers: { Authorization: 'Bearer ' + cfg.apiKey },
      body: { posted_at_max_age_days: cfg.windowDays || 1, job_title_or: cfg.titles, limit: cfg.pageSize || 25, page } });
    const rows = (r.body && r.body.data) || [];
    data.push(...rows);
    if (rows.length < (cfg.pageSize || 25)) break;
  }
  return { data };
}

async function hirebase(cfg, http, sleep) {
  // POST /v2/jobs/search, x-api-key header, `page` from 1 and `limit` up to 100; `ingested_after` is the poll filter that does not return old jobs.
  const jobs = [];
  for (let page = 1; page <= (cfg.maxPages || 10); page++) {
    const r = await call(http, sleep, { method: 'POST', url: trim(cfg.baseUrl) + '/v2/jobs/search', headers: { 'x-api-key': cfg.apiKey, 'Content-Type': 'application/json' },
      body: { job_titles: cfg.titles, ingested_after: cfg.since, page, limit: cfg.pageSize || 100 } });
    const rows = (r.body && r.body.jobs) || [];
    jobs.push(...rows);
    if (rows.length < (cfg.pageSize || 100)) break;
  }
  return { jobs };
}

async function crustdata(cfg, http, sleep) {
  // POST /job/search, Bearer auth and the x-api-version header, an opaque `cursor` answered as `next_cursor` (null at the end).
  const job_listings = [];
  let cursor;
  for (let i = 0; i < (cfg.maxPages || 10); i++) {
    const r = await call(http, sleep, { method: 'POST', url: trim(cfg.baseUrl) + '/job/search', headers: { Authorization: 'Bearer ' + cfg.apiKey, 'x-api-version': '2025-11-01' },
      body: { filters: { field: 'metadata.date_added', type: '>=', value: cfg.since }, fields: ['crustdata_job_id', 'job_details.title', 'job_details.url', 'company.basic_info.name',
        'location.raw', 'metadata.date_added'], sorts: [{ field: 'metadata.date_added', order: 'desc' }], limit: cfg.pageSize || 100, ...(cursor ? { cursor } : {}) } });
    job_listings.push(...((r.body && r.body.job_listings) || []));
    cursor = r.body && r.body.next_cursor;
    if (!cursor) break;
  }
  return { job_listings };
}

async function coresignal(cfg, http, sleep) {
  // POST /cdapi/v2/job_multi_source/search/es_dsl answers job ids and the header x-next-page-after; GET .../collect/{id} answers each record. apikey header.
  const out = [];
  let after;
  for (let i = 0; i < (cfg.maxPages || 5) && out.length < (cfg.maxJobs || 200); i++) {
    const r = await call(http, sleep, { method: 'POST', url: trim(cfg.baseUrl) + '/cdapi/v2/job_multi_source/search/es_dsl', headers: { apikey: cfg.apiKey, accept: 'application/json' },
      qs: after ? { after } : undefined, body: { query: { bool: { must: [{ match: { title: cfg.titles && cfg.titles[0] } }, { term: { application_active: 1 } }] } }, sort: ['id'] } });
    const ids = Array.isArray(r.body) ? r.body : [];
    for (const id of ids.slice(0, (cfg.maxJobs || 200) - out.length)) {
      const rec = await call(http, sleep, { method: 'GET', url: trim(cfg.baseUrl) + '/cdapi/v2/job_multi_source/collect/' + id, headers: { apikey: cfg.apiKey, accept: 'application/json' } });
      out.push(rec.body);
    }
    after = r.headers && r.headers['x-next-page-after'];
    if (!after || ids.length === 0) break;
  }
  return out;
}

async function jobdataapi(cfg, http, sleep) {
  // GET /api/jobs/ with `Authorization: Api-Key`, `page_size` and `max_age` in days; the answer carries the full `next` address until the last page.
  const results = [];
  let url = trim(cfg.baseUrl) + '/api/jobs/';
  let qs = { max_age: cfg.windowDays || 1, page_size: cfg.pageSize || 200, title: cfg.titles && cfg.titles[0] };
  for (let i = 0; i < (cfg.maxPages || 10) && url; i++) {
    const r = await call(http, sleep, { method: 'GET', url, headers: { Authorization: 'Api-Key ' + cfg.apiKey }, qs });
    results.push(...((r.body && r.body.results) || []));
    url = r.body && r.body.next;
    qs = undefined;
  }
  return { results };
}

async function linkdapi(cfg, http, sleep) {
  // GET /api/v1/jobs/search with `keyword`, `start` stepping by 25, header X-linkdapi-apikey.
  const jobs = [];
  for (let start = 0, i = 0; i < (cfg.maxPages || 5); start += 25, i++) {
    const r = await call(http, sleep, { method: 'GET', url: trim(cfg.baseUrl) + '/api/v1/jobs/search', headers: { 'X-linkdapi-apikey': cfg.apiKey },
      qs: { keyword: cfg.titles && cfg.titles[0], timePosted: '24h', start } });
    const rows = (r.body && r.body.data && r.body.data.jobs) || [];
    jobs.push(...rows);
    if (rows.length < 25) break;
  }
  return { data: { jobs } };
}

async function techmap(cfg, http, sleep) {
  // GET /api/v2/jobs/search on the RapidAPI host: headers X-RapidAPI-Key and X-RapidAPI-Host, query filters (title, countryCode, dateCreated), and for a complete stable
  // paging `cursor=true` on the first request, then `cursor=<nextCursor>` until nextCursor is missing (the pagination page). `result` holds the postings.
  const result = [];
  let cursor = 'true';
  for (let i = 0; i < (cfg.maxPages || 10) && cursor; i++) {
    const r = await call(http, sleep, { method: 'GET', url: trim(cfg.baseUrl) + '/api/v2/jobs/search',
      headers: { 'X-RapidAPI-Key': cfg.apiKey, 'X-RapidAPI-Host': cfg.host || 'daily-international-job-postings.p.rapidapi.com' },
      qs: { title: cfg.titles && cfg.titles[0], dateCreated: cfg.dateCreated, countryCode: cfg.countryCode, workPlace: cfg.workPlace, cursor } });
    result.push(...((r.body && r.body.result) || []));
    cursor = r.body && r.body.nextCursor;
  }
  return { result };
}

async function predictleads(cfg, http, sleep) {
  // GET /api/v3/companies/{domain}/job_openings, headers X-Api-Key and X-Api-Token, JSON:API: `data`, `included`, `links.next`.
  const data = [];
  const included = [];
  for (const domain of cfg.domains || []) {
    let url = trim(cfg.baseUrl) + '/api/v3/companies/' + encodeURIComponent(domain) + '/job_openings';
    let qs = { limit: cfg.pageSize || 100, with_job_title_only: true };
    for (let i = 0; i < (cfg.maxPages || 5) && url; i++) {
      const r = await call(http, sleep, { method: 'GET', url, headers: { 'X-Api-Key': cfg.apiKey, 'X-Api-Token': cfg.apiToken }, qs });
      data.push(...((r.body && r.body.data) || []));
      included.push(...((r.body && r.body.included) || []));
      url = r.body && r.body.links && r.body.links.next;
      qs = undefined;
    }
  }
  return { data, included };
}

async function brightdata(cfg, http, sleep) {
  // Scraper API, asynchronous: POST /datasets/v3/trigger answers a snapshot_id, GET /datasets/v3/progress/{id} answers a status until it is `ready`
  // (and a status of failed or canceled is an error), GET /datasets/v3/snapshot/{id}?format=json answers the records.
  const auth = { Authorization: 'Bearer ' + cfg.apiKey };
  const t = await call(http, sleep, { method: 'POST', url: trim(cfg.baseUrl) + '/datasets/v3/trigger', headers: auth,
    qs: { dataset_id: cfg.datasetId || 'gd_lpfll7v5hcqtkxl6l', include_errors: 'true', type: 'discover_new', discover_by: 'keyword' },
    body: { input: [{ keyword: cfg.titles && cfg.titles[0], location: cfg.location || 'Remote', time_range: 'Past 24 hours' }] } });
  const id = t.body && t.body.snapshot_id;
  if (!id) throw new Error('Bright Data answered no snapshot_id');
  for (let i = 0; i < (cfg.maxPolls || 40); i++) {
    const p = await call(http, sleep, { method: 'GET', url: trim(cfg.baseUrl) + '/datasets/v3/progress/' + id, headers: auth });
    const status = p.body && p.body.status;
    if (status === 'ready') {
      const s = await call(http, sleep, { method: 'GET', url: trim(cfg.baseUrl) + '/datasets/v3/snapshot/' + id, headers: auth, qs: { format: 'json' } });
      return Array.isArray(s.body) ? s.body : [];
    }
    if (status === 'failed' || status === 'canceled') throw new Error('Bright Data snapshot ' + id + ' ' + status);
    await sleep(cfg.pollMs || 1000);
  }
  throw new Error('Bright Data snapshot ' + id + ' was not ready in time');
}

async function apify(cfg, http, sleep) {
  // POST /v2/acts/{actorId}/run-sync-get-dataset-items runs the actor and answers its dataset items in one call (Bearer auth). The input is the actor's own.
  const r = await call(http, sleep, { method: 'POST', url: trim(cfg.baseUrl) + '/v2/acts/' + encodeURIComponent(cfg.actorId) + '/run-sync-get-dataset-items',
    headers: { Authorization: 'Bearer ' + cfg.apiKey }, body: cfg.actorInput || { queries: cfg.titles, maxItems: cfg.pageSize || 100 } });
  return Array.isArray(r.body) ? r.body : [];
}

const PULLERS = { theirstack, hirebase, crustdata, coresignal, jobdataapi, linkdapi, predictleads, techmap, brightdata, apify };

async function pullVendor(vendor, cfg, http, sleep) {
  if (!PULLERS[vendor]) throw new Error('no pull is written for ' + vendor + ' (a delivery vendor pushes to a webhook instead)');
  const body = await PULLERS[vendor](cfg, http, sleep || ((ms) => new Promise((r) => setTimeout(r, ms))));
  return body;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { pullVendor, PULLERS, call };
}
