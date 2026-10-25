# Layout

Where things live. The run steps are in the [README](../README.md).

```
app/
  connectors/        one module per platform + the shared contract
    base.py          Connector protocol, CanonicalPoint, error taxonomy
    shopify.py       Admin GraphQL: pacing, day bucketing, money, degradation
    ga4.py           GA4 Data API runReport: daily rows, paging, bounded retries, typed errors
    ga4_fake.py      stand-in GA4 server on 127.0.0.1 for the tests and the sample
    shopify_oauth.py install flow, offline tokens
    shopify_webhooks.py  HMAC verification over the raw body
    tokens.py        refresh behind one lock, absolute expiry
    registry.py      per-platform auth kind and rate-limit style
  secret_files.py    where the live scripts read their token files from
  crypto.py          AES-256-GCM credential encryption, AAD bound to the row
  db.py              pool, tenant-scoped transactions, RLS role assertion
  ai/catalog_copy.py LLM meta descriptions that must pass a number-grounding check
  sync/runner.py     claim, lease, heartbeat, resume, finish
  api/               FastAPI, bearer auth, RBAC, request log and /metrics
  obs.py             JSON log formatter, request ids, Prometheus text page
  health.py          stuck-sync gauges and the alert policy
  changes.py         daily snapshot compare, events, company matching, alert outbox
  changes_fake_sink.py  stand-in CRM sinks for tests
  report/            monthly report: KPI math, website figures (web.py), grounded narrative, PowerPoint deck
scripts/check_sync_health.py   cron entry point that alerts a Slack webhook
scripts/monthly_report.py      builds the monthly deck from stored snapshots, store and website
scripts/sync_ga4.py            pulls one GA4 property's daily figures into the snapshots
scripts/make_carrier_snapshot.py, load_carrier_snapshot.py, measure_change_detection.py
migrations/          001 core + RLS, 002 control plane, 003 API keys, 004 sync health, 005 change detection, 006 GA4 metric keys
docs/GA4_SOURCES.md  what Google's pages said about runReport, and what is UNVERIFIED
web/                 Next.js dashboard
tests/               the test suite and the mutation harness
```
