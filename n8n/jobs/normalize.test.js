// Run: node --test n8n/jobs/normalize.test.js
const test = require('node:test');
const assert = require('node:assert');
const { detectAts, normalizeBatch } = require('./normalize.js');

test('ATS detection by link, one case per platform with its organisation and job id', () => {
  const cases = [
    ['https://job-boards.greenhouse.io/acme/jobs/4012345', 'greenhouse', 'acme', '4012345'],
    ['https://boards.greenhouse.io/acme/jobs/77', 'greenhouse', 'acme', '77'],
    ['https://jobs.lever.co/acme/5a3b6c7d-1111-4222-8333-944455556666', 'lever', 'acme', '5a3b6c7d-1111-4222-8333-944455556666'],
    ['https://acme.wd5.myworkdayjobs.com/en-US/External/job/Remote/Data-Engineer_JR-10442', 'workday', 'acme', 'JR-10442'],
    ['https://jobs.ashbyhq.com/acme/0c1d2e3f-aaaa-4bbb-8ccc-ddddeeeeffff', 'ashby', 'acme', '0c1d2e3f-aaaa-4bbb-8ccc-ddddeeeeffff'],
    ['https://jobs.smartrecruiters.com/Acme/743999', 'smartrecruiters', 'Acme', '743999'],
    ['https://apply.workable.com/acme/j/ABC123DEF4/', 'workable', 'acme', 'ABC123DEF4'],
    ['https://acme.icims.com/jobs/2210/engineer/job', 'icims', 'acme', '2210'],
    ['https://acme.bamboohr.com/careers/45', 'bamboohr', 'acme', '45'],
    ['https://jobs.jobvite.com/acme/job/oXyz12', 'jobvite', 'acme', 'oXyz12'],
    ['https://acme.recruitee.com/o/senior-backend', 'recruitee', 'acme', 'senior-backend'],
    ['https://acme.teamtailor.com/jobs/5551-backend', 'teamtailor', 'acme', '5551'],
    ['https://acme.jobs.personio.de/job/88', 'personio', 'acme', '88'],
  ];
  for (const [url, ats, org, id] of cases) {
    const got = detectAts(url);
    assert.strictEqual(got.ats, ats, url);
    assert.strictEqual(got.org, org, url);
    assert.strictEqual(got.ats_job_id, id, url);
  }
});

test('a company page that only carries a gh_jid parameter is still Greenhouse, with the id', () => {
  const got = detectAts('https://acme.com/careers/opening?gh_jid=998877');
  assert.deepStrictEqual([got.ats, got.ats_job_id, got.org], ['greenhouse', '998877', null]);
});

test('a link that is no ATS gives nothing, and the page markers can still name one', () => {
  assert.strictEqual(detectAts('https://acme.com/careers/42').ats, null);
  assert.strictEqual(detectAts('https://acme.com/careers/42', '<div id="grnhse_app"></div>').ats, 'greenhouse');
  assert.strictEqual(detectAts('https://acme.com/c', '<script src="https://jobs.lever.co/embed.js"></script>').ats, 'lever');
  assert.strictEqual(detectAts(null).ats, null);
});

test('a lookalike host is not an ATS', () => {
  assert.strictEqual(detectAts('https://greenhouse.io.evil.example/acme/jobs/1').ats, null);
  assert.strictEqual(detectAts('https://notlever.co/acme/5a3b6c7d-1111-4222-8333-944455556666').ats, null);
});

const THEIRSTACK = { data: [{
  id: 5367315, job_title: 'Senior Data Engineer', company: 'Acme', company_domain: 'acme.com', location: 'Remote, US', short_location: 'Remote',
  remote: true, url: 'https://www.linkedin.com/jobs/view/1234567890', source_url: 'https://www.linkedin.com/jobs/view/1234567890',
  final_url: 'https://job-boards.greenhouse.io/acme/jobs/4012345', date_posted: '2026-11-10', description: '<p>Build pipelines</p>',
  min_annual_salary_usd: 100000, max_annual_salary_usd: 120000, salary_currency: 'USD', employment_statuses: ['full_time'],
  technology_slugs: ['postgresql', 'kafka'] }] };

test('TheirStack: the ATS link beats the board link and the record is complete', () => {
  const out = normalizeBatch('theirstack', THEIRSTACK);
  assert.strictEqual(out.jobs.length, 1);
  const j = out.jobs[0];
  assert.strictEqual(j.ats, 'greenhouse');
  assert.strictEqual(j.ats_job_id, '4012345');
  assert.strictEqual(j.apply_url, 'https://job-boards.greenhouse.io/acme/jobs/4012345');
  assert.strictEqual(j.description, 'Build pipelines');
  assert.strictEqual(j.salary_min, 100000);
  assert.strictEqual(j.posted_at, '2026-11-10T00:00:00.000Z');
  assert.strictEqual(j.dedupe_key, 'greenhouse|acme|4012345');
});

test('the same job from Hirebase gives the same dedupe key as from TheirStack', () => {
  const hb = normalizeBatch('hirebase', { jobs: [{ _id: 'abc', job_title: 'Senior Data Engineer', company_name: 'Acme', location_raw: 'Remote',
    location_type: 'Remote', application_link: 'https://job-boards.greenhouse.io/acme/jobs/4012345', date_posted: '2026-11-11',
    salary_range: { min: 100000, max: 120000, currency: 'USD', period: 'yearly' }, job_type: 'Full-time', technologies: ['Kafka'] }] });
  const ts = normalizeBatch('theirstack', THEIRSTACK);
  assert.strictEqual(hb.jobs[0].dedupe_key, ts.jobs[0].dedupe_key);
  assert.strictEqual(hb.jobs[0].remote, true);
});

test('without an ATS id the key is the employer own posting address, and company, title and place only when there is none', () => {
  const a = normalizeBatch('jobdataapi', { results: [{ id: 1, title: 'Backend Engineer (Remote)', company: { name: 'Acme Inc', website_url: 'https://www.acme.com' },
    location: 'Berlin, Germany', has_remote: true, application_url: 'https://acme.com/careers/1/?utm_source=x#top', published: '2026-11-01' }] });
  const b = normalizeBatch('apify', [{ id: 'x', positionName: 'Backend engineer', companyName: 'ACME', companyWebsite: 'acme.com', location: 'berlin', url: 'https://www.acme.com/careers/1' }]);
  assert.strictEqual(a.jobs[0].dedupe_key, 'url|acme|acme.com/careers/1');
  assert.strictEqual(b.jobs[0].dedupe_key, a.jobs[0].dedupe_key, 'another vendor, another way of writing the place and the title, the same posting');
  // a job board address identifies nothing of the employer's own posting: the key falls back to company, title and place
  const c = normalizeBatch('apify', [{ id: 'y', positionName: 'Backend  engineer', companyName: 'Acme Inc', location: 'berlin', url: 'https://www.linkedin.com/jobs/view/123' }]);
  const d = normalizeBatch('apify', [{ id: 'z', positionName: 'Backend Engineer (Remote)', companyName: 'Acme', location: 'Berlin', url: 'https://www.indeed.com/viewjob?jk=9' }]);
  assert.strictEqual(c.jobs[0].dedupe_key, 'acme|backend engineer|berlin');
  assert.strictEqual(d.jobs[0].dedupe_key, c.jobs[0].dedupe_key);
  // the same title in two cities is two jobs when nothing else tells them apart
  const e = normalizeBatch('apify', [{ id: 'w', positionName: 'Backend Engineer', companyName: 'Acme', location: 'Manila', url: 'https://www.linkedin.com/jobs/view/124' }]);
  assert.notStrictEqual(e.jobs[0].dedupe_key, c.jobs[0].dedupe_key);
});

test('Crustdata, CoreSignal, LinkdAPI, Bright Data and PredictLeads map their documented shapes', () => {
  const cd = normalizeBatch('crustdata', { job_listings: [{ crustdata_job_id: 9, company: { basic_info: { name: 'Stripe' } },
    job_details: { title: 'Software Engineer', url: 'https://www.linkedin.com/jobs/view/4342849906' }, location: { raw: 'Dublin' }, metadata: { date_added: '2025-12-10T15:57:16' } }] });
  assert.strictEqual(cd.jobs[0].title, 'Software Engineer');
  assert.strictEqual(cd.jobs[0].location, 'Dublin');

  const cs = normalizeBatch('coresignal', { job_id: 12345678, title: 'Backend Engineer', location: 'Akron, OH', description: 'x', external_url: null,
    job_sources: [{ url: 'https://www.indeed.com/view-job?jk=1a2b3c4d' }], company_name: 'Example Company', company_url: 'http://www.example-company.com',
    created_at: '2025-06-02T00:06:00.000', employment_type: ['Full-time'], salary: [{ amount_min: 1, amount_max: 2, currency: 'USD' }] });
  assert.strictEqual(cs.jobs[0].company_domain, 'example-company.com');
  assert.strictEqual(cs.jobs[0].apply_url, 'https://www.indeed.com/view-job?jk=1a2b3c4d');
  assert.strictEqual(cs.jobs[0].employment_type, 'Full-time');

  const ld = normalizeBatch('linkdapi', { data: { jobs: [{ jobId: '3801', title: 'ML Engineer', company: { name: 'Initech' }, location: 'Remote' }] } });
  assert.strictEqual(ld.jobs[0].url, 'https://www.linkedin.com/jobs/view/3801');

  const bd = normalizeBatch('brightdata', [{ job_posting_id: '77', job_title: 'Data Analyst', company_name: 'Globex', job_location: 'Austin',
    url: 'https://www.linkedin.com/jobs/view/77', apply_link: 'https://acme.wd5.myworkdayjobs.com/External/job/X/Analyst_R-5512', job_posted_date: '2026-11-12T00:00:00.000Z',
    base_salary: { min_amount: 80000, max_amount: 90000, currency: 'USD', payment_period: 'year' } }]);
  assert.strictEqual(bd.jobs[0].ats, 'workday');
  assert.strictEqual(bd.jobs[0].salary_max, 90000);

  const pl = normalizeBatch('predictleads', { data: [{ id: 'uuid-1', type: 'job_opening', attributes: { title: 'AI Engineer', url: 'https://www.ycombinator.com/companies/terra-api/jobs/0f5CP0r-ai-engineer',
    first_seen_at: '2025-10-17T19:11:22Z', location: 'London', salary_data: { salary_low: 50000, salary_high: 120000, salary_currency: 'USD', salary_time_unit: 'year' } },
    relationships: { company: { data: { id: 'c1', type: 'company' } } } }], included: [{ id: 'c1', type: 'company', attributes: { company_name: 'Terra', domain: 'tryterra.co' } }] });
  assert.strictEqual(pl.jobs[0].company_name, 'Terra');
  assert.strictEqual(pl.jobs[0].company_domain, 'tryterra.co');
});

test('a delivered file (Grepsr or a data feed) maps through the customer column names', () => {
  const out = normalizeBatch('grepsr', [{ position: 'Support Lead', org: 'Initech', city_state: 'Manila', link: 'https://initech.com/j/1', day: '2026-11-02' }],
    { map: { title: 'position', company_name: 'org', location: 'city_state', url: 'link', posted_at: 'day' } });
  assert.strictEqual(out.jobs[0].title, 'Support Lead');
  assert.strictEqual(out.jobs[0].company_name, 'Initech');
  assert.strictEqual(out.jobs[0].posted_at, '2026-11-02T00:00:00.000Z');
});

test('a record with no title or no link and no company is counted invalid and left out, never guessed', () => {
  const out = normalizeBatch('hirebase', { jobs: [{ _id: '1', job_title: null, application_link: null }, { _id: '2', job_title: 'Ok', company_name: 'A', application_link: 'https://a.com/j/2' }] });
  assert.strictEqual(out.received, 2);
  assert.strictEqual(out.invalid, 1);
  assert.strictEqual(out.jobs.length, 1);
});

test('a record with a link and a company but no title is invalid', () => {
  const out = normalizeBatch('hirebase', { jobs: [{ _id: '3', job_title: '', company_name: 'Acme', application_link: 'https://acme.com/careers/3' }] });
  assert.strictEqual(out.jobs.length, 0);
  assert.strictEqual(out.invalid, 1);
});

test('hostile text in a record stays text: no markup survives and nothing throws on odd input', () => {
  const out = normalizeBatch('apify', [{ title: 'T', companyName: 'C', url: 'https://a.com/x', description: '<img src=x onerror=alert(1)>hello &amp; bye', postedAt: 'not a date' }, null === 1 ? 1 : {}]);
  assert.strictEqual(out.jobs[0].description, 'hello & bye');
  assert.strictEqual(out.jobs[0].posted_at, null);
  assert.strictEqual(out.invalid, 1);
});

test('the normaliser gives the same answers in a sandbox with no URL global, which is where n8n runs it', () => {
  const vm = require('node:vm');
  const fs = require('node:fs');
  const ctx = { module: { exports: {} } };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(require('node:path').join(__dirname, 'normalize.js'), 'utf8'), ctx);
  assert.strictEqual(vm.runInContext('typeof URL', ctx), 'undefined', 'the sandbox must not have URL');
  const rec = [{ id: 'x', positionName: 'Backend engineer', companyName: 'ACME', companyWebsite: 'https://www.acme.com/', location: 'berlin', url: 'https://www.acme.com/careers/1/?utm=1' }];
  const inside = JSON.parse(JSON.stringify(ctx.module.exports.normalizeBatch('apify', rec)));
  const outside = JSON.parse(JSON.stringify(normalizeBatch('apify', rec)));
  assert.deepStrictEqual(inside, outside);
  assert.strictEqual(inside.jobs[0].dedupe_key, 'url|acme|acme.com/careers/1');
  assert.strictEqual(inside.jobs[0].company_domain, 'acme.com');
});
