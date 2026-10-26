# api-analytics-hub

[![CI](https://github.com/Exalt24/api-analytics-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/Exalt24/api-analytics-hub/actions/workflows/ci.yml)

![The dashboard against a local stack: an API key unlocks it, 90 days of orders and sales read from stored Shopify data, an admin queues a sync, and a viewer key is refused with its reason](docs/demo.gif)

*Recorded against the local stack (docker Postgres, `seed_local.py` with real figures from a Shopify development store, the API and the built dashboard) with `scripts/record_dashboard.cjs`. [Full video](docs/demo.mp4).*

Multi-tenant analytics hub: external APIs are synced, normalised, stored in PostgreSQL, served by a backend and shown on a dashboard. There are four connectors: **Shopify Admin GraphQL**, verified against a real development store, and **Google Analytics 4**, **Meta Ads** and **Google Search Console**, each tested only against a stand-in server written from the vendor's pages and never run live. The dashboard **never** calls a vendor API to render a page; it reads snapshots the sync wrote. A monthly PowerPoint report puts the store, the website, the ads and search side by side, and an n8n workflow builds it for each tenant on the first of the month.

| Source | Run against | Not run |
|---|---|---|
| Shopify | a real development store | orders older than 60 days |
| Google Analytics 4 | a stand-in server ([sources](docs/GA4_SOURCES.md)) | a live property |
| Meta Ads | a stand-in server ([sources](docs/META_SOURCES.md)) | a live ad account |
| Search Console | a stand-in server ([sources](docs/GSC_SOURCES.md)) | a live property |
| n8n | a real n8n 2.44.6 container, six runs read node by node | live Slack or SMTP, Make, Zapier |

## Live

The dashboard front end is on Vercel (https://analytics-hub-pi.vercel.app) and loads, but **the API behind it has been offline since 2026-09-19**: the free Render PostgreSQL instance expired that day, so the demo key signs in to nothing. There is no live demo until a database is provisioned; run it locally with the steps below.

---

## Run it

Requirements: Docker, Python 3.13, Node 20+, and `psql` (or the container's own, second form below). Run on Windows 11 with Git Bash; the psql form and the macOS and Linux lines were not run.

```bash
# 1. Postgres
docker compose up -d --wait               # exposes 5433, deliberately not 5432; waits for the healthcheck

# 2. Backend
python -m venv .venv
. .venv/Scripts/activate                  # Windows, Git Bash. macOS or Linux: . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="postgresql://analytics:analytics@localhost:5433/analytics"
export CREDENTIAL_KEYS="1:$(python -c 'from app.crypto import generate_key; print(generate_key())')"
export DASHBOARD_ORIGIN="http://localhost:3100"

# migrations, in order, with the psql client
for f in migrations/00*.sql; do psql "$DATABASE_URL" -f "$f"; done
# or through the container, no client needed (run on Windows, in a scratch database)
for f in migrations/00*.sql; do docker compose exec -T db psql -q -U analytics -d analytics -v ON_ERROR_STOP=1 < "$f"; done

uvicorn app.api.main:app --port 8000

# 3. Dashboard
cd web && npm install && npm run build
NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000 npm start -- -p 3100
```

`seed_local.py` needs the owner's Shopify development store: it reads the store's domain and Admin API token from a two-line file named by `SHOPIFY_DEV_TOKEN_FILE`, or `.secrets/shopify_dev_token.txt` (never committed), so nobody else can run it. It drops the `public` schema and applies only migrations 001 to 003, so run 004 to 009 (the report tables included) after it. It then loads real figures (the last 120 days, `SEED_DAYS` changes it) and prints an admin and a viewer API key, the fastest way to see the role split. The dashboard is built and started rather than run with `npm run dev`, because the development server did not hydrate the page in the browsers this was recorded in; open it as `http://localhost:3100` (the API's CORS origin).

Paste a key into the box: the viewer key can read, and Sync now comes back 403 with the reason.

### Ports

Postgres is on **5433** and the dashboard on **3100**, because a clash on 5432 or 3000 looks like an authentication error against the wrong database.

---

## Layout

`app/` holds the connectors, sync runner, API, change detection and monthly report, `migrations/` the SQL in order, `n8n/` the monthly workflow, `web/` the dashboard and `tests/` the suite and guards. File by file: [docs/layout.md](docs/layout.md).

---

## Architecture, briefly

**Connectors translate, and nothing else.** A connector fetches a window and yields
`CanonicalPoint` values. It never returns a vendor's field names, so `spend` from
one platform and `cost` from another arrive as one metric key with one unit.
Adding a platform means writing one connector and registering it; the scheduler,
the API and the dashboard do not change. GA4, Meta and Search Console each landed as one
class, one registry entry and a migration of metric keys. Two things needed more: the
database's currency check names its money metrics, and Search Console's per query rows have
no place in a table keyed by metric and day, so they got a table of their own. The monthly
report assumed a single source, and that is where the work went (see Monthly report).

**Money is an integer in minor units with its currency beside it**, enforced by a
`currency_iff_money` CHECK in the database as well as at the connector boundary,
because a later connector or a manual backfill bypasses the Python guard. Parsing
uses `Decimal`, since `float("19.99")` is `19.989999999999998` and a day of orders
drifts by cents. Zero-decimal currencies such as JPY are not scaled.

**History is accumulated, not fetched.** Snapshots are dated rows per (connection,
metric, day), keyed on the date the *platform* attributed the value to, so 90-day growth
is answerable when an API only exposes a current value, and the clock starts when a
connector goes live. Shopify exposes only 60 days of orders without `read_all_orders`.

**Revenue is attributed to `processedAt`, not `createdAt`.** They differ for every
imported order, and `createdAt` renders a migrated shop's history as one enormous day.

**The tenant boundary is in the database.** Row level security is enabled *and
forced* on every tenant table (all but `api_key`, which is read before the tenant is known and says so in its migration), policies read the tenant from a transaction-local
GUC, and the pool drops into a non-superuser role and refuses to start if that role
can bypass RLS. The API does not rely on a `WHERE tenant_id` clause for isolation, so a forgotten one cannot leak; a few updates (the report run, the sync command) repeat it as a second guard.

**Credentials are encrypted with the row bound in.** AES-256-GCM, and the AAD is
`(tenant_id, connection_id, platform)`. RLS stops a tenant *reading* another row but not
a ciphertext being *copied* between rows; the binding makes a moved blob undecryptable.

**Syncs are leased, not flagged.** `claim_sync` takes a non-blocking advisory lock, so a
scheduled run and a Sync Now click cannot overlap; the claim expires unless renewed, and a
reaper turns a silently dead worker into a visible failure.

---

## Grounded catalogue copy

`app/ai/catalog_copy.py` is an LLM copywriter for product meta descriptions that refuses to publish a number it was not given: `verify_grounded` requires every number in the output to appear in the product facts in the prompt, and a failure is a rejected row listing the unsupported figures. Against a live model with `ai_live_check.py`, three runs with the grounding rule in the prompt invented nothing; with the rule stripped the model produced a 99.99% purity and 150 x 100 x 30 mm dimensions from nowhere and the verifier refused all four figures, which is why there are two layers. Fixes found by its own tests: [docs/catalogue-copy.md](docs/catalogue-copy.md); 30 tests and four mutation guards cover it.

---

## Monthly report

`scripts/monthly_report.py --month 2026-10 --tenant <uuid>` turns one month of stored snapshots into a PowerPoint deck: seven slides for a store, up to ten with a website, an ad account and a search property. Each added source gets a slide, a headline row in the summary table and a row on the data health slide naming its days with no data. Ads are in the ad account's own currency and never added to a store figure, rates come from the sums, average position is weighted by impressions, and the last days before the report date are marked provisional. Every number is computed in code. The narrative is a fixed template without a model key; a model's text is used only if every figure in it is a computed one, in the right currency and beside its own metric, otherwise it is dropped for the template. That check does not catch a wrong direction word, a cause or a right figure in the wrong role.

**The monthly run.** `POST /api/reports/monthly` builds the deck for the caller's tenant and answers the same deck, byte for byte, for the same month, and says whether it was delivered, so a rerun builds nothing and sends nothing. [The n8n workflow](n8n/monthly_report.workflow.json) calls it for each tenant on the first of the month; a failing tenant goes down an error output so the others still run, and the execution ends as an error naming who failed. Slack and email are off until switched on. Running it in a real n8n container found two faults in the first version that a file check could not: a condition that read its left side as text, and a Send Email node that passed its input on after a credential error, so an unsent deck was marked delivered. Both are fixed and pinned by tests; only the six runs of the fixed version are saved ([output](docs/test-runs/2026-10-26-n8n.txt)). Make and Zapier were not built or run.

**What was and was not run.** Meta and Search Console ran only against stand-in servers on 127.0.0.1 written from the vendors' documentation. The sample deck's figures are invented, and the model path is tested only through a mocked transport. Sample deck: [docs/monthly-report-sample](docs/monthly-report-sample/contact-sheet.png). Method, tests and limits: [docs/MONTHLY_REPORT.md](docs/MONTHLY_REPORT.md).

---

## Change detection on a daily file

`app/changes.py` loads one day of about 400,000 carriers, finds the few that changed with one hash compare, keeps every change in history and raises each alert once behind a UNIQUE key. Measured on 2026-10-24 (desktop PC, local Docker Postgres): day 2 (400,200 rows, 0.5 percent changed) took 9.89 s and raised 2,371 events; the same file again raised 0, and an error at each of 7 stages left the tables unchanged. Not scheduled, not tenant scoped, invented data. Method and limits: [docs/change-detection.md](docs/change-detection.md).

---

## Testing

```bash
python -m pytest tests/ -q                # 2,613 tests: 2,608 passed, 5 known limits (virtual environment active)
python tests/mutation_check.py            # all 229 hand matched guards; edits source in place, run it in a copy (--help)
python live_check.py                      # against the real store
```

Counts are from the run of 2026-10-26 ([output](docs/test-runs/2026-10-26-pytest.txt)). The database tests (the compose Postgres, or `ANALYTICS_TEST_DSN`) skip without one, CI starts one, and they drop and recreate the `public` schema of whatever database they point at, so aim them at a scratch database. The mutation harness writes each mutation into the real source file and restores the original bytes, also on Ctrl-C or SIGTERM, but a killed process can leave a weakened line behind: run it in a copy of the repository.

Of the 229 hand matched guards, 209 date from before the Meta, Search Console and monthly run work (41 older, 80 change detection, 40 report, 48 GA4 and website) and 20 are new, for migrations 007, 008 and 009. On 2026-10-26 the 97 aimed at the report, the services and the migrations all went red as they should, run in a copy; the other 132 were not rerun. The harness refuses to start when a guard cannot find its target, and CI checks that on every push.

Hand matched guards go stale when a line is reworded, so the new code was also mutated from its syntax tree (`tests/derived_guards.py`, driven by the author's `mutate_ast.py`, which is not in this repository): 2,779 mutants in 14 files, 2,662 killed, 117 survived, of which 90 are a `|` in a type annotation and 27 were each read and are equivalent changes. Each round of survivors named a line no test pinned, and each real one got a test ([table and reasons](docs/test-runs/2026-10-26-derived-guards.txt)). The registry file was not mutated this way. Nobody independent has reviewed the new code: this stage ran alone, so its tests were written by the author of the code. The commits of this work are not pushed, so the CI badge above covers an earlier commit and no CI run has seen the new code.

---

## What I would improve before real use

This is a lab build, not a production service. Honest list, in the order I would
actually do it.

1. **The deployment is down and was never hardened** (see Live): no managed Postgres with backups, no uptime monitoring.
2. **The syncs have no clock.** The monthly report has one (n8n, run for real), but
   `sync/runner.py` claims, leases, heartbeats and resumes correctly and nothing calls it on a
   cadence; the three `scripts/sync_*.py` run one window by hand. The schedule table exists; the daemon does not.
3. **Key management is an environment variable.** `CREDENTIAL_KEYS` should be a KMS or
   secrets manager with audited rotation; the versioned envelope makes rotation a config change.
4. **API keys are the whole auth model.** No users, sessions, SSO or per-key scoping
   beyond three roles: fine for a service integration, not for a team.
5. **`customers` and `users` are per-day distinct counts and cannot be summed.** The
   dashboard shows buyers per day, and the report averages users over the days that have data.
6. **Three connectors never run live.** GA4, Meta and Search Console have run only against
   stand-in servers written from the vendors' pages, whose sources files list what is UNVERIFIED,
   and they take an issued access token and cannot get or refresh one. The next step is one real ad account and one real property.
7. **Alerting stops at one Slack channel.** A stuck or stale sync posts to a webhook ([docs/observability.md](docs/observability.md)); there is no paging or escalation.
8. **Quarantine is a table, not a workflow.** Rows that will not normalise are captured, not reviewable or replayable.
9. **No traces.** JSON logs, a request id and per process Prometheus metrics that reset on restart.
10. **Backfill is manual.** A real one needs chunking, restart-safe resumption and a rate-limit budget shared with the live syncs.
