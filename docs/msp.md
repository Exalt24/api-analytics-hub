# MSP service desk and endpoint connectors

An MSP (managed service provider) keeps its tickets in a PSA such as ConnectWise Manage or Autotask and its endpoints in an RMM such as NinjaOne. These connectors read those systems into the hub, and the backlog analysis ranks what an automation backlog is ordered by. What each one was written from, and what its sources leave unstated, is in [MSP_SOURCES.md](MSP_SOURCES.md).

**Honest limits, first.** None of the three vendors has a live account here. Every connector was written from published documentation (ConnectWise: third party descriptions, because its own developer documentation needs a login that refuses a free mailbox address) and tested against a stand-in server on 127.0.0.1. None has run against a live ConnectWise, Autotask or NinjaOne tenant, and nothing here says it has.

## ConnectWise Manage (PSA)

`app/connectors/connectwise.py` reads service tickets and time entries into four daily figures (migration 025) and, for the analysis, into ticket and time records.

| Key | Meaning |
|---|---|
| `cw_tickets_opened` | tickets created that day (UTC day of `_info.dateEntered`) |
| `cw_tickets_closed` | tickets closed that day (UTC day of `closedDate`) |
| `cw_resolution_goals_missed` | tickets whose resolution goal fell on that day and were resolved after it, or are still unresolved when the sync ran (restated by every sync) |
| `cw_time_seconds` | technician time worked that day, in whole seconds |

- Authenticates with the company id and an API member's public and private key (`Authorization: Basic base64(company+publicKey:privateKey)`) and the integration's `clientId`.
- Pages forward-only at 1,000 and follows the `Link` header's `next` URL as given, never off the host or outside the API path.
- Sends no `conditions` or `fields` of its own: the grammar is on no saved page. It reads the lists in full and cuts them to the window; `ticket_conditions` and `time_conditions` pass an operator's own through unchanged.
- States no rate limit, because none of its sources does: it retries a 429 and a 5xx with a bound and hands a long wait to the sync runner.
- The schema says the SLA times in the ticket list may not be current for an active ticket, so `refresh_open_sla=True` re-reads each open ticket by id.

```bash
CONNECTWISE_API_URL=https://na.myconnectwise.net/v4_6_release/apis/3.0 CONNECTWISE_COMPANY_ID=... CONNECTWISE_PUBLIC_KEY=... \
CONNECTWISE_PRIVATE_KEY=... CONNECTWISE_CLIENT_ID=... \
python scripts/sync_connectwise.py --tenant <tenant uuid> --connection <connection uuid> [--ledger tickets.jsonl]
```

## Autotask PSA

`app/connectors/autotask.py` reads tickets and time entries into four daily figures (migration 026) and into records. Written from Autotask's public developer help, saved in [vendor-contract/autotask/](vendor-contract/autotask/).

| Key | Meaning |
|---|---|
| `at_tickets_opened` | tickets created that day (UTC day of `createDate`) |
| `at_tickets_closed` | tickets completed that day (UTC day of `completedDate`) |
| `at_resolution_goals_missed` | tickets whose resolution goal fell on that day and were resolved after it, or are still unresolved when the sync ran (restated by every sync) |
| `at_time_seconds` | technician time worked that day, in whole seconds |

- Asks for the user's zone first (no credential sent), then sends `Username`, `Secret` and `APIIntegrationcode` only to an `autotask.net` API address.
- Queries 500 at a time with `IncludeFields` (the rich text fields are never read) and follows `nextPageUrl` as given, never off the API.
- Reads `ThresholdInformation` once and stops before a call when fewer than 200 of the hour's 10,000 requests are left; sends one request at a time.
- A ticket is closed when it has a `completedDate`: the picklist value for "Complete" is on no saved page. The resolution goal is `resolvedDueDateTime` unless `resolution_due_field="dueDateTime"`.
- A goal missed is counted on the goal's day whenever the ticket was finished, so the ticket query includes tickets whose goal falls in the window as well as the open backlog.

```bash
AUTOTASK_USERNAME=api@example.com AUTOTASK_SECRET=... AUTOTASK_INTEGRATION_CODE=... python scripts/sync_autotask.py --tenant <tenant uuid> --connection <connection uuid> [--ledger tickets.jsonl]
```

## NinjaOne (RMM)

`app/connectors/ninjaone.py` reads devices and active alerts into four figures (migration 027) and into records. Written from NinjaOne's public API documentation, saved in [vendor-contract/ninjaone/](vendor-contract/ninjaone/).

| Key | Meaning |
|---|---|
| `nj_devices` | devices listed when the sync ran (a gauge, written on the window's last day) |
| `nj_devices_offline` | the same, for devices whose `offline` is true (a gauge) |
| `nj_alerts_active` | active alerts when the sync ran (a gauge) |
| `nj_alerts_created` | active alerts whose `createTime` falls on that day (a count for each day of the window) |

- Signs in as the app (client credentials, the `monitoring` scope), renews the token a minute before it expires and on a 401, once, behind a lock.
- Reads devices by `after` until a page is empty, and refuses ids that do not rise. The alert list is one answer: the OpenAPI file gives it no paging.
- The timestamp unit is a required setting (`epoch_unit`): NinjaOne's own file uses seconds for some fields and milliseconds for others and says nothing for `createTime`. Check one alert, then set it.
- Only ACTIVE alerts are listed, so an alert reset before the sync is never seen, and the three gauges cannot be restated for a past day.

```bash
NINJAONE_CLIENT_ID=... NINJAONE_CLIENT_SECRET=... NINJAONE_EPOCH_UNIT=seconds python scripts/sync_ninjaone.py --tenant <tenant uuid> --connection <connection uuid> [--ledger devices.jsonl]
```

## The backlog analysis

`scripts/msp_backlog.py analyze` reads the ledgers the three sync scripts write with `--ledger` (one JSON line per ticket, time entry, device or alert, in one vocabulary whatever the vendor), merges them, and ranks the three things an automation backlog is ordered by. It reads files only and calls no vendor; `app/msp/analysis.py` holds the arithmetic, and the same ledger always gives the same report.

```bash
python scripts/sync_connectwise.py --tenant <tenant> --connection <connection> --ledger cw.jsonl      # likewise sync_autotask.py and sync_ninjaone.py
python scripts/msp_backlog.py analyze cw.jsonl at.jsonl nj.jsonl [--as-of 2026-11-29T12:00:00Z] [--window-days 30] [--horizon-hours 4] [--top 10] [--format markdown|json] [--out report.md]
python scripts/msp_backlog.py sample --seed 7 --out sample.jsonl                                      # a GENERATED ledger, no client data
```

| What | How it is measured |
|---|---|
| **Ticket drivers** | The tickets opened in the window, grouped by category, issue type and a normalised title (lower case, every run of digits one `#`, so "Disk space low on SRV-042" and "disk space low on srv-17" are one driver). A **repeat** is a ticket after the first for the same company in its group; a ticket with no company is never a repeat. Ranked by repeats, then tickets, then name; also by category and issue type with each one's share of all tickets opened. |
| **Alert drivers** | The active alerts grouped by source type and source name, with the devices behind them and the **repeats** (alerts beyond the first that one device raised from the same source), and the devices that raised most. Only active alerts exist in NinjaOne's list, so this describes what is still open. |
| **SLA risk** | The tickets **open at `--as-of`** (not closed and not resolved by then; so an old ledger can be judged at an earlier time) sorted by their resolution goal: `breached` (the goal is before as_of), `at_risk` (it falls inside the horizon, 4 hours by default), `ok`, or `no_goal` (the vendor gave none, which is never called fine). Grouped by category and priority, ranked by breached, then at risk, then open, with the first responses overdue and the oldest open ticket in whole hours; also the tickets finished in the window that finished after their goal, and opened against finished per category. |
| **Technician touch time** | The window's time entries spread over the category of the ticket each was worked on (matched by vendor and ticket number), with the technicians under each category, tickets touched, the mean seconds per ticket (rounded down) and the ticket that took most. Time on no ticket, and time on a ticket the ledger does not hold, is reported as its own total and never spread over the categories. |

A window is half open on UTC days; a percentage is a whole number rounded down. "At risk" is a count of open tickets near their goal, not a forecast: the analysis has no arrival or staffing model and does not weigh one priority above another, because the order of a priority is on no saved vendor page. The report ends with what it cannot see, written from the vendors present (ConnectWise's list goals may be stale for open tickets, Autotask's closed means completed, NinjaOne's alerts are active ones only).

[msp-backlog-sample.md](msp-backlog-sample.md) is the output over a generated world (`sample --seed 7`, 400 tickets, 710 time entries, 45 alerts, 30 devices; made-up companies, technicians and titles), with the top 5 of each ranking.

## Tests

```bash
python -m pytest tests/test_connectwise_connector.py tests/test_autotask_connector.py tests/test_ninjaone_connector.py tests/test_msp_records.py -q    # the connectors against their stand-ins, the records and the ledger, no database
python -m pytest tests/test_msp_analysis.py tests/test_msp_backlog_cli.py -q                    # the analysis against a brute force oracle, the command line, and the path from three stand-ins to a report
python -m pytest tests/test_msp_sync_db.py -q                                         # through the sync runner into Postgres (docker compose up -d)
python tests/mutation_check_msp.py --only autotask                                    # guards broken one at a time (also connectwise, ninjaone, common, records, ledger, analysis); edits source, so use a copy
```

Each refusal test runs a positive control first (the same call without the defect succeeds). The connector file also runs 40 seeded worlds against a brute force oracle that shares no code with the connector: random tickets and time entries across window edges, time zone offsets, page sizes and transient faults, and every daily figure must match.
