# api-analytics-hub

[![CI](https://github.com/Exalt24/api-analytics-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/Exalt24/api-analytics-hub/actions/workflows/ci.yml)

![The dashboard against a local stack: an API key unlocks it, 90 days of orders and sales read from stored Shopify data, an admin queues a sync, and a viewer key is refused with its reason](docs/demo.gif)

*Recorded against the local stack (docker Postgres, `seed_local.py` with real figures from a Shopify development store, the API and the built dashboard) with `scripts/record_dashboard.cjs`. [Full video](docs/demo.mp4).*

Multi-tenant analytics hub: external APIs are synced, normalised, stored in PostgreSQL, served by a backend and shown on a dashboard. There are four connectors: **Shopify Admin GraphQL**, verified against a real development store, and **Google Analytics 4**, **Meta Ads** and **Google Search Console**, the last three built and tested against stand-in servers written from the vendors' documentation. The dashboard **never** calls a vendor API to render a page; it reads snapshots the sync wrote. A monthly PowerPoint report puts all four side by side; an n8n workflow builds it for each tenant on the first of the month.

| Source | Built and tested against |
|---|---|
| Shopify | a real development store |
| Google Analytics 4 | a stand-in server ([sources](docs/GA4_SOURCES.md)) |
| Meta Ads | a stand-in server ([sources](docs/META_SOURCES.md)) |
| Search Console | a stand-in server ([sources](docs/GSC_SOURCES.md)) |
| n8n | a real n8n 2.44.6 container, seven runs read node by node |

## Demo

The recording above is the whole stack, started with the commands below. The dashboard front end is on Vercel (https://analytics-hub-pi.vercel.app).

---

## Run it

Requirements: Docker (010 needs pgvector), Python 3.13, Node 20+, and `psql` (or the container's own, second form below).

```bash
# 1. Postgres
docker compose up -d --wait               # exposes 5433, deliberately not 5432; waits for the healthcheck

# 2. Backend
python -m venv .venv
. .venv/Scripts/activate                  # Windows, Git Bash. macOS or Linux: . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="postgresql://analytics:analytics@127.0.0.1:5433/analytics"
export CREDENTIAL_KEYS="1:$(python -c 'from app.crypto import generate_key; print(generate_key())')"
export DASHBOARD_ORIGIN="http://localhost:3100"

# migrations, in order, with the psql client
cat migrations/*.sql | psql "$DATABASE_URL" -v ON_ERROR_STOP=1
# or through the container, without installing psql (run on Windows, in a scratch database)
for f in migrations/*.sql; do docker compose exec -T db psql -q -U analytics -d analytics -v ON_ERROR_STOP=1 < "$f"; done

uvicorn app.api.main:app --port 8000

# 3. Dashboard
cd web && npm install && NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000 npm run build
npm start -- -p 3100
```

`seed_local.py` needs the owner's Shopify development store (a two-line domain and token file: `SHOPIFY_DEV_TOKEN_FILE` or `.secrets/shopify_dev_token.txt`); anyone else runs `python seed_demo.py` (sample figures, same keys). Both drop the `public` schema; `seed_demo.py` applies every migration, `seed_local.py` only 001 to 003 (run 004 to 012 after) and loads the last 120 days (`SEED_DAYS`). Each prints an admin and a viewer API key to see the role split. The dashboard is built and started, not run with `npm run dev`, which did not hydrate the page in the browsers this was recorded in; open it as `http://localhost:3100` (the API's CORS origin).

Paste a key into the box: the viewer key can read, and Sync now comes back 403 with the reason.

---

## Layout

`app/` holds the connectors, sync runner, API, change detection and monthly report, `migrations/` the SQL in order, `n8n/` the monthly workflow, `web/` the dashboard and `tests/` the suite and guards. File by file: [docs/layout.md](docs/layout.md). Backups with a proven restore: [docs/backups.md](docs/backups.md).

---

## Architecture, briefly

**Connectors translate, and nothing else.** A connector fetches a window and yields
`CanonicalPoint` values. It never returns a vendor's field names, so `spend` from
one platform and `cost` from another arrive as one metric key with one unit.
Adding a platform means writing one connector and registering it; the scheduler,
the API and the dashboard do not change. GA4, Meta and Search Console each landed as one
class, one registry entry and a migration of metric keys; the exceptions were the currency check, which names its money metrics,
Search Console's per query rows, which got a table of their own, and the monthly report, which assumed a single source (see Monthly report).

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

`app/ai/catalog_copy.py` is an LLM copywriter for product meta descriptions that refuses to publish a number it was not given: `verify_grounded` requires every number in the output to appear in the product facts in the prompt, and a failure is a rejected row listing the unsupported figures. Against a live model (`ai_live_check.py`) three runs with the rule in the prompt invented nothing; with it stripped the model produced a 99.99% purity and 150 x 100 x 30 mm dimensions from nowhere and the verifier refused all four figures, which is why there are two layers. Fixes found by its own tests: [docs/catalogue-copy.md](docs/catalogue-copy.md); 30 tests and four mutation guards cover it.

---

## Competitor price watch

`app/price_watch/` reads competitor pages in Chromium (Playwright), matches them to the catalogue (an uncertain match waits for a person), labels estimated shipping ESTIMATED, reports where a competitor's landed price beats ours without breaking a floor, and syncs person-approved prices to Shopify and Zoho Inventory. Tested against stand-in pages and stand-in vendor servers: the vendor calls are written from their [documentation](docs/vendor-contract/README.md). Details and the demo: [docs/price-watch.md](docs/price-watch.md).

---

## Prepaid wallet and billing ledger

`app/billing/` is a prepaid wallet for a multi-tenant SaaS: a double-entry ledger in integer minor units (an unbalanced transaction cannot commit, entries are append only, a wallet cannot go below zero, all enforced by the database under forced row level security), top-ups and refunds through a provider interface (the official Stripe SDK, or a hand written client), per-tenant usage prices, subscriptions with proration, and a signed webhook receiver that applies each event once. Every money command takes an idempotency key and a lost response is settled with the same key, so a retry cannot charge twice. The hand written client was tested against a stand-in written from Stripe's [saved pages](docs/vendor-contract/README.md). [docs/billing.md](docs/billing.md).

**The official Stripe SDK.** `app/billing/stripe_sdk.py` runs payments and refunds through the `stripe` SDK and checks webhooks with its own `stripe.Webhook.construct_event`. It ran against stripe-mock, Stripe's own mock server, and a stand-in that scripts declines, 5xx errors and lost responses. A good signature credits once; a bad or stale one, a replayed event id, another amount and an unknown event type credit nothing more, and eleven mutation guards on these tests went red ([output](docs/test-runs/2026-10-31-stripe-sdk-mutation.txt)).

---

## Monthly report

`scripts/monthly_report.py --month 2026-10 --tenant <uuid>` turns one month of stored snapshots into a PowerPoint deck: seven slides for a store, up to ten with a website, an ad account and a search property. Each added source gets a slide, a headline row and a data health row naming its days with no data. Ads are in the ad account's own currency and never added to a store figure, rates come from the sums, average position is weighted by impressions, and the last days before the report date are marked provisional. The narrative is a fixed template without a model key; a model's text is used only if every figure in it is a computed one, in the right currency and beside its own metric, otherwise it is dropped for the template. That check does not catch a wrong direction word, a cause or a right figure in the wrong role.

**The monthly run.** `POST /api/reports/monthly` builds the same deck, byte for byte, for the same month and says whether it was delivered, so a rerun builds and sends nothing. [The n8n workflow](n8n/monthly_report.workflow.json) calls it for each tenant at 13:00 UTC on the first of the month, and a failing tenant does not stop the others. A real n8n container found two faults a file check could not, both fixed and pinned ([output](docs/test-runs/2026-10-26-n8n.txt)); the schedule, the error branch and the rest are in [docs/MONTHLY_REPORT.md](docs/MONTHLY_REPORT.md#the-monthly-run).

**What it was tested against.** Meta and Search Console ran against stand-in servers on 127.0.0.1. The sample deck's figures are sample data, and the model path is tested through a mocked transport. Sample deck: [docs/monthly-report-sample](docs/monthly-report-sample/contact-sheet.png). Method, tests and limits: [docs/MONTHLY_REPORT.md](docs/MONTHLY_REPORT.md).

---

## Change detection on a daily file

`app/changes.py` loads one day of about 400,000 carriers, finds the few that changed with one hash compare, keeps every change in history and raises each alert once behind a UNIQUE key. Measured on 2026-10-24 (desktop PC, local Docker Postgres): day 2 (400,200 rows, 0.5 percent changed) took 9.89 s and raised 2,371 events; the same file again raised 0, and an error at each of 7 stages left the tables unchanged. Runs on sample data. Method and limits: [docs/change-detection.md](docs/change-detection.md).

---

## Testing

```bash
python -m pytest tests/ -q                # 5,278 tests: 5,270 passed, 3 skipped, 5 known limits
python tests/mutation_check.py            # all 300 hand matched guards; edits source in place, so use a copy (--help)
python live_check.py                      # against the real store
```

Counts are from the run of 2026-10-31 ([output](docs/test-runs/2026-10-31-stripe-sdk-pytest.txt)). The database tests (the compose Postgres, or `ANALYTICS_TEST_DSN`) skip without one (CI starts one), and drop and recreate the `public` schema of whatever database they point at, so aim them at a scratch one.

Of the 300 hand matched guards, 91 are new; the 97 aimed at the report, the services and the migrations went red on 2026-10-26, the 25 for price watch ([output](docs/test-runs/2026-10-30-price-watch-mutation.txt)) and the 35 for billing ([output](docs/test-runs/2026-10-30-billing-mutation.txt)) on 2026-10-30, the 11 for the Stripe SDK on 2026-10-31, and the other 132 were not rerun. The 2026-10-26 code was also mutated from its syntax tree (`tests/derived_guards.py`, driven by the author's `mutate_ast.py`, not in this repository): 2,779 mutants in 14 files, 2,662 killed, 117 survived, 90 of them a `|` in a type annotation and 27 read and equivalent ([table](docs/test-runs/2026-10-26-derived-guards.txt)). Blind review of the ads, search and monthly run code ran 159 cases in four passes: seven findings were fixed by class, six are known limits pinned by tests, and 191 of 201 mutants of the fixes were killed, the rest read ([cases](tests/corpus), [round 2](docs/test-runs/2026-10-26-output-round-2-mutation.txt), [limits](docs/MONTHLY_REPORT.md#limits)). No pass after the second round was run.

---

## Roadmap

The list, in the order I would do it.

1. **Hosting.** A managed Postgres with backups and uptime monitoring is the next step for the deployment.
2. **A clock for the syncs.** The monthly report has one (n8n); next is a cadence for
   `sync/runner.py`, since the three `scripts/sync_*.py` run one window by hand.
3. **Key management.** `CREDENTIAL_KEYS` is an environment variable today; a KMS or
   secrets manager with audited rotation is next, and the versioned envelope makes rotation a config change.
4. **Auth.** API keys with three roles fit a service integration; users, sessions, SSO and per-key scoping are next for a team.
5. **Distinct counts.** `customers` and `users` are per-day distinct counts: the
   dashboard shows buyers per day, and the report averages users over the days that have data.
6. **Connectors.** GA4, Meta and Search Console are built against stand-ins and take an issued token;
   their sources files list each assumption and the one real call that would settle it, and token refresh is next.
7. **Alerting.** A stuck or stale sync posts to a webhook ([docs/observability.md](docs/observability.md)); paging and escalation are next.
8. **Quarantine.** Rows that will not normalise are captured in a table; a review and replay workflow is next.
9. **Traces.** JSON logs, a request id and per process Prometheus metrics are in; traces are next.
10. **Backfill.** A real one needs chunking, restart-safe resumption and a rate-limit budget shared with the live syncs.
