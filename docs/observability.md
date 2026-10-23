# Observability

*One JSON log line per request, a Prometheus `/metrics` page, and an alert on syncs that are stuck or have gone stale.*

## What is logged

`app/obs.py` writes one JSON object per line to stdout. A request line carries the method, the route template (`/api/kpis`, never a path with an id in it), the status, the duration, a request id and the tenant id of an authenticated caller. It never carries a header, a query string, a body or a token: the formatter only passes fields from an allow list, so a secret handed to the logger by mistake is dropped. An exception is logged as its class name, because the message of a driver error can hold a connection string. A caller's `X-Request-ID` is reused only when it is 8 to 64 plain ASCII characters, otherwise a new one is made, so a header cannot inject a line into a log. The response carries the id back.

## What `/metrics` reports

- `hub_http_requests_total{method,route,status}` and `hub_http_request_seconds` (a histogram per route). These are per process, so run one scrape per instance.
- `hub_sync_problems{platform,kind}`, `hub_sync_problems_total` and `hub_sync_oldest_problem_age_seconds`, read from the database at scrape time.
- `hub_sync_runs_24h{platform,status}`.
- `hub_scrape_ok`: 1 when the database gauges were read, 0 when they could not be. A failed read serves the request counters, omits the sync gauges and says 0. It is never shown as a row of zeros, which would look like a healthy system.

No series is labelled with a tenant, a shop domain or a connection id. Set `METRICS_TOKEN` to require `Authorization: Bearer <token>` on the page.

The gauges come from a second small pool opened as the `sync_worker` role, which crosses tenants by design. The request pool stays `app_request`, and a test connects as a plain login in `app_request` and checks that it cannot `SET ROLE sync_worker`.

## What counts as stuck

`sync_problems()` in `migrations/004_observability.sql` names three kinds, each of which fails silently if nobody looks:

| Kind | Meaning |
|---|---|
| `expired_lease` | a run still says `running` but its lease ran out: the worker stopped heartbeating |
| `never_succeeded` | an active connection with no successful run, older than its window |
| `stale` | the last success is older than three times the connection's own cadence (at least 15 minutes), or the default of six hours for a connection with no schedule |

A paused connection and a connection too new to have run are not reported.

## The alert

```bash
DATABASE_URL=postgresql://... SLACK_WEBHOOK_URL=https://hooks.slack.com/... \
  python scripts/check_sync_health.py --state /var/lib/hub/alert_state.json
```

Run it from cron. Exit 0 means nothing is wrong, 1 means problems exist, 2 means the check or the delivery failed, so a broken check never reads as healthy. It posts to Slack when the set of problems changes, repeats an unchanged set after 6 hours (`--repeat-seconds`), and sends one recovery message when the set clears. A failed delivery keeps the old state, so the next run tries again. The webhook URL is read from the environment and never printed or logged.

## Limits

- Sent to a Slack incoming webhook only. There is no paging service, no on-call rota and no escalation if nobody reads the channel.
- The request metrics are in memory and reset on restart. With several processes each one reports its own counts.
- Nothing here is a trace. A slow request shows up as a latency bucket and a log line, not as a breakdown of where the time went.
- The alert is as fresh as its cron interval. It was run against a seeded local Postgres and a local fake Slack, not against a real Slack workspace or a production database.
