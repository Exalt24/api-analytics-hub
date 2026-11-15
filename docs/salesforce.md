# Salesforce and lead matching

The hub reads a Salesforce org the same way it reads every other platform: a connector translates, the runner stores dated snapshots, and the dashboard never calls Salesforce. This page is how to run it and what it does and does not prove. The documentation each rule came from, with quotes and what is unconfirmed, is the Salesforce section of [AD_AND_BUSINESS_SOURCES.md](AD_AND_BUSINESS_SOURCES.md).

**Status: written from Salesforce's public documentation and tested only against a stand-in server.** No Salesforce org is held, so nothing here claims a live org. The stand-in (`app/connectors/salesforce_fake.py`) speaks real HTTP on 127.0.0.1, understands the SOQL the connector sends, and refuses what the pages say Salesforce refuses (a quoted datetime literal, an unknown field, a bad token, a used up daily limit).

## What it writes

Five daily keys per connection (migration `017_salesforce_metrics.sql`): `sf_leads_created`, `sf_leads_converted`, `sf_opportunities_created`, `sf_opportunities_won` and `sf_won_amount` (money, in the connection's currency). A created day is the UTC day; a converted or close day is the date as Salesforce writes it.

    DATABASE_URL=... SALESFORCE_INSTANCE_URL=https://yourname.my.salesforce.com SALESFORCE_CURRENCY=USD \
    SALESFORCE_CLIENT_ID=... SALESFORCE_CLIENT_SECRET=... SALESFORCE_REFRESH_TOKEN=... \
    python scripts/sync_salesforce.py --tenant <uuid> --connection <uuid>

## How it behaves

- **OAuth 2.0 refresh token flow.** The secrets go in the POST body, never the query string. The answer has no expiry, so a 401 refreshes once, behind a lock (simultaneous refreshes with one token fail), and the request is repeated once. A rotated refresh token is handed to the caller. `invalid_grant`, `inactive_user` and `invalid_client*` mean a person must reconnect.
- **SOQL paging.** Each `nextRecordsUrl` is followed as given. One that is not a path under `/services/data/` is refused, so the token cannot be sent to another host. `totalSize` must match what arrived.
- **Describe first.** Lead and Opportunity are described once: a field the user cannot see is named before any query runs, and a `CurrencyIsoCode` field marks a multi-currency org, where a row in another currency is refused and never added in.
- **Limits.** `Sforce-Limit-Info` is read from every answer and the sync stops before a call when fewer than 50 of the org's daily calls are left. A 403 `REQUEST_LIMIT_EXCEEDED` is handed back at once; waiting a minute does not refill a daily limit.

## Matching a lead to a call and a source

`SalesforceConnector.leads()` returns Lead rows (Id, CreatedDate, Email, Phone, MobilePhone, LeadSource, IsConverted). `app/lead_match.py` turns those and a list of calls into answers with a reason code on each:

| Step | Rule | Reason codes |
|---|---|---|
| dedupe | the same normalised email, or a phone held by one email at most, is one person; the oldest lead survives; a phone behind two different emails is a shared line and merges nobody | `SURVIVOR`, `EMAIL`, `PHONE`, `EMAIL_AND_PHONE`, `TRANSITIVE` |
| attribute | a call from the person's number in the 14 days before their first lead is the first touch and wins; a shared line never attributes anyone | `CALL_BEFORE_LEAD`, `LEAD_SOURCE`, `CALL_SOURCE_MISSING`, `AMBIGUOUS_NUMBER`, `NO_SOURCE` |
| work queue | a missed call nobody has answered since, oldest first | `MISSED_CALL_NO_LEAD`, `MISSED_CALL_OPEN_LEAD`, `MISSED_CALL_NUMBER_UNUSABLE` |

It never writes back to Salesforce: the clusters are a report for the administrator, who owns the merge. It matches phone numbers on digits with an explicit default country code and never on a name. Address matching is a different problem and lives in [parcelkey](https://github.com/Exalt24/parcelkey).

## Tests

`tests/test_salesforce_connector.py` (the connector against the stand-in, 60 seeded worlds recounted independently), `tests/test_lead_match.py` (150 seeded worlds with planted people, shuffled input), `tests/test_salesforce_sync_db.py` (the runner into real Postgres with row level security) and `tests/mutation_check_salesforce.py`, which breaks 58 guards one at a time and demands that the named test turns red.
