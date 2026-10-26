# Meta sources

What the Meta Marketing API connector (`app/connectors/meta.py`) and its stand-in server (`app/connectors/meta_fake.py`) were written from. Every page below was loaded in a real browser on 2026-10-26 and saved as text; the quote under each fact is copied from the saved page. A fact the pages did not give is marked UNVERIFIED here and in the code, and it stays that way until a real ad account answers.

No ad account is held and none was used. The connector has only run against the stand-in, so nothing here is a statement that Meta accepts these requests. The stand-in is written from these pages, and it refuses what they say Meta refuses (`tests/test_meta_fake_server.py`).

## The call

**One edge.** Account level insights. Page: https://developers.facebook.com/docs/marketing-api/insights/ (updated Apr 27, 2026).
"The Ads Insights API is available as an edge off of all ad objects." The table on the page lists "/{ad-account-id}/insights" as "Insights from an ad account". The connector sends `GET https://graph.facebook.com/v26.0/act_{id}/insights`.
The version is the one that page's own example uses ("https://graph.facebook.com/v26.0/<CAMPAIGN_ID>/insights"). The reference page's example says `v25.0`, so UNVERIFIED which version is current; the version is a constant in the connector.

**The account id.** Page: https://developers.facebook.com/docs/marketing-api/reference/ad-account/. `id` is "The string act_{ad_account_id}." The connector accepts `123` or `act_123` and refuses anything else before a request.

**The parameters.** Page: https://developers.facebook.com/docs/marketing-api/reference/ad-account/insights/ (no date on the page). Used: `level`, `time_increment`, `time_range`, `fields`, `action_attribution_windows`, `limit`, `after`.
`level`: "enum {ad, adset, campaign, account}". The connector asks for `account`.
`time_increment`: "If it is an integer, it is the number of days from 1 to 90." The connector sends `1`, one result set a day.
`time_range`: "{'since':YYYY-MM-DD,'until':YYYY-MM-DD}" and "UNIX timestamp not supported."
**UNVERIFIED: whether `until` is inclusive.** The page does not say. The connector treats it as inclusive, as `date_start` and `date_stop` suggest, and sends the window end minus one day; a response that did not include that last day would look like a quiet day, which no test here can tell apart.
`fields`: "Default behavior is to return impressions and spend." The connector asks for `account_currency`, `spend`, `impressions`, `clicks`, `actions` and `action_values`.

**Attribution is named, never defaulted.** Same page, `action_attribution_windows`: "The default option means ["7d_click","1d_view"]." and "The attribution window determines the window (e.g. 7d) and engagement type (e.g click) that's used as a filter to report actions." The request sends `["7d_click"]`, so a change of Meta's default cannot change what a purchase means, and the slide says which window it is. UNVERIFIED: whether `1d_view` was removed or changed since the page; the connector does not use it.

**The answer carries purchases per window.** Page: https://developers.facebook.com/docs/marketing-api/reference/ads-action-stats/. `value` is "Metric value of default attribution window"; `7d_click` is "Metric value of attribution window "7 days after clicking the ad"", and every window number is a "numeric string". The connector reads the `7d_click` number, never `value`: after the request names a window, `value` is the default window's number, which is not the one pinned. A test makes the stand-in send a `value` that differs from `7d_click` and checks the connector reads the window.
`action_type` lists "omni_purchase: Purchases", "offsite_conversion.fb_pixel_purchase: Purchases" and "onsite_conversion.purchase: On-Facebook Purchases". The pages do not say how these overlap (UNVERIFIED), so adding them could count a purchase more than once. Only `omni_purchase` is read, the one whose name does not tie it to a single channel; that choice is the connector's own.
UNVERIFIED: whether an account with no purchases sends an `actions` list without an `omni_purchase` entry or no list at all. The connector writes a purchase count of 0 for the first and no purchase figure for the second, and the report counts the days that have one.

**Money and counts are strings.** Same reference page: `spend` is "numeric string" and "The estimated total amount of money you've spent on your campaign, ad set or ad during its schedule. This metric is estimated." `clicks` is "numeric string", "The number of clicks on your ads." `account_currency` is "Currency that is used by your ad account." The connector parses `spend` and the purchase value with Decimal into integer minor units rounded half up (a zero decimal currency is not scaled), takes the currency from `account_currency` of each row and refuses a row without one, and refuses a window whose rows name two. `clicks` counts every click, not link clicks; the slide says "Clicks".
UNVERIFIED: how many decimal places `spend` carries and how Meta rounds it.

**A day with no delivery.** UNVERIFIED: the pages do not say whether a day with no impressions and no spend has a row. The stand-in sends none, and the report tells a quiet day from a day nobody looked at from the sync runs, as for GA4.

**The ad account itself.** Same ad account page: `account_status` is the "Status of the account" with `1 = ACTIVE`, `2 = DISABLED`, `3 = UNSETTLED` and more, `currency` is "The currency used for the account, based on the corresponding value in the account settings." and `timezone_name` is "Name for the time zone". `verify()` reads these three fields and refuses an account that is not `1`.

**Time zone and freshness.** Page: https://developers.facebook.com/docs/marketing-api/insights/best-practices/ (updated May 5, 2026).
"We report ad insights data in the ad account's timezone."
"Insights refresh every 15 minutes and do not change after 28 days of being reported"
"Insights metrics may continue to update for a couple of days after an ad has completed"
So an ad day is cut in the account's time zone and a recent day can still change. The connector's default window ends twelve hours behind UTC, as for GA4, so a day still running anywhere is not pulled; a day that is over can still gain purchases inside the click window. The report marks the last 7 days of a month as provisional for that reason (the 7 is the click window, an inference from it, not a number on the pages: UNVERIFIED how long Meta takes to settle a day).

## Paging

Page: https://developers.facebook.com/docs/graph-api/results.
"after : This is the cursor that points to the end of the page of data that has been returned."
"next : The Graph API endpoint that will return the next page of data. If not included, this is the last page of data. Due to how pagination works with visibility and privacy, it is possible that a page may be empty but contain a next paging link. Stop paging when the next link no longer appears."
"Don't store cursors. Cursors can quickly become invalid if items are added or deleted."
The connector pages with `after` while `next` is present, continues through an empty page that has a `next`, and stops when `next` is gone.
**The `next` link is never requested.** The page's example shows it as a full URL, and a Graph `next` link carries the access token; following an address a response names would send the token wherever the response points. The connector only reads that a next page exists and builds the request itself. A cursor that comes back unchanged is refused as paging that does not advance, so a response cannot loop the connector.
The cursor is kept on the sync run for resuming within a run, not across days, which the page warns against.

## Errors

Page: https://developers.facebook.com/docs/graph-api/guides/error-handling.
The error object: `{"error": {"message": "...", "type": "OAuthException", "code": 190, "error_subcode": 460, "error_user_title": "...", "error_user_msg": "...", "fbtrace_id": "..."}}`.
**UNVERIFIED: the HTTP status Meta puts on an error.** The pages describe the object and the codes, not the statuses. The connector therefore reads the error object first and classifies by `code`, whatever the status, and so also when the object arrives inside a 200. The stand-in uses 400, which is its own choice.

| Code | The page says | What the connector does |
|---|---|---|
| 190 | "Access token has expired ... Get a new access token." | `AuthExpired`, retryable after a new token |
| 102 | "API Session ... the login status or access token has expired, been revoked, or is otherwise invalid" | `AuthExpired` |
| 10, 200 to 299, 3 | "Permission is either not granted or has been removed" | `AuthRevoked`, a person must fix the access |
| 4, 17, 32, 341, 613 | "Temporary issue due to throttling. Wait and retry the operation" (4 and 17), "a custom rate limit has been reached" (613) | retried with backoff, then `RateLimited` |
| 1, 2 | "Possibly a temporary issue due to downtime. Wait and retry the operation." | the same |
| 100, 2642, 3018, 2500, 105, 3001, any other | Insights reference: "Invalid parameter", "Invalid cursors values", "The start date of the time range cannot be beyond 37 months from the current date", and others | `UpstreamBroken`, not retried |

A body that is not an object, a list, an HTML page or a string `error` is handled without a crash: a 200 that is not a JSON object is `UpstreamBroken`, a 5xx or 429 of any shape is retried then `RateLimited`, a 401 with no object is `AuthExpired` and a 403 with none `AuthRevoked`. A data-per-call limit is "error_code = 100, CodeException (error subcode: 1487534)" on the best practices page, and is `UpstreamBroken` with a message that names the code.

## Rate limits

Pages: https://developers.facebook.com/docs/graph-api/overview/rate-limiting and the best practices page above.
"Check the x-fb-ads-insights-throttle HTTP header in every API response to know how close your app is to its limit"; its value is `{ "app_id_util_pct": 100, "acc_id_util_pct": 10, "ads_api_access_tier": "standard_access" }`. The rate limit page also shows `x-ad-account-usage` (`acc_id_util_pct`), `x-app-usage` (`call_count`, `total_time`, `total_cputime`) and `x-business-use-case-usage`, whose objects carry `call_count`, `total_cputime`, `total_time`, `type` and `estimated_time_to_regain_access` ("Time, in minutes, until calls will not longer be throttled").
"Add a back-off mechanism to slow down or pause your /insights queries when you come close to hitting 100% utility for your application, or for your ad account."
The connector reads all of these as percentages, keeps the highest, and stops before a further page when it is at or above 90 (its own choice: the page names no number), raising `RateLimited` so the runner keeps the pages already written. After a throttle error it waits the header's minutes if that is no longer than 30 seconds and otherwise hands the wait back to the runner. A header that is not JSON or holds other types is ignored, because pacing must not crash a sync.
UNVERIFIED: a `Retry-After` header. No page read mentions one; the connector uses it when it is numeric.

## Authentication

**Permission.** Insights page: "The ads_read permission."
**The token.** An access token, already issued, from `META_ACCESS_TOKEN` (or a function that returns the current one), held in a `Secret`. The pages show it as the `access_token` request parameter (`curl -G -d "access_token=<ACCESS_TOKEN>"`), so that is where it goes. UNVERIFIED: that an `Authorization: Bearer` header would also work; it was not used. No URL, request or response body is put in an error message, and a transport error is reported by its class only.
**Not built:** getting a token, exchanging a short one for a long one, or refreshing one.

## What the stand-in invents

Everything not quoted above. The HTTP status of an error (400), the text of the error messages, the `fbtrace_id`, that a day with no impressions and no spend has no row, the base64 form of its cursors, the utilisation numbers it reports, that an unknown ad account answers error 200, and the answer to `verify()` (the three fields are on the page, the shape of the reply is not). It cannot tell you how Meta restates a day after the fact, how long a purchase takes to arrive, what an account with no pixel sends, or whether `until` is inclusive.
