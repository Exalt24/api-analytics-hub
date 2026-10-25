# GA4 sources

What the Google Analytics 4 connector (`app/connectors/ga4.py`) and its stand-in server (`app/connectors/ga4_fake.py`) were written from. Every page below was loaded in a real browser on 2026-10-25 and saved as text; the quote under each fact is copied from the saved page. A fact the pages did not give is marked UNVERIFIED here and in the code, and it stays that way until a real property answers.

No live property was used. The connector has only run against the stand-in, so nothing here is a statement that Google accepts these requests. The stand-in is written from these pages, and it refuses what they say Google refuses (tests/test_ga4_fake_server.py).

## The call

**One method.** runReport, a POST with the property in the path. Page: https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport (last updated 2026-04-23).
"POST https://analyticsdata.googleapis.com/v1beta/{property=properties/*}:runReport"

**The property id is a number.** Page: https://developers.google.com/analytics/devguides/reporting/data/v1/property-id (2026-10-22).
"API requests are required to specify a numeric Google Analytics property identifier."
The page says to find it under Admin, Property Settings, as "PROPERTY ID". The connector accepts `123` or `properties/123` and refuses anything else before a request.

**The request body.** Same runReport page. Fields used: `dateRanges`, `dimensions`, `metrics`, `orderBys`, `limit`, `offset`, `returnPropertyQuota`.
"limit ... string (int64 format)" and "offset ... string (int64 format)", so both are sent as strings.
"returnPropertyQuota ... Toggles whether to return the current state of this Google Analytics property's quota."

**The date range is inclusive at both ends.** https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/DateRange (2026-04-23).
"The inclusive end date for the query in the format YYYY-MM-DD."
The hub's windows are half open, so the connector sends `endDate` as the window end minus one day.  `scripts/sync_ga4.py` ends its default window at the UTC date twelve hours ago, so a day still running in a US time zone is never pulled and counted as finished. Relative forms are accepted by the page ("NdaysAgo, yesterday, or today"); `verify()` uses `yesterday`.

**Limits per request.** Dimension page: "Requests are allowed up to 9 dimensions." Metric page: "Requests are allowed up to 10 metrics." DateRange page: "Requests are allowed up to 4 date ranges." The connector sends one dimension, four metrics and one range.

**Paging.** https://developers.google.com/analytics/devguides/reporting/data/v1/basics (2026-10-22), and the runReport page for the defaults.
"The rowCount parameter in the response indicates the total number of rows, independent of the limit and offset values in the request."
"If unspecified, 10,000 rows are returned. The API returns a maximum of 250,000 rows per request, no matter how many you ask for."
The connector pages with `offset` until the offset reaches `rowCount`. One row a day means a window never fills a page; the tests page with a page size of 1 to 7 instead.

**Ordering for stable pages.** https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/OrderBy (2026-09-14).
`dimension: {dimensionName, orderType}` with "ALPHANUMERIC ... Alphanumeric sort by Unicode code point". The connector orders by `date` with ALPHANUMERIC, which is chronological for YYYYMMDD. UNVERIFIED: that Google keeps the order the same between two page requests (the pages do not promise it; the order by is what the connector relies on).

**Days with no activity are left out.** runReport page, `keepEmptyRows`:
"If false or unspecified, each row with all metrics equal to 0 will not be returned."
The connector does not set it. A day with no row is therefore "the property reported nothing or nobody asked", and the report tells the two apart from the sync runs, the same rule as for Shopify orders.

## The answer

**The shape.** https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/RunReportResponse (2026-04-23).
"The number of DimensionHeaders and ordering of DimensionHeaders matches the dimensions present in rows." The same is said of metric headers. The connector reads columns by header name, not by position, and refuses a response whose dimension column is not `date` or that lacks a requested metric.
`kind` is "the fixed string "analyticsData#runReport"". `rowCount` is an integer.

**Every value is a string.** https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/Row (2026-09-14): `"value": "15"` for a metric value. Dimension page: "Dimension values in report responses are strings". The connector parses a metric value with Decimal, accepts only a whole number of at least 0, and refuses anything else as a broken response.

**The date dimension.** https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema (2026-10-22).
"date ... The date of the event, formatted as YYYYMMDD."

**The four metrics, by their current names.** Same api-schema page.
"sessions ... The number of sessions that began on your site or app (event triggered: session_start)."
"totalUsers ... The number of distinct users who have logged at least one event, regardless of whether the site or app was in use when that event was logged."
"screenPageViews ... The number of app screens or web pages your users viewed. Repeated views of a single page or screen are counted. (screen_view + page_view events)."
"keyEvents ... The count of key events. Marking an event as a key event affects reports from time of creation. It doesn't change historic data."
Stored as `sessions`, `users`, `page_views`, `key_events` (migration 006).
**`conversions` is not used.** The saved metric listing has no `conversions` metric; the only place the word appears on that page is a navigation link titled "Conversions". The current name is `keyEvents`, and the stand-in answers 400 to `conversions`. UNVERIFIED: whether Google still accepts `conversions` as an alias. It does not matter to the connector, which never asks for it.
`totalUsers` is a distinct count for one day, so a month cannot add it up. The report shows the mean over the days that have data (the same rule as `customers`).

**Metadata.** https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/ResponseMetaData (2026-09-14).
"timeZone ... The property's current timezone." The connector keeps it for the label `verify()` returns. `samplingMetadatas`: "If this report results is sampled, this describes the percentage of events used in this report." A sampled report is stored with `{"sampled": true}` in `raw`.
UNVERIFIED: that the `date` dimension is cut at the property's time zone. The page defines `date` only as "the date of the event", and describes `timeZone` as meant for hour and minute dimensions. The report footnote says website days follow the property's own time zone and store days follow UTC; if that is wrong the two sources are lined up by date label and could differ by a day at the edges.

## Authentication

**Scope.** runReport page: "Requires one of the following OAuth scopes: https://www.googleapis.com/auth/analytics.readonly, https://www.googleapis.com/auth/analytics". The connector's registry entry names `analytics.readonly`.

**What it takes.** An access token, already issued, from `GA4_ACCESS_TOKEN` (or a function that returns the current one), held in a `Secret` and sent only in the Authorization header. The property id is `GA4_PROPERTY_ID`. Nothing is read from a file or committed.
UNVERIFIED: the literal `Authorization: Bearer <token>` header. The pages read speak of an "OAuth 2.0 access token" (errors page) and never print the header; Bearer is the standard form for OAuth 2.0 access tokens and the stand-in checks for it.
**Not built: getting or refreshing a token.** The service account page (https://developers.google.com/identity/protocols/oauth2/service-account, 2026-03-23) was retrieved, but the HTTP tab with the token request did not come through in the saved text, so the signed token exchange was not written from it. A person or a scheduler supplies a token that lasts about an hour; a 401 is raised as `AuthExpired` so the caller knows to fetch a new one.

## Errors

Page: https://developers.google.com/analytics/devguides/reporting/data/v1/errors (2026-10-22). (The address `.../error-responses` answers 404.)
The error object: `{"error": {"code": 403, "message": "...", "status": "PERMISSION_DENIED"}}`.

| HTTP | Canonical | The page says | What the connector does |
|---|---|---|---|
| 400 | INVALID_ARGUMENT | "The request is malformed." | `UpstreamBroken`, not retried |
| 401 | UNAUTHENTICATED | "the OAuth 2.0 access token is missing, invalid, or expired" | `AuthExpired`, retryable after a new token |
| 403 | PERMISSION_DENIED | "does not have access to the requested Google Analytics property" | `AuthRevoked`, a person must fix the access |
| 429 | RESOURCE_EXHAUSTED | "denied due to API quota limits being reached" | retried with backoff, then `RateLimited` |
| 500 | INTERNAL | "Retrying the request with exponential backoff is recommended." | same as 429 |
| 503 | UNAVAILABLE | same sentence | same as 429 |

"To avoid exceeding server error quotas, it is important to implement exponential backoff with retry limits." So a call is tried 3 times by default, waits about 1 and then 2 seconds (jittered to between half and all of each), never waits longer than 30 seconds in the process, and raises `RateLimited` after the last try. A longer wait is handed back to the sync runner.
Any other 5xx (502, 504) is also retried. UNVERIFIED: the pages list only 500 and 503, so this is the connector's own choice.
UNVERIFIED: a `Retry-After` header on a 429. No page read mentions one. The connector uses it when it is a number and falls back to its own backoff when it is absent, which is the normal case it was tested on.
UNVERIFIED: the status Google sends for a property id that does not exist. The 403 row covers a property the token cannot read; the stand-in answers 403 for both.

## Quotas

Page: https://developers.google.com/analytics/devguides/reporting/data/v1/quotas (2026-10-22), and https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/PropertyQuota (2026-04-23).
For a standard property: 200,000 core tokens per day, 40,000 per hour, 14,000 per project per hour, 10 concurrent requests, and "Server Errors are 500 and 503 codes" with 10 per hour for the project and property pair.
"If any quota bucket reaches 0 remaining, subsequent requests to that property return a 429 RESOURCE_EXHAUSTED error."
"All daily quotas are refreshed at midnight Pacific Standard Time. All hourly quotas are refreshed within an hour but not necessarily on the whole hour boundaries."
"Monitor remaining balances dynamically: Inspect propertyQuota.tokensPerProjectPerHour.remaining and propertyQuota.tokensPerHour.remaining in responses and throttle or pause non-urgent background jobs when remaining tokens fall below a safety threshold."
The connector asks for `returnPropertyQuota`, remembers `tokensPerHour.remaining`, and stops before a further page when it is at or under 100, raising `RateLimited` so the runner keeps the pages already written and the cursor. The floor of 100 is the connector's own choice; the pages name no number. The connector makes one request a window in practice, so this matters for a backfill of many properties, not for one.

## What the stand-in invents

Everything not quoted above. The text of the quota error message, the exact `tokensPerDay` figures it reports, the `TYPE_INTEGER` type it gives each metric header (the pages show it only for `activeUsers`), `currencyCode`, the time zone `America/Los_Angeles`, and `emptyReason`. The connector reads none of those except `timeZone` and `tokensPerHour.remaining`.
It also answers one date range only, though the real API allows four, and its data is a dictionary a test fills in. It cannot tell you how Google restates a day after the fact, how long data takes to settle, or whether a real property is thresholded or sampled. The connector rewrites a day's rows when a later sync returns different figures (tested), and keeps the old figures for a day Google later leaves out (a known limit, pinned by an expected failure in tests/test_ga4_sync_db.py).
