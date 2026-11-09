# api-analytics-hub

[![CI](https://github.com/Exalt24/api-analytics-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/Exalt24/api-analytics-hub/actions/workflows/ci.yml)

![The dashboard against a local stack: an API key unlocks it, 90 days of orders and sales read from stored Shopify data, an admin queues a sync, and a viewer key is refused with its reason](docs/demo.gif)

*Recorded against the local stack (`scripts/record_dashboard.cjs`): docker Postgres, `seed_local.py` with real figures from a Shopify development store, the API and the built dashboard. [Full video](docs/demo.mp4).*

Multi-tenant analytics hub: external APIs are synced, normalised, stored in PostgreSQL, served by a backend and shown on a dashboard.

There are ten connectors. **Shopify Admin GraphQL** is verified against a real development store. Nine more (**Google Analytics 4**, **Meta Ads**, **Search Console**, **Google Ads**, **TikTok Ads**, **Google Business Profile**, **Square**, **QuickBooks Online**, **Shopmonkey**) are built and tested against stand-in servers written from the vendors' documentation. The dashboard **never** calls a vendor API to render a page. It reads snapshots the sync wrote. A monthly PowerPoint report puts all four side by side, built by an n8n workflow for each tenant on the first of the month.

| Source | Built and tested against |
|---|---|
| Shopify | a real development store |
| Google Analytics 4 | a stand-in server ([sources](docs/GA4_SOURCES.md)) |
| Meta Ads | a stand-in server ([sources](docs/META_SOURCES.md)) |
| Search Console | a stand-in server ([sources](docs/GSC_SOURCES.md)) |
| Google Ads, TikTok Ads, Google Business Profile, Square, QuickBooks Online, Shopmonkey | a stand-in server each ([sources](docs/AD_AND_BUSINESS_SOURCES.md)) |
| n8n | a real n8n 2.44.6 container, seven runs read node by node |

## Demo

The dashboard front end is on Vercel (https://analytics-hub-pi.vercel.app).

---

## Run it

Requirements: Docker (010 needs pgvector), Python 3.13, Node 20+, and `psql` (or the container's own, second form below). `nix develop` gives Python 3.13, PostgreSQL 17 with pgvector, git and Node 22 from a lock file ([docs/nix.md](docs/nix.md)).

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

`seed_local.py` needs the owner's Shopify development store. That is a two-line domain and token file: `SHOPIFY_DEV_TOKEN_FILE` or `.secrets/shopify_dev_token.txt`. Anyone else runs `python seed_demo.py` (sample figures, same keys). Both drop the `public` schema. `seed_demo.py` applies every migration. `seed_local.py` applies only 001 to 003, so run 004 to 012 after. It loads the last 120 days (`SEED_DAYS`). Each prints an admin and a viewer API key to see the role split. The dashboard is built and started, not run with `npm run dev`. `npm run dev` did not hydrate the page in the browsers this was recorded in. Open it as `http://localhost:3100`.

Paste a key into the box. The viewer key can read. Sync now comes back 403 with the reason.

---

## Layout

`app/` holds the connectors, sync runner, API, change detection, monthly report and `sms.py` (a Twilio SMS sender, [docs/sms.md](docs/sms.md)). `migrations/` holds the SQL in order. `n8n/` holds the monthly workflow. `web/` is the dashboard. `tests/` holds the suite and guards. File by file: [docs/layout.md](docs/layout.md).

---

## Architecture, briefly

**Connectors translate, and nothing else.** A connector fetches a window and yields
`CanonicalPoint` values. It never returns a vendor's field names. So `spend` from
one platform and `cost` from another arrive as one metric key with one unit.
Adding a platform means writing one connector and registering it. The scheduler,
the API and the dashboard do not change. GA4, Meta, Search Console and the five newest each landed as one
class, one registry entry and a migration of metric keys. There were three exceptions. The currency check names its money metrics. Search Console's per query rows got a table of their own. The monthly report assumed a single source (see Monthly report).

**Money is an integer in minor units with its currency beside it.** A
`currency_iff_money` CHECK enforces that in the database as well as at the connector boundary.
A later connector or a manual backfill bypasses the Python guard. Parsing
uses `Decimal`. `float("19.99")` is `19.989999999999998`, so a day of orders
drifts by cents. Zero-decimal currencies such as JPY are not scaled.

**History is accumulated, not fetched.** Snapshots are dated rows per (connection,
metric, day). They are keyed on the date the *platform* attributed the value to. So 90-day growth
is answerable when an API only exposes a current value. The clock starts when a
connector goes live. Shopify exposes only 60 days of orders without `read_all_orders`.

**Revenue is attributed to `processedAt`, not `createdAt`.** They differ for every
imported order. `createdAt` renders a migrated shop's history as one enormous day.

**The tenant boundary is in the database.** Row level security is enabled *and
forced* on every tenant table. The one exception is `api_key`, which is read before the tenant is known and says so in its migration. Policies read the tenant from a transaction-local
GUC. The pool drops into a non-superuser role and refuses to start if that role
can bypass RLS. The API does not rely on a `WHERE tenant_id` clause for isolation, so a forgotten one cannot leak. A few updates (the report run, the sync command) repeat it as a second guard.

**Credentials are encrypted with the row bound in.** AES-256-GCM, and the AAD is
`(tenant_id, connection_id, platform)`. RLS stops a tenant *reading* another row. It does not stop
a ciphertext being *copied* between rows. The binding makes a moved blob undecryptable.

**Syncs are leased, not flagged.** `claim_sync` takes a non-blocking advisory lock. A
scheduled run and a Sync Now click cannot overlap. The claim expires unless renewed. A
reaper turns a silently dead worker into a visible failure.

---

## Ad platforms, business systems and the ad spend view

Google Ads, TikTok, Square, QuickBooks and Shopmonkey are written from the vendors' public documentation. They are tested against stand-in servers that follow it. Google Ads also counts Local Services Ads leads. Google Business Profile reads a local listing's daily calls, website clicks, direction requests and impressions. `app/report/adspend.py` puts Google Ads, Meta and TikTok side by side. It shows spend, conversions and cost per conversion, by platform and week, with the shop's booked jobs. Details and limits: [docs/ad-spend.md](docs/ad-spend.md).

---

## Grounded catalogue copy

`app/ai/catalog_copy.py` is an LLM copywriter for product meta descriptions. It refuses to publish a number it was not given. `verify_grounded` requires every number in the output to appear in the product facts in the prompt. A failure is a rejected row listing the unsupported figures. Against a live model (`ai_live_check.py`), three runs with the rule in the prompt invented nothing. With the rule stripped, the model produced a 99.99% purity and 150 x 100 x 30 mm dimensions from nowhere. The verifier refused all four figures. That is why there are two layers. Fixes found by its own tests: [docs/catalogue-copy.md](docs/catalogue-copy.md). 30 tests and four mutation guards cover it.

---

## Competitor price watch

`app/price_watch/` reads competitor pages in Chromium (Playwright). It matches them to the catalogue, and an uncertain match waits for a person. It labels estimated shipping ESTIMATED. It reports where a competitor's landed price beats ours without breaking a floor. It syncs person-approved prices to Shopify and Zoho Inventory. It is tested against stand-in pages and stand-in vendor servers. The vendor calls are written from their [documentation](docs/vendor-contract/README.md). Details and the demo: [docs/price-watch.md](docs/price-watch.md).

---

## Prepaid wallet and billing ledger

`app/billing/` is a prepaid wallet for a multi-tenant SaaS with a double-entry ledger in integer minor units, enforced by the database under forced row level security: an unbalanced transaction cannot commit, entries are append only, and a wallet cannot go below zero. Top-ups, refunds, subscriptions and a signed webhook receiver run through a provider interface (the official Stripe SDK, tested against stripe-mock, or a hand written client tested against a stand-in written from Stripe's [saved pages](docs/vendor-contract/README.md)), and every money command takes an idempotency key. Detail, mutation guards and limits: [docs/billing.md](docs/billing.md).

---

## Monthly report

`scripts/monthly_report.py --month 2026-10 --tenant <uuid>` turns one month of stored snapshots into a PowerPoint deck. It has seven slides for a store and up to ten with a website, an ad account and a search property. Each added source gets a slide, a headline row and a data health row naming its days with no data. Ads are in the ad account's own currency and never added to a store figure. Rates come from the sums. Average position is weighted by impressions. The last days before the report date are marked provisional. The narrative is a fixed template without a model key. A model's text is used only if every figure in it is a computed one, in the right currency and beside its own metric. Otherwise it is dropped for the template. That check does not catch a wrong direction word, a cause or a right figure in the wrong role.

**The monthly run.** `POST /api/reports/monthly` builds the same deck, byte for byte, for the same month. It says whether it was delivered, so a rerun builds and sends nothing. [The n8n workflow](n8n/monthly_report.workflow.json) calls it for each tenant at 13:00 UTC on the first of the month. A failing tenant does not stop the others. A real n8n container found two faults a file check could not. Both are fixed and pinned ([output](docs/test-runs/2026-10-26-n8n.txt)). The schedule, the error branch and the rest are in [docs/MONTHLY_REPORT.md](docs/MONTHLY_REPORT.md#the-monthly-run).

**Google Drive delivery.** `scripts/monthly_report.py --drive-folder <folder id>` uploads the finished deck to a Drive folder through the Drive API v3 (multipart or resumable, with backoff). The file name is stable for a tenant and month, so a rerun replaces the file and never adds a copy. Tested against a stand-in written from Google's documentation and once for real into a personal Drive: created, replaced, read back, deleted ([output](docs/test-runs/2026-11-10-drive-delivery.txt)). The n8n workflow does not call it yet. [Detail](docs/drive-delivery.md).

**What it was tested against.** Meta and Search Console ran against stand-in servers on 127.0.0.1. The sample deck's figures are sample data. The model path is tested through a mocked transport. Sample deck: [docs/monthly-report-sample](docs/monthly-report-sample/contact-sheet.png). Method, tests and limits: [docs/MONTHLY_REPORT.md](docs/MONTHLY_REPORT.md).

---

## Report export from the dashboard

The Reports page in `web/` exports a tenant's month as CSV, Excel or PDF through Next.js Server Actions with Zod checks. It reads through Kysely under forced row level security. An AI summary is optional: a code guard refuses any figure not in the data, a refused one is retried once, then a plain template is used and the file says so. A document attached goes up by a presigned S3 URL. The page is Next.js App Router with strict TypeScript, Tailwind CSS 4, React Hook Form with Zod, the Vercel AI SDK and shadcn/ui on Radix UI. Saved presets are local first: Replicache keeps them in IndexedDB so a save works offline, and route handlers sync them to tenant scoped Postgres. Tested on real Postgres, a scripted fake model and SeaweedFS for S3; no real model or AWS account. The Kysely read path also ran against a free Neon Postgres project under a non superuser role, and Neon's owner role was refused at startup ([detail](docs/web-reports.md#the-neon-run)). Page state is held in React Context; 156 tests, 12 browser tests, and 76 deliberate breakages each caught. Claims and tests: [docs/web-reports.md](docs/web-reports.md).

---

## Change detection on a daily file

`app/changes.py` loads one day of about 400,000 carriers. It finds the few that changed with one hash compare. It keeps every change in history. It raises each alert once behind a UNIQUE key. Measured on 2026-10-24 (desktop PC, local Docker Postgres): day 2 (400,200 rows, 0.5 percent changed) took 9.89 s and raised 2,371 events. The same file again raised 0. An error at each of 7 stages left the tables unchanged. Runs on sample data. Method and limits: [docs/change-detection.md](docs/change-detection.md).

---

## Testing

```bash
python -m pytest tests/ -q                # 6,627 tests: 6,612 passed, 10 skipped, 5 known limits
python tests/mutation_check.py            # all 300 hand matched guards; edits source in place, so use a copy (--help)
python live_check.py                      # against the real store
nix run .#fast-tests                      # 470 of them in a pinned Nix shell, about a second ([docs/nix.md](docs/nix.md))
cd web && npm test                        # 156 tests ([docs/web-reports.md](docs/web-reports.md))
cd web && npm run build && npx playwright test    # 12 browser tests
cd web && node scripts/mutation_check.mjs # 76 guards broken one at a time; use a copy
```

Run dates, outputs and the skipped tests (the stripe-mock and pg_dump ones, which run in CI): [docs/TESTING.md](docs/TESTING.md).

The harness holds 300 hand matched guards plus 2,779 syntax tree mutants of the 2026-10-26 code. Figures, outputs and known limits are in [docs/TESTING.md](docs/TESTING.md).

---

## Open items

The list, in the order I would do it, is in [docs/roadmap.md](docs/roadmap.md).
