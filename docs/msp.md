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

## Tests

```bash
python -m pytest tests/test_connectwise_connector.py tests/test_msp_records.py -q    # the connector against its stand-in, the records and the ledger, no database
python -m pytest tests/test_msp_sync_db.py -q                                         # through the sync runner into Postgres (docker compose up -d)
python tests/mutation_check_msp.py --only connectwise                                 # guards broken one at a time; edits source, so use a copy
```

Each refusal test runs a positive control first (the same call without the defect succeeds). The connector file also runs 40 seeded worlds against a brute force oracle that shares no code with the connector: random tickets and time entries across window edges, time zone offsets, page sizes and transient faults, and every daily figure must match.
