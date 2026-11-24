# api-analytics-hub

[![CI](https://github.com/Exalt24/api-analytics-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/Exalt24/api-analytics-hub/actions/workflows/ci.yml)

![The dashboard against a local stack: an API key unlocks it, 90 days of orders and sales read from stored Shopify data, an admin queues a sync, and a viewer key is refused with its reason](docs/demo.gif)

*Recorded against the local stack: docker Postgres, `seed_local.py` with real figures from a Shopify development store, the API and the built dashboard. [Full video](docs/demo.mp4).*

Multi-tenant analytics hub: external APIs are synced, normalised, stored in PostgreSQL, served by a backend and shown on a dashboard.

There are thirteen connectors. **Shopify Admin GraphQL** is verified against a real development store. Twelve more (table below) ran against stand-in servers written from the vendors' documentation. The dashboard **never** calls a vendor API to render a page; it reads snapshots the sync wrote. A monthly PowerPoint report, built by an n8n workflow for each tenant, puts the sources side by side.

| Source | Built and tested against |
|---|---|
| Shopify | a real development store |
| Google Analytics 4 | a stand-in server ([sources](docs/GA4_SOURCES.md)) |
| Meta Ads | a stand-in server ([sources](docs/META_SOURCES.md)) |
| Search Console | a stand-in server ([sources](docs/GSC_SOURCES.md)) |
| Google Ads, TikTok Ads, Google Business Profile, Square, QuickBooks Online, Shopmonkey, Salesforce | a stand-in server each ([sources](docs/AD_AND_BUSINESS_SOURCES.md)) |
| Harvest (time, budgets), Microsoft Graph (SharePoint, Teams, Excel) | a stand-in server each ([how](docs/harvest-graph.md)) |
| n8n | a real n8n 2.44.6 container, seven runs read node by node |

The dashboard front end is on Vercel (https://analytics-hub-pi.vercel.app).

---

## Run it

Requirements: Docker (010 needs pgvector), Python 3.13, Node 20+, and `psql` (or the container's own, second form below). `nix develop` gives the same tools from a lock file ([docs/nix.md](docs/nix.md)).

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
# or, without psql:
for f in migrations/*.sql; do docker compose exec -T db psql -q -U analytics -d analytics -v ON_ERROR_STOP=1 < "$f"; done

uvicorn app.api.main:app --port 8000

# 3. Dashboard
cd web && npm install && NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000 npm run build
npm start -- -p 3100
```

`seed_local.py` needs the owner's Shopify development store (a two-line domain and token file); anyone else runs `python seed_demo.py` (sample figures, same keys). Both drop the `public` schema and print an admin and a viewer API key; `seed_local.py` applies 001 to 003, so run the rest after. Build and start the dashboard (not `npm run dev`, which did not hydrate) and open `http://localhost:3100`.

---

## Layout

`app/` holds the connectors, sync runner, API, change detection, monthly report and `sms.py` ([docs/sms.md](docs/sms.md)). `migrations/` holds the SQL in order. Backups: [docs/backups.md](docs/backups.md); metrics: [docs/observability.md](docs/observability.md). File by file: [docs/layout.md](docs/layout.md).

---

## Architecture, briefly

Connectors translate and nothing else: each yields `CanonicalPoint` values, never a vendor's field names. Money is an integer in minor units with its currency beside it. History is accumulated in dated snapshots, not fetched. The tenant boundary is in the database: row level security is enabled and forced on every tenant table and the pool refuses to start as a role that can bypass it. Credentials are AES-256-GCM encrypted with the row bound in, and syncs are leased, not flagged. Each decision and why: [docs/architecture.md](docs/architecture.md).

---

## Ad platforms, business systems and the ad spend view

Google Ads, TikTok, Square, QuickBooks, Shopmonkey and Salesforce are written from the vendors' public documentation and ran against stand-in servers. Salesforce adds OAuth refresh, SOQL paging and a lead matcher with a reason code per answer ([docs/salesforce.md](docs/salesforce.md)). `app/report/adspend.py` puts Google Ads, Meta and TikTok side by side. Limits: [docs/ad-spend.md](docs/ad-spend.md).

---

## Grounded catalogue copy

`app/ai/catalog_copy.py` is an LLM copywriter for product meta descriptions that refuses to publish a number it was not given: `verify_grounded` rejects any row whose figures are not in the product facts. Against a live model it invented nothing with the rule, and made up a 99.99% purity and a 150 x 100 x 30 mm size without it, all four refused. 30 tests and four mutation guards: [docs/catalogue-copy.md](docs/catalogue-copy.md).

---

## Competitor price watch

`app/price_watch/` reads competitor pages in Chromium (Playwright). It matches them to the catalogue (an uncertain match waits for a person), reports where a competitor's landed price beats ours without breaking a floor, and syncs person-approved prices to Shopify and Zoho Inventory. Tested against stand-in pages and vendor servers written from the vendors' [documentation](docs/vendor-contract/README.md). Details: [docs/price-watch.md](docs/price-watch.md).

---

## Job pipeline

An hourly n8n workflow pulls ten job data vendors, normalises and dedupes them, archives the raw JSON, embeds each new job into Qdrant and scores a tailored resume and cover letter that refuses any claim the applicant's facts do not hold. A form filler types a field only at 0.95 confidence; the rest go to a review queue where a person answers, and the answer resumes the run through RabbitMQ and an n8n Wait node.

Run against stand-ins, real Redis, Qdrant, RabbitMQ, Postgres and n8n ([output](docs/test-runs/2026-11-17-job-pipeline-n8n.txt)), with the embeddings and a chat model live ([output](docs/test-runs/2026-11-17-job-pipeline-live.txt)). Not pointed at a vendor account or an employer's application form. [Details](docs/job-pipeline.md), [vendors](docs/job-vendors.md).

---

## Prepaid wallet and billing ledger

`app/billing/` is a prepaid wallet with a double-entry ledger in integer minor units under forced row level security: an unbalanced transaction cannot commit and a wallet cannot go below zero. Top-ups, refunds, subscriptions and a signed webhook receiver run through a Stripe provider interface (the official SDK against stripe-mock, or a client written from Stripe's [saved pages](docs/vendor-contract/README.md)), with an idempotency key on every money command: [docs/billing.md](docs/billing.md).

---

## Monthly report

`scripts/monthly_report.py --month 2026-10 --tenant <uuid>` turns one month of stored snapshots into a PowerPoint deck: seven slides for a store, up to ten with a website, ads and search. The narrative is a fixed template; a model's text is used only if every figure in it is computed.
**The monthly run.** `POST /api/reports/monthly` builds the same deck byte for byte and says whether it was delivered, so a rerun sends nothing. [The n8n workflow](n8n/monthly_report.workflow.json) calls it for each tenant at 13:00 UTC on the first of the month; a real n8n container found two faults a file check could not ([output](docs/test-runs/2026-10-26-n8n.txt)).

**Google Drive delivery.** `--drive-folder <id>` uploads the deck under a stable name, so a rerun replaces it; tested against a stand-in and once for real ([output](docs/test-runs/2026-11-10-drive-delivery.txt), [detail](docs/drive-delivery.md)). [Method and limits](docs/MONTHLY_REPORT.md).

---

## Report export from the dashboard

The Reports page in `web/` (Next.js App Router, strict TypeScript, Tailwind CSS 4, shadcn/ui) exports a tenant's month as CSV, Excel or PDF through Server Actions with Zod checks. An optional AI summary passes a code guard that refuses any figure not in the data. Documents attach by presigned S3 URL, and saved presets sync to tenant scoped Postgres. Tested on real Postgres, a fake model, SeaweedFS, one live Groq run and a free Neon project ([detail](docs/web-reports.md#the-neon-run)): 156 tests, 12 browser tests, 76 deliberate breakages each caught.

---

## Inventory planning

![The Inventory page on the plan stored from the development store: two shortages, a purchase order draft for a supplier and the start of the planning table](docs/inventory.png)

`app/inventory/` reads **Shopify inventory levels per product variant** (available, on hand, committed and incoming at each location) and keeps them as dated snapshots in Postgres for each tenant under forced row level security, beside the order lines sold. It works out a **stockout-adjusted forecast**: the daily sales velocity of each SKU and variant is the units sold over the days it was on sale, so ten days with an empty shelf do not read as demand falling (40 units over 20 days on sale is 2.00 a day, where the 28 day window would say 1.43). From the supplier's lead time and the variation in demand it gives **days of cover, a reorder point and safety stock**, and a suggested order rounded to the minimum order and whole packs. A dated promotion factor a person enters lifts the forecast. Variants are ranked into ABC classes by sales contribution and labelled an **all-time best seller, a rising best seller or a slow mover**. Each shortage raises one alert behind a UNIQUE key, through the outbox code the change detection uses. The `/inventory` page is the **inventory dashboard with exception alerts** and writes a **supplier purchase order draft** for each supplier as CSV, Excel or a Google Sheets paste, reusing the Reports export code.

Run against the owner's Shopify development store, read only: 38 variants and 28 order lines, with five variants and 25 test orders made for the run and removed after it. The store holds no suppliers, so the lead times and one promotion are inputs. Its stock history began on the first sync, so the stockout adjustment is shown on a history written through the same code ([the run](docs/test-runs/2026-11-24-inventory-planning-dev-store.txt)).

```
variant               on hand  sold/day  cover  lead  reorder pt  order  class  label
IPT Studio Tee - M         10      0.57   17.5    21          28     30  A      best seller (2.0x the prior 28 days)
IPT Canvas Cap - Red      120      0.25  480.0    30          15      0  B      rising (7.0x)
IPT Wool Sock - L           0      0.14    0.0    21           6     48  C      steady, out of stock
-> alerts: sock out of stock; tee 17.5 days of cover against a 21 day lead time
-> draft for Harbor Textiles, expected 2026-12-15: 48 x IPT-SOCK-L, 30 x IPT-TEE-M, $408.00
```

Formulas, the rule for a day with no snapshot and what is not done: [docs/inventory-planning.md](docs/inventory-planning.md).

---

## Power BI report

![Power BI Desktop, Sales and ads page: cards, daily net sales with a seven day average, ad spend by month, a month table](docs/powerbi/sales-and-ads.png)

`powerbi/` holds a Power BI report over the same snapshot tables, as a `.pbix` and as a `.pbip` project: a five table star schema, 27 DAX measures, two pages and 18 visuals, on sample figures. `powerbi/check_model.py` runs the measures against 56 figures worked out from the CSV files: all match, and one dollar changed in the data fails six. [Details](docs/powerbi.md).

---

## Subdomain client portals and e-signatures

A Next.js proxy reads the Host header and rewrites a one label subdomain to an internal route; a key opens a portal only when its tenant's row carries that host's slug. Run on local Host headers against Postgres (16 tests, 14 live HTTP checks, 8 breakages caught): [docs/portals.md](docs/portals.md).

**E-signatures.** A client reads a document's SHA-256 and a versioned consent text, types their full name and signs once; the record links into a hash chained, append only audit log, and `verify` names the row or byte that changed. 212 tests over 50 seeded worlds and 63 guards, each caught. A tamper-evident record, not a certified e-signature service, not legal advice: [docs/esign.md](docs/esign.md).

---

## Change detection on a daily file

`app/changes.py` loads one day of about 400,000 carriers, finds the few that changed with one hash compare, keeps every change in history and raises each alert once behind a UNIQUE key. Measured on 2026-10-24 (local Docker Postgres, sample data): day 2 (400,200 rows, 0.5 percent changed) took 9.89 s and raised 2,371 events; the same file again raised 0, and an error at each of 7 stages left the tables unchanged. Method and limits: [docs/change-detection.md](docs/change-detection.md).

---

## Lakehouse: PySpark and Delta Lake

`lakehouse/` is a medallion pipeline over the hub's own daily figures, on open source Spark 4.1 and Delta Lake 4.3 on one machine. The Spark code never ran on Databricks; the same layers written as SQL did, on a Free Edition workspace ([run](docs/databricks)). Bronze quarantines an unreadable record with its reason, silver merges typed rows with a Delta `MERGE`, gold is a star schema, and a second run on the same input left every Delta version and row count unchanged. 68 tests; 24 of 24 breakages caught. [Details and limits](docs/lakehouse.md), [runbook](lakehouse/RUNBOOK.md).

---

## Testing

```bash
python -m pytest tests/ -q                # 8,406 tests, none failing; the skips and 5 known limits are marked
python tests/mutation_check.py            # all 300 hand matched guards; edits source, so use a copy
python tests/mutation_check_jobs.py       # the job pipeline's guards, run in a temporary copy
python tests/mutation_check_inventory.py  # 56 inventory guards, each broken on purpose; use a copy
python live_check.py                      # against the real store
cd web && npm test                        # 227 tests, 11 need the S3 stand-in ([docs/web-reports.md](docs/web-reports.md))
cd web && npm run build && npx playwright test    # 18 browser tests in Chromium, Firefox and WebKit
cd web && node scripts/mutation_check.mjs # 133 guards broken one at a time; use a copy
```

Run dates, outputs, skips and the 2,779 syntax tree mutants: [docs/TESTING.md](docs/TESTING.md).

---

Open items, in the order I would do them: [docs/roadmap.md](docs/roadmap.md).
