# Layout

Where things live. The run steps are in the [README](../README.md).

```
app/
  connectors/        one module per platform + the shared contract
    base.py          Connector protocol, CanonicalPoint, error taxonomy
    shopify.py       Admin GraphQL: pacing, day bucketing, money, degradation
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
  report/            monthly report: KPI math, grounded narrative, PowerPoint deck
scripts/check_sync_health.py   cron entry point that alerts a Slack webhook
scripts/monthly_report.py      builds the monthly deck from stored snapshots
scripts/make_carrier_snapshot.py, load_carrier_snapshot.py, measure_change_detection.py
migrations/          001 core + RLS, 002 control plane, 003 API keys, 004 sync health, 005 change detection
web/                 Next.js dashboard
tests/               the test suite and the mutation harness
```
