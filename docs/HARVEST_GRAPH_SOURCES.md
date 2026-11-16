# Harvest and Microsoft Graph sources

What the Harvest connector (`app/connectors/harvest.py`) and the Microsoft Graph connector (`app/connectors/msgraph.py`) and their stand-in servers (`harvest_fake.py`, `msgraph_fake.py`) were written from. Every page below was loaded in a real browser (Chromium) on 2026-11-17 and saved as text; a quote under a fact is copied from the saved page. A fact the pages did not give is marked UNVERIFIED here and in the code, and it stays that way until a real account answers.

No Harvest account and no Microsoft 365 tenant is held and none was used. Both connectors have only run against their stand-ins, so nothing here is a statement that either vendor accepts these requests. A stand-in is written from these pages and refuses what they say the vendor refuses (`tests/test_harvest_connector.py`, `tests/test_msgraph_connector.py`).

The shared rules (integer minor units, half up rounding, a float refused, one bounded retry loop) are in `app/connectors/common.py` and are checked in `tests/test_vendor_common.py`.

## Harvest

**The call.** Pages: https://help.getharvest.com/api-v2/timesheets-api/timesheets/time-entries/ and https://help.getharvest.com/api-v2/introduction/overview/general/.
`GET /v2/time_entries` on `https://api.harvestapp.com`, with `Authorization: Bearer $ACCESS_TOKEN`, `Harvest-Account-Id: $ACCOUNT_ID` and `User-Agent: MyApp (yourname@example.com)`.
"We also require that each request include a User-Agent header with both: The name of your application, A link to your application or email address." and "If you don’t include a User-Agent header, you’ll get a 400 Bad Request response." The connector sends `api-analytics-hub (<contact>)` and refuses to be built without a contact that is an email address or a web address.
"Each request will require your account ID as well, since you can use this Personal Access Token to access any of your Harvest or Forecast accounts." (https://help.getharvest.com/api-v2/authentication-api/authentication/authentication/). The account id is a credential field and a header.

**The window.** `from`: "Only return time entries with a spent_date on or after the given date." `to`: "Only return time entries with a spent_date on or before the given date." Both are inclusive, so the half open window end is moved back one day (`tests/test_harvest_connector.py`, the three headers test).

**The answer.** "The response contains an object with a time_entries property that contains an array of up to per_page time entries." The fields read: `id`, `spent_date` (a date), `hours` ("Number of (decimal time) hours tracked in this time entry."), `rounded_hours` ("Number of (decimal time) hours tracked in this time entry used in summary reports and invoices. This value is rounded according to the Time Rounding setting in your Preferences."), `billable` (a boolean), `billable_rate` (a decimal, null in the page's own example of a non billable entry), `project` (an object with `id`). Hours and rates are JSON numbers, so the response is parsed with `parse_float=Decimal`.
The connector computes the amount itself: `rounded_hours` times `billable_rate` for a billable entry with a rate, rounded half up to the currency's minor unit. UNVERIFIED: that Harvest's own invoices compute a line this way; the page says only that `rounded_hours` is the figure "used in summary reports and invoices".
UNVERIFIED: a maximum for one entry. The connector refuses an entry above 24 hours as its own bound.

**Paging.** Page: https://help.getharvest.com/api-v2/introduction/overview/pagination/.
"you should always use the pagination URLs provided by the links section of a response instead of constructing pagination links yourself." The connector follows `links.next` exactly as given and never builds a page URL.
"The default and maximum per_page value for API requests is 2000." and "If you provide a value for per_page that is too large, you will receive a 422 Unprocessable Entity error code with an Invalid per_page parameter error message." The connector asks for 2000.
The example response carries `total_entries`; the connector checks the number of records that arrived against it. UNVERIFIED: that `total_entries` is present and exact on every list endpoint, so a missing one is not an error.

**The limit.** "The rate limit for general API requests is 100 requests per 15 seconds." and "When the rate limit is exceeded Harvest will send an HTTP 429 status code. The number of seconds until the throttle is lifted is sent via the Retry-After HTTP header, as specified in RFC 2616." The connector counts its own requests and raises `RateLimited` before the 101st inside 15 seconds (a retry is a request and counts), and a 429 is waited for only when the vendor's figure is within the retrier's cap.
The same page lists 403, 404, 422 and 500 as the other statuses. UNVERIFIED: the status for a bad token (the connector reads a 401 as an expired token), and the body of any error. An error body is never put in a message: only the status is.

**Projects.** Page: https://help.getharvest.com/api-v2/projects-api/projects/projects/.
"You must be an Administrator or Manager with managed projects in order to interact with the /v2/projects endpoint. Insufficient permissions will result in a 403 Forbidden status code." A 403 on the project read makes the connector write the three hour counts and leave the amount out, and `degraded_metrics` says so.
`currency`: "The currency for the project’s billable rates, fees, and budgets, as an ISO 4217 code." An entry on a project in another currency than the connection's is refused, never added in.
`budget`: "The budget in hours for the project when budgeting by time. When the budget is by task or person this is the sum of the per-task or per-person amounts, so the figure is read-only." `budget_is_monthly`: "Option to have the budget reset every month." The budget check compares the hours spent with `budget`, and does not compare a monthly budget with hours counted over another span.
UNVERIFIED: the values `budget_by` can take (the page describes the field and does not list them), and what `budget` holds for a project budgeted by money (null is assumed: the connector treats a null or zero `budget` as no hours budget).

**The cheapest call.** `GET /v2/users/me`: "Retrieves the currently authenticated user. Returns a user object and a 200 OK response code." (https://help.getharvest.com/api-v2/users-api/users/users/). `verify()` uses it.

**Credentials.** A Personal Access Token ("Personal Access Tokens are the preferred method when you want to write your own scripts that consume our API") goes in the same header as an OAuth2 access token. The OAuth2 refresh flow is on the same page and is not implemented here: an OAuth2 token lasts `expires_in` seconds (1209600 in the page's example) and would need the Salesforce style refresh lock this connector does not carry.

## Microsoft Graph

**Signing in as the app.** Page: https://learn.microsoft.com/en-us/graph/auth-v2-service.
`POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`, with `scope=https://graph.microsoft.com/.default` ("Specify the preconfigured permissions by passing https://graph.microsoft.com/.default as the value for the scope parameter in the token request.") and `grant_type=client_credentials` ("Must be client_credentials."), the client id and secret in the body. The answer: `token_type` (`Bearer`: "The only type that Microsoft Entra ID supports is Bearer."), `expires_in` (3599 in the page's example), `ext_expires_in`, `access_token`. The token is renewed a minute before `expires_in` and on a 401, once, behind a lock.
UNVERIFIED: the JSON body and the HTTP status of a refused sign-in. The connector reads the OAuth 2.0 `error` word (`invalid_client`, `unauthorized_client`, `invalid_scope`, `invalid_request`, `unsupported_grant_type`) as "a person must fix the app registration", whatever the status, and quotes no description.

**A SharePoint list.** Page: https://learn.microsoft.com/en-us/graph/api/listitem-list?view=graph-rest-1.0.
`GET /sites/{site-id}/lists/{list-id}/items` and `GET /sites/{site-id}/lists/{list-id}/items?expand=fields`; the Application permission is `Sites.Read.All`. The example answer is `{"value": [{"id": "2", "fields": {"Name": "Gadget", ...}}]}`. The connector asks for `expand=fields` and refuses a row with no `fields`.
"Filtering works best on indexed columns." and the page gives no rule for combining two filters, so the connector reads the whole list and counts the days itself. `createdDateTime` and `lastModifiedDateTime` are the listItem properties the connector counts by; the page's example does not print them. UNVERIFIED: that they are in the default answer without a `$select` (the Graph `listItem` resource carries them, and that page was not read).

**A Teams channel.** Page: https://learn.microsoft.com/en-us/graph/api/channel-list-messages?view=graph-rest-1.0.
`GET /teams/{team-id}/channels/{channel-id}/messages`; Application permission `ChannelMessage.Read.Group` (least privileged, resource-specific consent) or `ChannelMessage.Read.All`. "Apply $top to specify the number of channel messages returned per page in the response. The default page size is 20 messages. You can extend up to 50 channel messages per page." The connector asks for 50.
"The channel messages in the response are sorted by the last modified date of the entire reply chain, including both the root channel message and its replies." So a message created in the window can sit on a late page, and the connector reads every page and counts by `createdDateTime`.
The example answer shows `messageType: "message"`, `createdDateTime`, `lastModifiedDateTime`, `deletedDateTime: null`, `from.user.displayName`, `body.contentType` and `body.content`, `webUrl`. The connector counts only `messageType` `message` with no `deletedDateTime`. UNVERIFIED: the other `messageType` values and how a deleted message appears (the stand-in uses `systemEventMessage` and a date).
UNVERIFIED: whether this API is metered for the tenant (the page was not read for it).

**An Excel register.** Pages: https://learn.microsoft.com/en-us/graph/api/table-list-rows?view=graph-rest-1.0 and https://learn.microsoft.com/en-us/graph/api/range-usedrange?view=graph-rest-1.0.
Both give the permission table "Application: Not supported." (delegated `Files.ReadWrite` only). An app signed in as itself therefore cannot read a workbook through `.../workbook/tables/{t}/rows` or `.../usedRange`, and the connector never calls them (the stand-in answers 403 to any `/workbook/` path, and a test shows the connector makes no such call).
The route that is documented for an application: https://learn.microsoft.com/en-us/graph/api/driveitem-get-content?view=graph-rest-1.0. `GET /drives/{drive-id}/items/{item-id}/content`; Application permission `Files.Read.All`. "Returns a 302 Found response redirecting to a preauthenticated download URL for the file" and "To download the contents of the file your application needs to follow the Location header in the response." The connector reads the `Location` itself, accepts only an https address (or loopback http for a stand-in), fetches it WITHOUT the token, and reads the file with openpyxl (values, not formulas, since a workbook written by Excel stores the last computed value).
UNVERIFIED: the size of a workbook this reads (the connector's own cap is 25 MB), and how a file in a SharePoint Embedded container is reached (that needs another permission, as the page notes).

**Paging.** Page: https://learn.microsoft.com/en-us/graph/paging.
"To read all results, you must continue to call Microsoft Graph with the @odata.nextLink property returned in each response until the @odata.nextLink property is no longer returned." and "Don't try to extract the $skiptoken or $skip value and use it in a different request." The connector follows the whole `@odata.nextLink` and never follows one that is not on the Graph host under `/v1.0/`.

**Limits.** Page: https://learn.microsoft.com/en-us/graph/throttling.
"Returns HTTP status code 429 Too Many Requests and the requests fail." with `Retry-After: 10` in the example, and "Wait the number of seconds specified in the Retry-After header." The connector waits for a short `Retry-After` and hands a long one to the runner. 503 and 504 are retried with a bound. UNVERIFIED: that every Graph surface the connector calls sends `Retry-After` (the page says all documented services do "except where indicated").

## What the stand-ins invent

Each is the stand-in's own choice, not a vendor fact. Harvest: the cursor value inside `links.next` (an opaque offset), the wording of an error body, the field order, pages cut at a size a test sets. Microsoft Graph: the `$skiptoken` form, the list page size, the access token's shape, the status of a bad sign-in (401), the `systemEventMessage` word, the way a deleted message is marked, the address of the pre-authenticated download.
