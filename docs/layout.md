# Layout

Where things live. The run steps are in the [README](../README.md).

```
app/
  connectors/        one module per platform + the shared contract
    base.py          Connector protocol, CanonicalPoint, error taxonomy
    shopify.py       Admin GraphQL: pacing, day bucketing, money, degradation
    ga4.py           GA4 Data API runReport: daily rows, paging, bounded retries, typed errors
    ga4_fake.py      stand-in GA4 server on 127.0.0.1 for the tests and the sample
    meta.py          Meta Marketing API insights: daily spend, impressions, clicks, purchases in the ad account's currency
    meta_fake.py     stand-in Meta server, written from Meta's pages
    meta_creatives.py       Meta ad creative reader: every ad's copy, links, video and preview across campaigns, paged and batched (docs/meta-creatives.md)
    meta_creatives_fake.py  stand-in server for it, written from Meta's pages, with a small sample account
    gsc.py           Search Console searchAnalytics.query: daily figures and each day's top queries and pages
    gsc_fake.py      stand-in Search Console server, written from Google's pages
    common.py        what the five newest connectors share: integer minor units, half up rounding, the bounded retry loop
    fake_http.py     the threaded stand-in server base, request log and scripted faults
    google_ads.py    Google Ads googleAds:search (GAQL): daily cost, clicks, conversions, optional Local Services leads
    tiktok.py        TikTok Business API synchronous basic report: daily advertiser figures in 30 day runs
    gbp.py           Google Business Profile Performance API: daily listing impressions, calls, website clicks, direction requests, messages, bookings
    square.py        Square Payments API: completed payments per location day, refunds, tip included
    quickbooks.py    QuickBooks Online query endpoint: invoices by invoice date, total and open balance
    shopmonkey.py    Shopmonkey v3 orders: work orders booked and invoiced per day
    salesforce.py    Salesforce REST SOQL: leads and opportunities per day, OAuth refresh token flow, Sforce-Limit-Info pacing, the lead rows for matching
    salesforce_fake.py   stand-in Salesforce server, written from Salesforce's pages
    harvest.py       Harvest v2 time entries: daily hours, billable hours, entries and billable value, project budgets in hours and a budget status per project
    msgraph.py       Microsoft Graph as the app itself (client credentials): a SharePoint list register and a Teams channel per day, and an Excel register read from the downloaded .xlsx
    harvest_fake.py, msgraph_fake.py   stand-in servers, written from Harvest's and Microsoft's pages (docs/HARVEST_GRAPH_SOURCES.md)
    google_ads_fake.py, tiktok_fake.py, square_fake.py, quickbooks_fake.py, shopmonkey_fake.py   stand-in servers, written from each vendor's pages
    shopify_oauth.py install flow, offline tokens
    shopify_webhooks.py  HMAC verification over the raw body
    tokens.py        refresh behind one lock, absolute expiry
    registry.py      per-platform auth kind and rate-limit style
  secret_files.py    where the live scripts read their token files from
  crypto.py          AES-256-GCM credential encryption, AAD bound to the row
  db.py              pool, tenant-scoped transactions, RLS role assertion
  ai/catalog_copy.py LLM meta descriptions that must pass a number-grounding check
  sync/runner.py     claim, lease, heartbeat, resume, finish
  sync/cli.py        the shared body of the sync commands for one connection
  api/               FastAPI, bearer auth, RBAC, request log and /metrics
    reports.py       the monthly report as an HTTP call: build, fetch the deck, mark delivered, all idempotent
  obs.py             JSON log formatter, request ids, Prometheus text page
  health.py          stuck-sync gauges and the alert policy
  backup.py          backup manifest, restore proof, row count window and the backup alert problems (pure)
  changes.py         daily snapshot compare, events, company matching, alert outbox
  changes_fake_sink.py  stand-in CRM sinks for tests
  report/            monthly report: KPI math, website (web.py), ads (paid.py), search (search.py), grounded narrative, PowerPoint deck
    sources.py       what the ad and search halves share: provisional days, the KPI table builder, headline wording
    runs.py          the record of each report a tenant asked for: one deck for each key, delivered flag, takeover of a dead run
    adspend.py       the combined ad spend view: cost per conversion by platform and week, booked jobs beside it, text and JSON
    adspend_service.py  reads a tenant's ad and booking snapshots for the view
    drive.py         the Google Drive delivery step: Drive API v3 multipart and resumable upload, a stable file name so a rerun replaces, retries, token renewal (docs/drive-delivery.md)
scripts/check_sync_health.py   cron entry point that alerts a Slack webhook
scripts/db_backup.py           pg_dump with a manifest, a scratch restore that compares row counts, and the backup alert
scripts/monthly_report.py      builds the monthly deck from stored snapshots: store, website, ads and search; with --drive-folder it then delivers the deck to Google Drive
scripts/sync_ga4.py, sync_meta.py, sync_gsc.py   pull one property's, ad account's or site's daily figures into the snapshots
scripts/sync_google_ads.py, sync_tiktok.py, sync_square.py, sync_quickbooks.py, sync_shopmonkey.py   the same for a Google Ads customer, TikTok advertiser, Square location, QuickBooks company or Shopmonkey shop
scripts/ad_spend_report.py   prints the ad spend view (text or JSON) for a tenant and a window
scripts/meta_creatives.py   writes a CSV and a JSON file of an ad account's creatives (`--stand-in` for the local run)
n8n/                 the monthly run: workflow JSON, its generator, and the script that runs it in a real n8n container
scripts/make_carrier_snapshot.py, load_carrier_snapshot.py, measure_change_detection.py
migrations/          001 core + RLS, 002 control plane, 003 API keys, 004 sync health, 005 change detection, 006 GA4 metric keys, 007 Meta metric keys and the money check, 008 search metrics and top rows, 009 report runs, 010 search query vectors (pgvector, HNSW, row level security), 011 price watch (catalogue, competitor listings and scans, shipping rates, report, review queue and audit trail), 012 billing (double-entry ledger, wallets, payments, subscriptions, webhook events; balanced, append only, forced row level security), 013 the metric keys of Google Ads, TikTok, Square, QuickBooks and Shopmonkey with the money check and the milli_count unit, 014 Business Profile metric keys, 015 report presets sync, 016 e-signatures (signing requests, signatures and a hash chained audit log, forced row level security, append only), 020 inventory planning (suppliers, variants, dated stock snapshots, order lines, promotions, the stored plan, alerts and their outbox; forced row level security, alerts append only), 021 purchase orders (orders, lines, events, late alerts; a trigger holds the legal status moves), 022 the four role access matrix (clinics, portal users, rep assignments, the 80 cell table and the functions every policy asks), 023 bank debit payments, invoices, their refunds and sales rep commissions, 024 partner fulfilment (orders as opaque references, the send and its dead letter, partner events, a hash chained audit log)
docs/GA4_SOURCES.md, META_SOURCES.md, GSC_SOURCES.md, AD_AND_BUSINESS_SOURCES.md   what each vendor's pages said, and what is UNVERIFIED
docs/ad-spend.md   the five connectors, their env variables, the ad spend view, its rules and limits
seed_demo.py         invented figures and two API keys, for anyone without the owner's Shopify store
deploy/              Caddyfile, systemd units for the nightly backup and its alert, and the script that verifies them (docs/deploy.md)
app/lead_match.py    lead dedupe, attribution to a call and a source, and the missed call queue, each answer with a reason code (docs/salesforce.md)
app/similar_queries.py hashed-trigram embedding and the tenant-scoped nearest-query lookup (docs/test-runs/2026-10-30-pgvector-rls-recall.md)
app/inventory/       inventory planning: stock per variant as dated snapshots, stockout adjusted velocity, safety stock, reorder point, cover, classes, alerts (docs/inventory-planning.md)
  model.py           the rules as pure functions: which days were on sale, velocity and its variance, safety stock, reorder point, order quantity, ABC classes, labels
  reader.py          Shopify Admin GraphQL read of variants, stock levels and order lines through the existing connector's transport (queries only)
  store.py           database side as the request role: variants, snapshots, order lines, suppliers, promotions, the stored plan
  sync.py            one tenant's sync: read the shop, then store the day's snapshots and the order lines in one transaction
  alerts.py          the shortage kinds and the late order alert, each raised once behind a UNIQUE key, the webhook sink and the digest of what is open (app.health.decide)
  orders.py          the purchase order rules as pure functions: the transition table, receipts, inbound stock, when an order is late
  order_store.py     purchase orders as the request role: a draft from the plan, moves, expected date, receipts, the late alert (the rules are in migration 021)
  workbook.py        the plan as an Excel workbook whose figures are live formulas (Inputs, Plan, Classes, Scenario)
  workbook_check.py  recalculates that workbook in LibreOffice (or the formulas package) and compares every computed cell with the Python plan
app/api/inventory.py   the order routes and the workbook download (read:inventory, write:inventory)
migrations/021_purchase_orders.sql   orders, lines, events, late alerts and their outbox; a trigger holds the transition table; four functions are the operations
scripts/inventory_plan.py   sync, supplier, assign, promo, plan, order commands, workbook, alerts and digest from the command line
scripts/inventory_live_check.py, inventory_orders_run.py, inventory_dev_store.py   the recorded runs against the development store, and the test data they need (created and removed by the script)
scripts/inventory_workbook_check.py   the workbook recalculation check on a stored plan
web/app/inventory/, web/components/InventoryPanel.tsx, PurchaseOrders.tsx, web/lib/inventory/   the Inventory page: exceptions, planning table, purchase order drafts as CSV, Excel or a Sheets paste, purchase orders, the workbook download, promotion form
tests/test_inventory_model.py, test_inventory_reader.py, test_inventory_alerts.py, test_inventory_db.py, test_inventory_orders_*.py, test_inventory_workbook.py, inventory_fixtures.py, mutation_check_inventory.py   the inventory tests and their mutation harness
tests/test_meta_creatives.py, test_meta_creatives_oracle.py, test_meta_creatives_fake_server.py, test_meta_creatives_cli.py, meta_creatives_world.py, mutation_check_creatives.py   the ad creative tests: examples, seeded random accounts against a brute force oracle, the stand-in's own rules, the CLI, and the mutation harness
app/price_watch/     competitor price monitoring (docs/price-watch.md)
  scraper.py         Playwright reader for one competitor page, and the pure extraction over what it collects
  fake_site.py       eight invented competitor pages on 127.0.0.1, for the tests and the demo
  money.py           price text to exact minor units
  matching.py        catalogue to competitor listing: GTIN, SKU, title score, review list, ties
  shipping.py        stated shipping against an estimate from the tenant's rate table, labelled ESTIMATED
  report.py          the daily report of lower landed prices, floor respected, markdown and CSV
  store.py           database side: catalogue, scans, matches, the stored report
  proposals.py       the review queue: legal moves, audit rows, approve and reject
  adapters.py        Shopify productVariantsBulkUpdate and Zoho Inventory item update, retries, token refresh
  sync.py            send an approved proposal; dry run by default; retries only the failed shop
  fakes.py           fake Shopify and Zoho servers (httpx transports) for the sync tests
  demo.py            invented products, listings and shipping rates
scripts/price_watch.py   scan, report, proposals, listings, approve, reject, confirm-match, sync and demo-server
seed_price_watch_demo.py invented products and competitor pages for the price watch loop
docs/price-watch.md  the loop, how to run it, what is not done; docs/vendor-contract/ the Shopify and Zoho pages the adapters are written from
app/billing/         prepaid wallet and billing ledger; the provider is the official stripe SDK or a hand written client, tested against stripe-mock and a stand-in (docs/billing.md)
  ledger.py          accounts, the balanced transaction, the wallet lock, request hashes
  wallet.py          top-up, usage debit at the tenant's own price, refund, service credit, admin adjustment: idempotent, never below zero
  payments.py        ask the provider for money, record failures, settle, request and reverse refunds
  provider.py        hand written Stripe-style client over an injected httpx client: the same idempotency key on every retry, card errors not retried
  stripe_sdk.py      the same provider interface on the official stripe SDK, and the webhook check through stripe.Webhook.construct_event
  fake_provider.py   fake Stripe as an httpx transport written from the saved pages, with scriptable failures, and the signed events it would send
  signature.py       Stripe-Signature verification: t and v1, HMAC SHA256, constant time, rotation, recency window
  webhooks.py        verify, route through a read only role, write each event once in the ledger's transaction, hold what arrives early
  subscriptions.py   plan state from invoice and subscription events, plan changes
  proration.py       mid period proration in integers, half up
  reports.py         statement, daily revenue and usage, reconciliation; markdown and CSV
  demo.py            invented plans, two tenants and a week of traffic
scripts/billing.py   price, topup, debit, refund, adjust, statement, report, reconcile, simulate-webhook and demo
seed_billing_demo.py plans and two invented tenants for the billing demo
app/access/         the access matrix: one table (matrix.py), the API guard and the credential lookup (principal.py), and the two tools the engine uses to act for a user and as itself (engine.py) (docs/access-matrix.md)
app/fulfillment/    partner fulfilment: the order state machine, the send with retries and a dead letter, the signed status webhook, the hash chained audit log (docs/fulfillment.md)
app/billing/invoices.py, commissions.py   invoices paid from the wallet, by card or by bank debit, refunds, and the sales rep's commission (docs/billing.md)
app/api/portal.py   the routes under /api/portal: orders, invoices, payments, commissions and the partner webhook
docs/billing.md      the ledger, the flows, how a duplicate charge is prevented, the demo, what is not done; docs/vendor-contract/stripe/ the Stripe pages the provider is written from
app/esign/           e-signature slice: a signing request, a signature record and a hash chained audit log, tested against a local Postgres (docs/esign.md)
  service.py         create, open, sign, decline, expire, verify and the certificate; one transaction for each command, ordered by an advisory lock on the request
  chain.py           the hashes and every check on them (pure): an edited, deleted or reordered row, a changed document, an edited signature
  consent.py         the versioned consent text and the typed-name rule
  certificate.py     the completion certificate as JSON and text, and the offline check a third party can run
app/api/esign.py     the routes under /api/esign: who may create, open, sign, decline and verify, and the status of each refusal
app/jobs/            the job pipeline: vendor postings in, one row per job, a tailored application, a form filler that asks when it is not sure (docs/job-pipeline.md)
  dedupe.py, archive.py, store.py, embed.py, pipeline.py   the Redis sliding window, raw JSON to S3 or R2, the Postgres upserts, OpenAI embeddings into Qdrant through LangChain, ingest_batch()
  tailor.py, render.py, llm.py, tech_lexicon.py            the resume and cover letter loop (ATS score, grounding), DOCX and PDF, the structured-output model call
  formfill.py, browser.py, hitl.py, worker.py, notion.py   the confidence mapper with Stagehand as a fallback, Playwright from local Chromium or Browserbase, the review queue and resume, the RabbitMQ worker, the Notion board
  fake_vendors.py, fake_openai.py, fake_ats.py, fake_browserbase.py, fake_notion.py   the stand-in servers the tests and the n8n run use, written from each service's pages (docs/job-vendors.md)
app/api/jobs.py      the routes under /api/jobs: ingest, list and search, apply, the review queue, answer, send to manual review
migrations/019_job_pipeline.sql   postings, ingest runs, the applicant's facts, tailored documents, application runs and review tasks: forced row level security, a token only while waiting
n8n/job_ingest.workflow.json, job_apply.workflow.json, build_job_workflow.py, verify_job_pipeline_in_docker.py   the hourly pull and the apply-with-review workflows, their generator and the run in a real n8n
n8n/jobs/            normalize.js (the vendor mappers and ATS detection), pull.js (nine vendor pulls), pull_cli.js, and their node tests
scripts/job_pipeline_live_check.py   the parts run against a real service: Upstash Redis, OpenAI embeddings, a chat model
migrations/016_esign.sql   the three tables: forced row level security, append only, a frozen request, one signature for each request
web/lib/esign.ts, web/app/portal/[slug]/sign/   the portal page where the client reads, signs or declines, and the portal route that serves the document
web/                 Next.js dashboard
  app/reports/       the Reports page and its Server Actions (CSV, Excel, PDF, presigned upload)
  lib/server/        Kysely database layer with the tenant setting and role switch, API key to tenant, the TypeScript port of the query embedding and lookup
  lib/report/        month data, CSV, Excel and PDF writers, the Zod input schemas, the figure guard, the AI summary and its template fallback
  lib/s3/            presigned upload and read links, and the upload request
  components/ui/     shadcn/ui style components on Radix primitives; ReportForm.tsx is the export form
  tests/             Vitest suites (database, formats, guard, summary, S3, search, compiler, mutation script)
  e2e/               Playwright test and the Anthropic stand-in server; scripts/mutation_check.mjs breaks 65 guards; lint/ holds ESLint (docs/web-reports.md)
powerbi/             the Power BI report (docs/powerbi.md)
  AnalyticsHub.pbix  the report with its data inside; AnalyticsHub.pbip, .SemanticModel/ (TMDL) and .Report/ (PBIR) are the same report as text
  data/              the four CSV files the model reads, written by export_data.py
  export_data.py     invented figures through the hub's migrations and upsert function, read back out as CSV; --check needs no database
  check_model.py     opens a copy of the .pbix in Desktop and compares the measures with figures worked out from the CSV files; run_dax.ps1 is its query runner
tests/               the test suite, the mutation harness and derived_guards.py (guards derived from the syntax tree)
lakehouse/           PySpark and Delta Lake medallion pipeline over the hub's daily figures (docs/lakehouse.md, lakehouse/RUNBOOK.md)
  bronze.py, silver.py, gold.py, quality.py   append only landing with quarantine, typed MERGE upserts, the SQL built model, the quality flags
  sql/               one Spark SQL file per gold table (three dimensions, a date table, the fact, two reports)
  pipeline.py, run.py, runlog.py, costs.py    the run, its command line, the run log table, the cost ranking
  stand_in.py, rest_client.py                 stand-in Salesforce style REST server and the paged reader for it
  tuning.py          the query tuning benchmark; tests/ holds the stage tests, the seeded property test and mutation_check.py
```
