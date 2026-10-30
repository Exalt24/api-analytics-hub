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
migrations/          001 core + RLS, 002 control plane, 003 API keys, 004 sync health, 005 change detection, 006 GA4 metric keys, 007 Meta metric keys and the money check, 008 search metrics and top rows, 009 report runs
docs/GA4_SOURCES.md, META_SOURCES.md, GSC_SOURCES.md   what each vendor's pages said, and what is UNVERIFIED
web/                 Next.js dashboard
tests/               the test suite, the mutation harness and derived_guards.py (guards derived from the syntax tree)
```
