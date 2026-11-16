// LIFESPAN: permanent
// Job normalisation and ATS detection for the job pipeline. One pure file, two homes: the n8n Code node in
// job_pipeline.workflow.json carries a copy of it (build_job_workflow.py inlines this file, so they cannot drift), and
// `node --test n8n/jobs/normalize.test.js` runs it on its own. It does no I/O and takes no clock, so the same record
// gives the same answer on every run.
//
// Field names come from each vendor's own documentation page (docs/job-vendors.md names the page for each). Where a
// vendor's output depends on the scraper or actor behind it (Apify, Bright Data's other datasets) the mapper reads a
// list of documented aliases, and a record it cannot read a title or a link from is returned with `valid: false`
// instead of being guessed at.

const ATS_URL_RULES = [
  ['greenhouse', /(?:boards|job-boards|boards-api)\.(?:eu\.)?greenhouse\.io\/(?:v1\/boards\/)?([^/?#]+)\/jobs\/(\d+)/i],
  ['greenhouse', /[?&]gh_jid=(\d+)/i],
  ['lever', /jobs\.(?:eu\.)?lever\.co\/([^/?#]+)\/([0-9a-f-]{36})/i],
  ['workday', /([a-z0-9-]+)\.wd\d+\.myworkday(?:jobs|site)\.com\/(?:[a-z]{2}-[A-Z]{2}\/)?[^?#]*?_((?:JR|R|REQ)[-_]?\d+[A-Za-z0-9-]*)/i],
  ['workday', /([a-z0-9-]+)\.wd\d+\.myworkday(?:jobs|site)\.com\//i],
  ['ashby', /jobs\.ashbyhq\.com\/([^/?#]+)\/([0-9a-f-]{36})/i],
  ['smartrecruiters', /(?:jobs|careers)\.smartrecruiters\.com\/([^/?#]+)\/(\d+)/i],
  ['workable', /apply\.workable\.com\/([^/?#]+)\/j\/([A-Z0-9]+)/i],
  ['icims', /([a-z0-9-]+)\.icims\.com\/jobs\/(\d+)/i],
  ['bamboohr', /([a-z0-9-]+)\.bamboohr\.com\/(?:careers|jobs\/view\.php\?id=)\/?(\d+)/i],
  ['jobvite', /jobs\.jobvite\.com\/([^/?#]+)\/job\/([A-Za-z0-9]+)/i],
  ['taleo', /([a-z0-9-]+)\.taleo\.net\/.*?[?&]job=([A-Za-z0-9]+)/i],
  ['recruitee', /([a-z0-9-]+)\.recruitee\.com\/o\/([a-z0-9-]+)/i],
  ['teamtailor', /([a-z0-9-]+)\.teamtailor\.com\/jobs\/(\d+)/i],
  ['breezy', /([a-z0-9-]+)\.breezy\.hr\/p\/([0-9a-f]+)/i],
  ['personio', /([a-z0-9-]+)\.jobs\.personio\.(?:de|com)\/job\/(\d+)/i],
  ['successfactors', /career\d*\.successfactors\.(?:com|eu)\/.*?[?&]jobId=(\d+)/i],
  ['oracle_cloud', /([a-z0-9-]+)\.fa\.[a-z0-9-]+\.oraclecloud\.com\/hcmUI\/CandidateExperience\/[^/]+\/sites\/[^/]+\/job\/(\d+)/i],
];

// What a rendered application page gives away when the link itself does not (a company's own careers page that embeds the form).
const ATS_HTML_MARKERS = [
  ['greenhouse', /id=["']grnhse_app["']|boards\.greenhouse\.io\/embed|job-boards\.greenhouse\.io/i],
  ['lever', /lever-jobs-embed|jobs\.lever\.co|class=["'][^"']*lever-application/i],
  ['workday', /myworkdayjobs\.com|data-automation-id=["']jobPostingPage["']|wd-application/i],
  ['ashby', /ashbyhq\.com|ashby_embed|data-ashby/i],
  ['smartrecruiters', /smartrecruiters\.com|data-qa=["']sr-/i],
  ['workable', /workable\.com|data-ui=["']application-form["']/i],
  ['icims', /icims\.com|iCIMS_/i],
  ['bamboohr', /bamboohr\.com\/careers|BambooHR/i],
];

function detectAts(url, html) {
  const out = { ats: null, org: null, ats_job_id: null, via: null };
  const u = typeof url === 'string' ? url : '';
  for (const [name, re] of ATS_URL_RULES) {
    const m = u.match(re);
    if (m) {
      out.ats = name;
      out.via = 'url';
      // a rule with two groups is (organisation, job id); a one group rule is the job id (gh_jid) or the organisation (workday)
      if (m.length >= 3) { out.org = m[1]; out.ats_job_id = m[2]; }
      else if (re.source.includes('gh_jid')) out.ats_job_id = m[1];
      else out.org = m[1];
      return out;
    }
  }
  if (typeof html === 'string' && html) {
    for (const [name, re] of ATS_HTML_MARKERS) {
      if (re.test(html)) { out.ats = name; out.via = 'page'; return out; }
    }
  }
  return out;
}

const first = (...vals) => { for (const v of vals) if (v !== undefined && v !== null && v !== '') return v; return null; };
const text = (v) => ((v === null || v === undefined) ? null : (String(v).replace(/\s+/g, ' ').trim() || null));
const num = (v) => {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(String(v).replace(/[^0-9.\-]/g, ''));
  return Number.isFinite(n) ? n : null;
};
const iso = (v) => { if (!v) return null; const d = new Date(v); return Number.isNaN(d.getTime()) ? null : d.toISOString(); };
// An address is split with a pattern, never with `new URL`: n8n's Code node runs in a sandbox where URL is not defined, a try/catch around it made that a silent null,
// and the dedupe keys of the same job from two vendors stopped matching only inside n8n (found by the n8n run, 32 keys for 30 jobs).
const splitUrl = (v) => {
  const m = typeof v === 'string' ? v.trim().match(/^(?:https?:)?\/\/([^\/?#:]+)(?::\d+)?([^?#]*)/i) : null;
  return m ? { host: m[1].replace(/^www\./i, '').toLowerCase(), path: m[2] || '' } : null;
};
const domainOf = (v) => {
  if (!v) return null;
  const u = splitUrl(/^(https?:)?\/\//i.test(v) ? v : 'https://' + v);
  return u ? u.host : null;
};
const BOARDS = /(^|\.)(linkedin|indeed|glassdoor|ziprecruiter|monster|dice|wellfound|simplyhired|jooble|adzuna|careerbuilder|talent|jobstreet|seek|bayt|naukri|google)\.[a-z.]+$/i;
// host and path of an address, lower case, no www, no trailing slash, no query or fragment; null for a job board, which does not identify the employer's own posting
const ownAddress = (v) => {
  const u = splitUrl(v);
  if (!u || BOARDS.test(u.host)) return null;
  return u.host + u.path.replace(/\/+$/, '').toLowerCase();
};
const stripHtml =(s) => text(typeof s === 'string' ? s.replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&') : s);

// The shape every vendor is mapped onto. `dedupe_key` is the part the hub hashes: the ATS's own id when there is one (so the same
// job from two vendors lands on one key), otherwise a stable company, a title with the noise taken out, and a place.
function canonical(vendor, id, f) {
  let ats = detectAts(first(f.apply_url, f.url));
  if (!ats.ats) ats = detectAts(f.url);
  const title = text(f.title);
  const company = text(f.company_name);
  const companyDomain = f.company_domain ? domainOf(f.company_domain) : null;
  const location = text(f.location);
  const norm = (s) => (s || '').toLowerCase().replace(/\(.*?\)|\[.*?\]/g, ' ').replace(/[^a-z0-9]+/g, ' ').trim();
  // the company NAME, not the domain: every vendor sends a name, but only some send a site, and a key that used the site when it was there would split one job in two
  const who = norm(company).replace(/ (inc|llc|ltd|limited|corp|corporation|co|gmbh|plc|the)$/g, '').trim() || companyDomain || '';
  const applyUrl = first(f.apply_url, f.url);
  // Key by, in order: the ATS's own job id; the employer's own posting address (vendors disagree on how to write a place, never on where the posting lives);
  // and last the company, the title and the place. A job board or aggregator address says nothing about the employer's posting, so it is skipped.
  const own = ownAddress(applyUrl);
  const keyParts = ats.ats_job_id ? [ats.ats, ats.org || who, ats.ats_job_id] : (own ? ['url', who, own] : [who, norm(title), norm(location)]);
  return {
    vendor,
    vendor_job_id: id === null || id === undefined ? null : String(id),
    title,
    company_name: company,
    company_domain: companyDomain,
    location,
    remote: f.remote === undefined ? null : f.remote,
    url: text(f.url),
    apply_url: text(applyUrl),
    ats: ats.ats,
    ats_org: ats.org,
    ats_job_id: ats.ats_job_id,
    posted_at: iso(f.posted_at),
    description: stripHtml(f.description),
    salary_min: num(f.salary_min),
    salary_max: num(f.salary_max),
    salary_currency: text(f.salary_currency),
    salary_period: text(f.salary_period),
    employment_type: text(Array.isArray(f.employment_type) ? f.employment_type[0] : f.employment_type),
    technologies: Array.isArray(f.technologies) ? f.technologies.map(String).slice(0, 40) : [],
    dedupe_key: keyParts.join('|'),
    valid: Boolean(title && (applyUrl || company)),
  };
}

const path = (o, p) => p.split('.').reduce((a, k) => (a === null || a === undefined ? undefined : a[k]), o);

const MAPPERS = {
  // POST /v1/jobs/search, TheirStack. `final_url` is the ATS link when TheirStack found one, `url` the board it saw it on.
  theirstack: (r) => canonical('theirstack', r.id, {
    title: r.job_title, company_name: r.company, company_domain: first(r.company_domain, path(r, 'company_object.domain')),
    location: first(r.location, r.short_location), remote: r.remote, url: first(r.url, r.source_url), apply_url: r.final_url,
    posted_at: r.date_posted, description: r.description, salary_min: r.min_annual_salary_usd, salary_max: r.max_annual_salary_usd,
    salary_currency: r.salary_currency, salary_period: 'year', employment_type: r.employment_statuses, technologies: r.technology_slugs,
  }),
  // POST /v2/jobs/search, Hirebase (x-api-key). `application_link` is the employer's own page.
  hirebase: (r) => canonical('hirebase', first(r._id, r.platform_job_id), {
    title: r.job_title, company_name: r.company_name, company_domain: null, location: r.location_raw,
    remote: r.location_type ? /remote/i.test(r.location_type) : null, url: first(r.job_board_link, r.application_link), apply_url: r.application_link,
    posted_at: first(r.date_posted, r.first_ingested), description: r.description,
    salary_min: path(r, 'salary_range.min'), salary_max: path(r, 'salary_range.max'),
    salary_currency: path(r, 'salary_range.currency'), salary_period: path(r, 'salary_range.period'),
    employment_type: r.job_type, technologies: r.technologies,
  }),
  // POST /job/search, Crustdata (fields chosen with `fields`, so the dotted paths are the request's own).
  crustdata: (r) => canonical('crustdata', r.crustdata_job_id, {
    title: path(r, 'job_details.title'), company_name: path(r, 'company.basic_info.name'),
    company_domain: path(r, 'company.basic_info.primary_domain'),
    location: path(r, 'location.raw'), url: path(r, 'job_details.url'),
    remote: path(r, 'job_details.workplace_type') ? /remote/i.test(path(r, 'job_details.workplace_type')) : null,
    posted_at: path(r, 'metadata.date_added'), description: path(r, 'job_details.description'),
  }),
  // POST /cdapi/v2/job_multi_source/search/es_dsl gives ids; GET .../collect/{id} gives this record. `job_sources[].url` are the boards.
  coresignal: (r) => canonical('coresignal', first(r.job_id, r.id), {
    title: r.title, company_name: r.company_name, company_domain: first(r.company_domain, r.company_url), location: r.location,
    remote: typeof r.accepts_remote === 'boolean' ? r.accepts_remote : null,
    url: first(r.external_url, path(r, 'job_sources.0.url')), apply_url: r.external_url,
    posted_at: first(r.date_posted, r.created_at), description: r.description,
    salary_min: path(r, 'salary.0.amount_min'), salary_max: path(r, 'salary.0.amount_max'),
    salary_currency: path(r, 'salary.0.currency'), employment_type: r.employment_type,
  }),
  // GET /api/v1/jobs/search then /api/v1/jobs/job/details, LinkdAPI (X-linkdapi-apikey).
  linkdapi: (r) => canonical('linkdapi', first(r.jobId, r.id), {
    title: r.title, company_name: path(r, 'company.name'), company_domain: null, location: r.location,
    url: first(r.url, r.jobId ? 'https://www.linkedin.com/jobs/view/' + r.jobId : null), apply_url: r.applyUrl,
    posted_at: r.postedAt, description: r.description,
  }),
  // Scraper API dataset gd_lpfll7v5hcqtkxl6l (LinkedIn job listings), output fields from the scraper's own schema page.
  brightdata: (r) => canonical('brightdata', first(r.job_posting_id, r.url), {
    title: r.job_title, company_name: r.company_name, location: r.job_location, url: r.url, apply_url: r.apply_link,
    posted_at: first(r.job_posted_date, r.job_posted_time), description: first(r.job_summary, r.job_description_formatted),
    salary_min: path(r, 'base_salary.min_amount'), salary_max: path(r, 'base_salary.max_amount'),
    salary_currency: path(r, 'base_salary.currency'), salary_period: path(r, 'base_salary.payment_period'),
    employment_type: r.job_employment_type,
  }),
  // Apify actors: the dataset items belong to the actor, so the mapper reads the names the common job actors use.
  apify: (r) => canonical('apify', first(r.id, r.jobId, r.url, r.link), {
    title: first(r.title, r.positionName, r.jobTitle, r.name), company_name: first(r.companyName, r.company, r.employer),
    company_domain: first(r.companyWebsite, r.companyUrl), location: first(r.location, r.jobLocation),
    url: first(r.url, r.jobUrl, r.link), apply_url: first(r.applyUrl, r.externalApplyLink, r.applyLink),
    posted_at: first(r.postedAt, r.datePosted, r.postingDate, r.publishedAt), description: first(r.description, r.descriptionText, r.jobDescription),
    salary_min: r.salaryMin, salary_max: r.salaryMax, salary_currency: r.salaryCurrency, employment_type: first(r.employmentType, r.jobType),
  }),
  // GET /api/v2/jobs/search, Techmap (through RapidAPI): `result` array; the posting also rides in `jsonLD` (schema.org JobPosting) with the full description.
  techmap: (r) => canonical('techmap', first(path(r, 'jsonLD.identifier'), r.id), {
    title: first(r.title, r.occupation), company_name: r.company, location: [r.city, r.countryCode && String(r.countryCode).toUpperCase()].filter(Boolean).join(', '),
    remote: Array.isArray(r.workPlace) ? r.workPlace.some((w) => /remote/i.test(w)) : null, url: first(path(r, 'jsonLD.sameAs'), path(r, 'jsonLD.url')),
    apply_url: first(r.externalApplyUrl, path(r, 'jsonLD.url')), posted_at: first(r.dateCreated, path(r, 'jsonLD.datePosted')), description: path(r, 'jsonLD.description'),
    salary_min: r.minSalary, salary_max: r.maxSalary, salary_currency: path(r, 'jsonLD.salaryCurrency'), employment_type: first(r.workType, r.contractType), technologies: r.skills,
  }),
  // GET /api/jobs/, JobDataAPI (Authorization: Api-Key). `published` is a date, `application_url` the employer's link.
  jobdataapi: (r) => canonical('jobdataapi', first(r.id, r.ext_id), {
    title: r.title, company_name: path(r, 'company.name'), company_domain: path(r, 'company.website_url'), location: r.location,
    remote: r.has_remote, url: r.application_url, apply_url: r.application_url, posted_at: r.published, description: r.description,
    salary_min: r.salary_min, salary_max: r.salary_max, salary_currency: r.salary_currency, employment_type: r.types,
  }),
  // GET /api/v3/companies/{domain}/job_openings, PredictLeads (JSON:API: `data[].attributes`, `included` holds the company).
  predictleads: (r, included) => {
    const a = r.attributes || {};
    const rel = path(r, 'relationships.company.data');
    const co = rel && (included || []).find((i) => i.type === 'company' && i.id === rel.id);
    return canonical('predictleads', r.id, {
      title: a.title, company_name: co && path(co, 'attributes.company_name'), company_domain: co && path(co, 'attributes.domain'),
      location: a.location, url: a.url, apply_url: a.url, posted_at: first(a.posted_at, a.first_seen_at), description: a.description,
      salary_min: path(a, 'salary_data.salary_low'), salary_max: path(a, 'salary_data.salary_high'),
      salary_currency: path(a, 'salary_data.salary_currency'), salary_period: path(a, 'salary_data.salary_time_unit'),
      employment_type: a.contract_types,
    });
  },
};

// Files and webhooks from a delivery vendor (Grepsr runs, Techmap data feed files): the column names are the customer's own, so the
// mapping is data. `map` is {canonicalField: sourceField}; a missing entry falls back to the common names.
function mapDelivered(vendor, r, map) {
  const m = map || {};
  const get = (k, ...alts) => first(...[m[k], ...alts].filter(Boolean).map((n) => r[n]));
  return canonical(vendor, get('id', 'id', 'job_id', 'url'), {
    title: get('title', 'title', 'job_title', 'position'), company_name: get('company_name', 'company', 'company_name', 'employer'),
    company_domain: get('company_domain', 'company_domain', 'company_website'), location: get('location', 'location', 'job_location'),
    url: get('url', 'url', 'link', 'job_url'), apply_url: get('apply_url', 'apply_url', 'apply_link'),
    posted_at: get('posted_at', 'posted_at', 'date_posted', 'published'),
    description: get('description', 'description', 'job_description'), salary_min: get('salary_min', 'salary_min'), salary_max: get('salary_max', 'salary_max'),
    salary_currency: get('salary_currency', 'salary_currency', 'currency'), employment_type: get('employment_type', 'employment_type', 'job_type'),
  });
}

// Pull the list of records out of each vendor's response envelope.
const ENVELOPES = {
  theirstack: (b) => b.data || [],
  hirebase: (b) => b.jobs || [],
  crustdata: (b) => b.job_listings || [],
  coresignal: (b) => (Array.isArray(b) ? b : [b]),
  linkdapi: (b) => (b.data && b.data.jobs) || [],
  brightdata: (b) => (Array.isArray(b) ? b : []),
  apify: (b) => (Array.isArray(b) ? b : []),
  jobdataapi: (b) => b.results || [],
  techmap: (b) => b.result || [],
  predictleads: (b) => b.data || [],
};

function normalizeBatch(vendor, body, opts) {
  const o = opts || {};
  let records;
  if (ENVELOPES[vendor]) records = ENVELOPES[vendor](body || {});
  else records = Array.isArray(body) ? body : ((body && body.items) || []);
  const jobs = records.map((rec) => {
    if (MAPPERS[vendor]) return MAPPERS[vendor](rec, body && body.included);
    return mapDelivered(vendor, rec, o.map);
  });
  return { vendor, received: records.length, jobs: jobs.filter((j) => j.valid), invalid: jobs.filter((j) => !j.valid).length };
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { detectAts, normalizeBatch, MAPPERS, ENVELOPES, ATS_URL_RULES, mapDelivered };
}
