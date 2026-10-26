# Search Console sources

What the Google Search Console connector (`app/connectors/gsc.py`) and its stand-in server (`app/connectors/gsc_fake.py`) were written from. Every page below was loaded in a real browser on 2026-10-26 and saved as text; the quote under each fact is copied from the saved page. A fact the pages did not give is marked UNVERIFIED here and in the code, and it stays that way until a real property answers.

No Search Console property is held and none was used. The connector has only run against the stand-in, so nothing here is a statement that Google accepts these requests. The stand-in is written from these pages, and it refuses what they say Google refuses (`tests/test_gsc_fake_server.py`).

## The call

**One method.** searchAnalytics.query. Page: https://developers.google.com/webmaster-tools/v1/searchanalytics/query (last updated 2026-08-11).
"POST https://www.googleapis.com/webmasters/v3/sites/siteUrl/searchAnalytics/query"
The base address is the one that page prints. UNVERIFIED: whether `searchconsole.googleapis.com` is also served; it was not used.

**The property.** Same page: "The URL of the property as defined in Search Console. Examples: http://www.example.com/ (for a URL-prefix property) or sc-domain:example.com (for a Domain property)". The page's own example puts it in the path percent-encoded in full (`https%3A%2F%2Fwww.example.com%2F`), and the connector does the same for both forms, so `sc-domain:example.com` travels as `sc-domain%3Aexample.com`. A bare origin (`https://www.example.com`) is given its trailing slash, because a URL-prefix property is written with it in the examples (the connector's reading, not a statement on the page).

**The request body.** Same page: `startDate`, `endDate`, `dimensions`, `type`, `rowLimit`, `startRow`, `dataState`.
"startDate ... [Required] Start date of the requested date range, in YYYY-MM-DD format, in PT time (UTC - 7:00/8:00). Must be less than or equal to the end date. This value is included in the range." The end date is "included in the range" too, so the hub's half open window is sent with its end moved back one day.
"rowLimit ... [Optional; Valid range is 1–25,000; Default is 1,000] The maximum number of rows to return. To page through results, use the startRow offset." and "startRow ... [Optional; Default is 0] Zero-based index of the first row in the response." Both are typed `integer`, so both are sent as JSON integers (the GA4 call types its paging fields as strings; this one does not).
"type ... "web": [Default] Filter results to the combined ("All") tab in Google Search. Does not include Discover or Google News results." The connector sends `web` explicitly.

**What a date query returns.** Same page: "When date is one of the dimensions, any days without data are omitted from the result list." and "Results are sorted by click count, in descending order, unless you group by date, in which case results are sorted by date, in ascending order (oldest first, newest last)." The connector reads one row a day and treats a missing day as either a quiet day or a day nobody looked at, which the report tells apart from the sync runs, as for Shopify orders and GA4.

**Paging.** How-to page: https://developers.google.com/webmaster-tools/v1/how-tos/all-your-data.
"Page through results by re-running the same query, increasing the startRow value by 25,000 in the request until you reach the last page (a response with 0 rows)."
"If startRow exceeds the number of results for the query, the response will be a successful response with zero rows." (query page). The connector pages until a page comes back empty, so it always makes one request more than the rows need; there is no row count in the response to stop earlier by. A day that appears on two pages is refused as a repeated page.

**One query a day for the top rows.** How-to page: "We recommend running a query each day for one day's worth of data" and "Running a daily query for one day of data should not exceed your daily quota." After the date query, the connector asks, for each day that has a row, one query grouped by `query` and one by `page` with `rowLimit` set to the number of top rows wanted (10 by default). The day is the unit because `rowLimit` cuts the whole answer, not each day of it, and because the result is "sorted by click count" with "If two rows have the same click count, they are sorted in an arbitrary way": a tie that straddles the cut keeps an arbitrary one of the tied rows, which is a top n by clicks and not a fixed one (a test asserts exactly that).
**What those rows are not.** Query page: "The API is bounded by internal limitations of Search Console and does not guarantee to return all data rows but rather top ones." How-to page: "When you group by page and/or query, our system may drop some data in order to be able to calculate results in a reasonable time using a reasonable amount of computing resources." and "the Search Analytics method exposes a maximum of 50K rows of data per day per search type". So the per query and per page figures are the top rows Search Console returns, never a total of the day, and the report says so. The daily totals group by date alone, which the how-to calls the form "For accurate counts".

## The answer

**The shape.** Query page: `rows[]` of `keys[]`, `clicks`, `impressions`, `ctr`, `position`, with `clicks`, `impressions`, `ctr` and `position` typed `double`; "rows[].ctr ... Click Through Rate (CTR) for the row. Values range from 0 to 1.0, inclusive." and "rows[].position ... Average position in search results." When a query matches nothing the page's schema implies no `rows` member; the connector reads a missing `rows` as none.
The connector does not store Google's `ctr`. It stores clicks and impressions and the report computes the rate from the integers, so a rate is never an average of rates.
**Counts that arrive as the wrong type.** The page types the counts as doubles. A value that is an integer, a whole float (`12.0`) or a numeric string is read; a fractional count, a negative, `NaN` (which Python's JSON parser would accept), a boolean, or more clicks than impressions is a broken response. `position` is kept in thousandths rounded half up. UNVERIFIED: which of those forms Google actually sends.

**Time zone.** "in PT time (UTC - 7:00/8:00)". Search days are Pacific days, stated on the page, and the report's footnote says so beside the store's UTC days.

## What is final, and what is not

**Data takes days.** How-to page: "Data is typically available after 2-3 days; you can learn what is the most recently available data by running a simple query grouped by date for the past 10 days." `verify()` does exactly that.
**`dataState`.** Query page: ""If "all" (case-insensitive), data will include fresh data. If "final" (case-insensitive) or if this parameter is omitted, the returned data will include only finalized data." With `final` (the default) the newest days have no row, and a sync window that covered them would read them as days with no clicks. So the connector asks for `all`, and the newest days arrive.
**Where the data stops being final.** Query page: "first_incomplete_date (string): The first date for which the data is still being collected and processed, presented in YYYY-MM-DD format" and "This field is populated only when the request's dataState is all and data is grouped by date, and the requested date range contains incomplete data points." and "All values after the first_incomplete_date may still change noticeably." The connector stores every day from that date on with `{"provisional": true}` in `raw`, and the next sync rewrites them (tested). The report does not read `raw`: it marks the last 3 days before the report date as provisional by arithmetic (3 is the connector's reading of "2-3 days", UNVERIFIED as a settling time), and says so on the slide.
UNVERIFIED: the metadata on a page that has no rows, and whether the metadata object is the same on every page of a paged query. The connector reads it from each page for that page's own rows.

## Authentication

**Scope.** Query page: "https://www.googleapis.com/auth/webmasters.readonly" (and "https://www.googleapis.com/auth/webmasters"). The registry entry names the read-only one.
**What it takes.** An access token, already issued, from `GSC_ACCESS_TOKEN` (or a function that returns the current one), held in a `Secret` and sent only in the Authorization header. The property is `GSC_SITE_URL`. UNVERIFIED: the literal `Authorization: Bearer <token>` header on these pages; it is the standard form for OAuth 2.0 and the stand-in checks for it. Not built: getting or refreshing a token, as for GA4.

## Errors

Page: https://developers.google.com/webmaster-tools/v1/errors (last updated 2025-08-28), the global error object:
`{"error": {"errors": [{"domain": "global", "reason": "invalidParameter", "message": "...", "locationType": "parameter", "location": "chart"}], "code": 400, "message": "..."}}`.
This is not the GA4 shape (`status` is a string there). The connector reads `code`, the first `reason` and `message`, and also an OAuth style `{"error": "invalid_token", "error_description": "..."}`, a list that wraps the object, and an object inside a 200.

| Status and reason | The page says | What the connector does |
|---|---|---|
| 401 `authError`, `expired` | "The authorization credentials provided for the request are invalid." and "Session Expired." | `AuthExpired`, retryable after a new token |
| 403 `forbidden`, `insufficientPermissions`, `accessNotConfigured` | "The requested operation is forbidden and cannot be completed." and "The authenticated user does not have sufficient permissions to execute this request." | `AuthRevoked`, a person must fix the access |
| **403 `rateLimitExceeded`, `userRateLimitExceeded`, `quotaExceeded`, `dailyLimitExceeded`, `limitExceeded`, `servingLimitExceeded`, `concurrentLimitExceeded`** | "Too many requests have been sent within a given time span." and the others, listed under FORBIDDEN (403) | retried with backoff, then `RateLimited`. Reading these as a revoked connection would mark a healthy one as disconnected. |
| 429 `rateLimitExceeded` | "Too many requests have been sent within a given time span." | the same |
| 500 `internalError`, 503 `backendError` | "The request failed due to an internal error." and "A backend error occurred." | the same |
| 400 `invalidParameter`, `badRequest`, 404, other | "The request failed because it contained an invalid parameter or parameter value." | `UpstreamBroken`, not retried |

Retries are bounded: 3 attempts by default, an exponential jittered wait, a wait the response asks for that is longer than 30 seconds is handed back as `RateLimited` for the runner to own, and no URL, request or response body is put in an error message.
UNVERIFIED: a `Retry-After` header on a 429; it is used when it is numeric.

## Quotas

Page: https://developers.google.com/webmaster-tools/limits. For Search Analytics "Per-site quota (calls querying the same site): 1,200 QPM", per user "1,200 QPM", per project "30,000,000 QPD" and "40,000 QPM". Load quota: "Queries are expensive when you group and/or filter by either page or query string. Queries grouped/filtered by page AND query string are the most expensive." The connector never groups by page and query together, asks for one day at a time as the how-to recommends, and makes two top queries per day with data: about 2 plus 2 requests per day for a window, so a 30 day window is about 62 requests, far under the per-minute limits. "The "quota exceeded" error is the same for all quota exceeded events" is why a 403 with a quota reason is retried rather than read as a permissions error.

## What the stand-in invents

Everything not quoted above. The number of days data takes to become final (3, from "2-3 days"), the 403 reason it gives an unknown property (`forbidden`), the error messages, the order of rows that tie on clicks (it shuffles them on request), and how it writes counts (an integer, a whole float or a string, each on request). It also answers one data type, `web`, and holds its data in a dictionary a test fills in. It cannot tell you how Google restates a day, what a real property's rows look like when queries are anonymised, or whether a count arrives as `12` or `12.0`.
