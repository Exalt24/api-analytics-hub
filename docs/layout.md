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
    gsc.py           Search Console searchAnalytics.query: daily figures and each day's top queries and pages
    gsc_fake.py      stand-in Search Console server, written from Google's pages
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
scripts/check_sync_health.py   cron entry point that alerts a Slack webhook
scripts/db_backup.py           pg_dump with a manifest, a scratch restore that compares row counts, and the backup alert
scripts/monthly_report.py      builds the monthly deck from stored snapshots: store, website, ads and search
scripts/sync_ga4.py, sync_meta.py, sync_gsc.py   pull one property's, ad account's or site's daily figures into the snapshots
n8n/                 the monthly run: workflow JSON, its generator, and the script that runs it in a real n8n container
scripts/make_carrier_snapshot.py, load_carrier_snapshot.py, measure_change_detection.py
migrations/          001 core + RLS, 002 control plane, 003 API keys, 004 sync health, 005 change detection, 006 GA4 metric keys, 007 Meta metric keys and the money check, 008 search metrics and top rows, 009 report runs, 010 search query vectors (pgvector, HNSW, row level security), 011 price watch (catalogue, competitor listings and scans, shipping rates, report, review queue and audit trail), 012 billing (double-entry ledger, wallets, payments, subscriptions, webhook events; balanced, append only, forced row level security)
docs/GA4_SOURCES.md, META_SOURCES.md, GSC_SOURCES.md   what each vendor's pages said, and what is UNVERIFIED
seed_demo.py         invented figures and two API keys, for anyone without the owner's Shopify store
deploy/              Caddyfile, systemd units for the nightly backup and its alert, and the script that verifies them (docs/deploy.md)
app/similar_queries.py hashed-trigram embedding and the tenant-scoped nearest-query lookup (docs/test-runs/2026-10-30-pgvector-rls-recall.md)
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
app/billing/         prepaid wallet and billing ledger against a fake Stripe-style provider (docs/billing.md)
  ledger.py          accounts, the balanced transaction, the wallet lock, request hashes
  wallet.py          top-up, usage debit at the tenant's own price, refund, service credit, admin adjustment: idempotent, never below zero
  payments.py        ask the provider for money, record failures, settle, request and reverse refunds
  provider.py        Stripe-style client over an injected httpx client: the same idempotency key on every retry, card errors not retried
  fake_provider.py   fake Stripe as an httpx transport written from the saved pages, with scriptable failures, and the signed events it would send
  signature.py       Stripe-Signature verification: t and v1, HMAC SHA256, constant time, rotation, recency window
  webhooks.py        verify, route through a read only role, write each event once in the ledger's transaction, hold what arrives early
  subscriptions.py   plan state from invoice and subscription events, plan changes
  proration.py       mid period proration in integers, half up
  reports.py         statement, daily revenue and usage, reconciliation; markdown and CSV
  demo.py            invented plans, two tenants and a week of traffic
scripts/billing.py   price, topup, debit, refund, adjust, statement, report, reconcile, simulate-webhook and demo
seed_billing_demo.py plans and two invented tenants for the billing demo
docs/billing.md      the ledger, the flows, how a duplicate charge is prevented, the demo, what is not done; docs/vendor-contract/stripe/ the Stripe pages the provider is written from
web/                 Next.js dashboard
tests/               the test suite, the mutation harness and derived_guards.py (guards derived from the syntax tree)
```
