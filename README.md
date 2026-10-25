# api-analytics-hub

[![CI](https://github.com/Exalt24/api-analytics-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/Exalt24/api-analytics-hub/actions/workflows/ci.yml)

![The dashboard against a local stack: an API key unlocks it, 90 days of orders and sales read from stored Shopify data, an admin queues a sync, and a viewer key is refused with its reason](docs/demo.gif)

*Recorded against the local stack (docker Postgres, `seed_local.py` with real figures from a Shopify development store, the API and the built dashboard), with `scripts/record_dashboard.cjs`. The hosted copy is offline, see below. [Full video](docs/demo.mp4).*

Multi-tenant analytics hub: external APIs are synced, normalised, stored in PostgreSQL, served by a backend and shown on a dashboard. There are two connectors: **Shopify Admin GraphQL**, verified against a real development store, and **Google Analytics 4**, tested only against a stand-in server and never run against a live property. The dashboard **never** calls a vendor API to render a page; it reads snapshots the sync wrote. A monthly PowerPoint report puts the store and the website side by side.

## Live

The dashboard front end is on Vercel (https://analytics-hub-pi.vercel.app) and loads, but **the API behind it has been offline since 2026-09-19**: the free Render PostgreSQL instance expired that day, so the service cannot reach its database and the demo key signs in to nothing. Until a database is provisioned there is no live demo; run it locally with the steps below. While it was up, the demo key signed in as a viewer and **Sync now** answered `403: role viewer may not write:sync`, the role check working as designed.

---

## Run it

Requirements: Docker, Python 3.13, Node 20+, and the `psql` client (or use the container's own, second form below). Run on Windows 11 with Git Bash. The psql form of the migrations and the macOS and Linux lines (`.venv/bin`, the standard venv layout) were not run here.

```bash
# 1. Postgres
docker compose up -d                      # exposes 5433, deliberately not 5432

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
cd web && npm install
NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000 npm run dev -- -p 3100
```

`seed_local.py` loads real figures from the dev store (the last 120 days by default, `SEED_DAYS` changes it) and prints an admin and a viewer API key, the fastest way to see the role split in the browser. It reads the store's domain and Admin API token from a two-line file named by `SHOPIFY_DEV_TOKEN_FILE` (`SHOP=<store>.myshopify.com`, `ACCESS_TOKEN=<token>`), or `.secrets/shopify_dev_token.txt` when the variable is unset, through `app/secret_files.py`; the file is never committed. Build the dashboard first (`npm run build`, then `npm start -- -p 3100`), because the development server did not hydrate the page in the browsers this was recorded in, and open it as `http://localhost:3100` (the API's CORS origin).

Open the dashboard and paste a key into the box on the page. The viewer key can read, and Sync now comes back 403 with the reason. A deployment can set `NEXT_PUBLIC_DEMO_VIEWER_KEY` for a one-click viewer sign-in; none is shipped, because the hosted demo is offline.

### Ports

Postgres is on **5433** and the dashboard on **3100**, both non-default, because a clash on 5432 or 3000 shows up as an authentication error against the wrong database.

---

## Layout

`app/` holds the connectors, sync runner, API, change detection and the monthly report, `migrations/` the SQL in order, `web/` the Next.js dashboard and `tests/` the suite and the mutation harness. File by file: [docs/layout.md](docs/layout.md).

---

## Architecture, briefly

**Connectors translate, and nothing else.** A connector fetches a window and yields
`CanonicalPoint` values. It never returns a vendor's field names, so `spend` from
one platform and `cost` from another arrive as one metric key with one unit.
Adding a platform means writing one connector and registering it; the scheduler,
the API and the dashboard do not change. GA4 was the test of that claim: it landed as one
class, one registry entry and a migration that adds four metric keys (`sessions`, `users`,
`page_views`, `key_events`), with no change to the contract, the sync runner, the snapshot
table or its row level security. The one place that did assume a single source was the
monthly report, which had a single connection in its query and its deck, and that is where
the work went (see Monthly report).

**Money is an integer in minor units with its currency beside it**, enforced by a
`currency_iff_money` CHECK in the database as well as at the connector boundary,
because a later connector or a manual backfill bypasses the Python guard. Parsing
uses `Decimal`: `float("19.99")` is `19.989999999999998` and a day of orders drifts
by cents. Zero-decimal currencies such as JPY are not scaled.

**History is accumulated, not fetched.** Snapshots are dated rows per (connection,
metric, day), never overwritten, keyed on the date the *platform* attributed the
value to. That is what makes 90-day growth answerable when an API only exposes a
current value, and it is why the clock starts when a connector goes live. Shopify
exposes only 60 days of orders without approval for `read_all_orders`.

**Revenue is attributed to `processedAt`, not `createdAt`.** They differ for every
imported, migrated or backdated order, and the Admin API lets you set the former
and not the latter. Bucketing on `createdAt` renders a migrated shop's entire
history as one enormous day, with every individual figure correct.

**The tenant boundary is in the database.** Row level security is enabled *and
forced* on every tenant table, policies read the tenant from a transaction-local
GUC, and the pool drops into a non-superuser role and refuses to start if that role
can bypass RLS. The API never writes `WHERE tenant_id = ...` for isolation, so a
forgotten clause cannot leak anything.

**Credentials are encrypted with the row bound in.** AES-256-GCM, and the AAD is
`(tenant_id, connection_id, platform)`. RLS stops a tenant *reading* another row and
does nothing about a ciphertext being *copied* between rows by anything with write
access; the binding makes a moved blob undecryptable.

**Syncs are leased, not flagged.** `claim_sync` takes a non-blocking advisory lock,
so a scheduled run and a Sync Now click cannot overlap. The claim expires unless the
worker keeps renewing it, and a reaper turns a died-silently worker into a visible
failure instead of a row stuck at `running` forever.

---

## Grounded catalogue copy

`app/ai/catalog_copy.py` is an LLM copywriter for product meta descriptions that refuses to publish a number it was not given: `verify_grounded` extracts every number from the output and requires each one to appear in the product facts that went into the prompt, and a failure is a rejected row with the unsupported figures listed. I ran it against a live model in both directions with `ai_live_check.py` (your own Shopify dev store token and Groq key). With the grounding rule in the prompt, three runs invented nothing. With the rule stripped, the model produced a 99.99% purity and 150 x 100 x 30 mm dimensions from nowhere and the verifier refused all four figures, which is why there are two layers. Its own tests found three defects in the first version (digits in chemical formulas read as quantities, a reasoning model spending 158 of its 160 completion tokens on its trace, numbers written as words). Design decisions and fixes: [docs/catalogue-copy.md](docs/catalogue-copy.md). `tests/test_catalog_copy.py` and `tests/test_catalog_copy_transport.py` cover it (30 tests), and four of the mutation guards target it.

---

## Monthly report

`scripts/monthly_report.py --month 2026-10 --tenant <uuid>` turns one month of stored snapshots into a PowerPoint deck: seven slides for a store, eight when the tenant also has a Google Analytics 4 connection. The website figures (sessions, users per active day, page views, key events and an order rate) sit next to the store's with the change on the month before, a sessions chart stands beside the orders chart, a weekly table sets sessions against orders, and the data health slide names the days with no data for each source. `scripts/sync_ga4.py` pulls a property's daily figures into the snapshots, with its credentials from the environment. Every number is computed in code. The narrative is a fixed template when no model key is set. A model's text is used only if every figure in it is a computed figure, in the right currency and beside its own metric, otherwise it is dropped for the template. That check does not catch a wrong direction word, a cause or a right figure in the wrong role; its other known limits are in the docs.

**What was and was not run.** The GA4 side was tested against a stand-in server on 127.0.0.1 written from Google's documentation, and it has not run against a live property. The sample deck's figures, store and website, are invented. A store with no website renders exactly as before, which a test checks against the saved read-back of the earlier sample. The model path is tested only through a mocked transport. What the Google pages said and what is UNVERIFIED: [docs/GA4_SOURCES.md](docs/GA4_SOURCES.md). Sample deck and render: [docs/monthly-report-sample](docs/monthly-report-sample/contact-sheet.png). Method, tests and limits: [docs/MONTHLY_REPORT.md](docs/MONTHLY_REPORT.md).

---

## Change detection on a daily file

`app/changes.py` loads one day of about 400,000 carriers, finds the few that changed with one hash compare, keeps every change in history and raises each alert once behind a UNIQUE key. Measured on 2026-10-24 (desktop PC, local Docker Postgres): day 2 (400,200 rows, 0.5 percent changed) took 9.89 s in one transaction and raised 2,371 events; the same file again raised 0, and an error at each of 7 stages left the tables unchanged. Not scheduled, not tenant scoped, not wired into the hub, invented data, delivery at least once. Numbers, method and limits: [docs/change-detection.md](docs/change-detection.md).

---

## Testing

```bash
python -m pytest tests/ -q                # 1423 passed, 5 known limits (virtual environment active)
python tests/mutation_check.py            # all 209 guards; edits source in place, run it in a copy (--help)
python live_check.py                      # against the real store
```

Counts are from a run on 2026-10-25. The database tests (the compose Postgres, or `ANALYTICS_TEST_DSN`) skip without one, CI starts one, and they drop and recreate the `public` schema of whatever database they point at, so aim them at a scratch database; the mutation harness needs it up the whole run. It writes each mutation into the real source file and restores the original bytes, also on Ctrl-C or SIGTERM, but a killed process can leave a weakened line behind: run it in a copy of the repository.

Of the 209 guards, 41 are older, 80 cover change detection, 40 the monthly report and 48 the GA4 connector, the website half of the report and their database path. On 2026-10-25 the 40 report guards and the 48 new ones all caught their mutation, run in a copy of the repository; the other 121 were not rerun after the GA4 change. The harness refuses to start when a guard cannot find its target, and CI checks that on every push. Saved output: [docs/test-runs](docs/test-runs).

The mutation harness is the part worth reading: it breaks each load-bearing guard in turn and asserts the suite goes red, because a green suite is not evidence. It has caught four of this project's own vacuous tests, one of which measured a constant using that same constant.

---

## What I would improve before real use

This is a lab build, not a production service. Honest list, in the order I would
actually do it.

1. **The deployment is down and was never hardened** (see Live): no managed Postgres with backups, no uptime monitoring.
2. **The scheduler is a runner without a clock.** `sync/runner.py` claims, leases,
   heartbeats and resumes correctly, and nothing calls it on a cadence; `scripts/sync_ga4.py`
   runs one window by hand. The schedule table and jitter column exist; the daemon does not.
3. **Key management is an environment variable.** `CREDENTIAL_KEYS` should be a KMS or
   secrets manager with audited rotation. The versioned envelope makes rotation a config
   change, not a migration, but the key still sits in the process environment.
4. **API keys are the whole auth model.** No users, sessions or SSO, and no per-key
   scoping beyond three roles. Fine for a service integration, not for a team.
5. **`customers` and `users` are per-day distinct counts and cannot be summed.** The
   dashboard shows buyers per day for this reason, and the report averages users over the
   days that have data. A true window-distinct needs identities kept per day or an
   aggregate computed at source.
6. **Two connectors, one never run live.** GA4 showed what the contract absorbs and
   what it does not: the report assumed a single source. It has run only against a
   stand-in server written from Google's pages ([docs/GA4_SOURCES.md](docs/GA4_SOURCES.md)
   lists what is UNVERIFIED), it takes an issued access token and cannot get or refresh
   one, and nothing calls its sync on a schedule. The next step is one real property.
7. **Alerting stops at one Slack channel.** A stuck or stale sync posts to an incoming webhook ([docs/observability.md](docs/observability.md)); there is no paging, on-call rota or escalation.
8. **Quarantine is a table, not a workflow.** Rows that will not normalise are captured,
   with no way to review or replay them.
9. **No traces.** JSON logs, a request id and per process Prometheus metrics that reset on restart; a slow sync shows as a late run, not a breakdown.
10. **Backfill is manual.** Windows are passed by hand. A real backfill needs chunking,
    resumability across restarts, and a rate-limit budget shared with the live syncs.
