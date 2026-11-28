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

## Tests

```bash
python -m pytest tests/test_connectwise_connector.py tests/test_autotask_connector.py tests/test_ninjaone_connector.py tests/test_msp_records.py -q    # the connectors against their stand-ins, the records and the ledger, no database
python -m pytest tests/test_msp_sync_db.py -q                                         # through the sync runner into Postgres (docker compose up -d)
python tests/mutation_check_msp.py --only autotask                                    # guards broken one at a time (also connectwise, ninjaone, common, records, ledger, analysis); edits source, so use a copy
```

Each refusal test runs a positive control first (the same call without the defect succeeds). The connector file also runs 40 seeded worlds against a brute force oracle that shares no code with the connector: random tickets and time entries across window edges, time zone offsets, page sizes and transient faults, and every daily figure must match.
