"""Google Ads API connector: daily account figures, and optionally Local Services Ads leads.

The fifth connector behind the contract in `base.py`, and the second paid media one: it writes the same
`ad_spend`, `ad_impressions` and `ad_clicks` keys Meta does, so the dashboard and the combined ad spend view treat
the two platforms the same way, plus `ad_conversions` and `ad_conversion_value`, and `ad_leads` for Local Services Ads.
It has been run only against the stand-in server in `google_ads_fake.py`, never against a live account: no
account is held. Every fact about the calls, with the page it came from and what is not confirmed, is in
docs/AD_AND_BUSINESS_SOURCES.md.

WHAT IT CALLS. `POST {base}/v26/customers/{id}/googleAds:search` with a GAQL query in the body. The account level
query selects `segments.date`, `customer.currency_code` and the metrics `cost_micros`, `impressions`, `clicks`,
`conversions` and `conversions_value` FROM `customer`, one row per day. With `local_services=True` it also runs a
second query FROM `local_services_lead` and counts the leads per day.

WHY SEARCH AND NOT SEARCHSTREAM. The Search method pages in fixed pages of 10,000 rows with a `nextPageToken`, so a lost
connection costs one page, and the runner can keep a cursor. SearchStream returns everything in one array. A day is one row,
so a month is 31 rows and the choice matters only for a very long backfill.

MONEY AND CURRENCY. `cost_micros` is an int64, which the JSON form sends as a string, in millionths of the account's
currency. It is stored as integer minor units with the currency beside it, and the currency comes from the response
(`customer.currency_code`), never from a default: a row that does not name one is refused, and so is a window whose rows
name two. `conversions` is a double, so the response is parsed with `parse_float=Decimal` and the count is stored in
thousandths (`ad_conversions` is a `milli_count`), because data driven attribution makes it fractional.

NO DEVELOPER TOKEN IS SENT. The developer token policy page says developer tokens are sunset, the REST examples carry
none, and a client "can stop sending developer tokens". `login_customer_id` is sent when the credentials are a
manager's. UNVERIFIED: whether an account still on the old programme is refused without one.

WHY THE RETRIES ARE BOUNDED. A call is tried `max_attempts` times with an exponential, jittered wait. A wait the response
asks for that is longer than `backoff_cap` is handed back as `RateLimited` for the runner to own. Google's own page says to
use exponential backoff on `RESOURCE_TEMPORARILY_EXHAUSTED`.

THE TOKEN. An already issued OAuth 2.0 access token (the `https://www.googleapis.com/auth/adwords` scope), from the
environment by `from_env` or a callable that returns the current one, held in a `Secret` and sent only in the
`Authorization` header. No URL, query or response body is put in an error message.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, AsyncIterator, Callable, Mapping

import httpx

from app.crypto import Secret

from .base import AuthExpired, AuthRevoked, CanonicalPoint, Connector, FetchResult, UpstreamBroken
from .common import Retrier, Retry, micros_to_minor, milli_units, minor_units, retry_after, whole_number
from .ga4 import settled_window_end  # noqa: F401 - the same window rule, re-exported for the sync script

BASE_URL = "https://googleads.googleapis.com"
#: Pinned in the URL. v26 is the version the REST pages read (2026-11-06) use in every example.
API_VERSION = "v26"
SUPPORTED = frozenset({"ad_spend", "ad_impressions", "ad_clicks", "ad_conversions", "ad_conversion_value", "ad_leads"})

#: The lookback for a query segmented by day, from the segmentation page: "up to 37 months".
MAX_MONTHS_BACK = 37
#: A guard against a response that never ends: 100 pages of 10,000 rows is a thousand years of days.
MAX_PAGES = 100

_CUSTOMER_RE = re.compile(r"^\d{10}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T]\d{2}:\d{2}:\d{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_DELAY_RE = re.compile(r"^(\d+(?:\.\d+)?)s$")

_RETRY_STATUS = frozenset({"RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED", "UNKNOWN", "ABORTED"})


def normalise_customer_id(value: Any) -> str:
    """Ten digits, with or without hyphens (`123-456-7890`). The header page says to remove the hyphens."""
    text = str(value).strip().replace("-", "")
    if not _CUSTOMER_RE.match(text):
        raise ValueError("customer_id must be the ten digit Google Ads customer id, got %r" % (value,))
    return text


def _decimal_json(text: str) -> Any:
    return json.loads(text, parse_float=Decimal)


class _GoogleError:
    """A Status object read into the few fields the connector acts on."""

    def __init__(self, code: int | None, status: str, message: str, http: int, delay: float | None):
        self.code, self.status, self.message, self.http, self.delay = code, status, message, http, delay

    def text(self) -> str:
        head = self.status or ("HTTP %d" % self.http)
        return ("%s: %s" % (head, self.message)).strip().rstrip(":")[:200]


def _read_error(body: Any, http: int) -> _GoogleError | None:
    if isinstance(body, list) and body:
        body = body[0]
    if not isinstance(body, dict) or "error" not in body:
        return None
    err = body["error"]
    if not isinstance(err, dict):
        return _GoogleError(None, "", str(err)[:120], http, None)
    code = err.get("code")
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    status = err.get("status") if isinstance(err.get("status"), str) else ""
    delay = None
    details = err.get("details")
    for item in details if isinstance(details, list) else []:
        if isinstance(item, dict) and isinstance(item.get("retryDelay"), str):
            m = _DELAY_RE.match(item["retryDelay"])
            if m:
                delay = float(m.group(1))
    return _GoogleError(code, status, str(err.get("message") or "")[:150], http, delay)


class GoogleAdsConnector(Connector):
    platform = "google_ads"
    supported_metrics = SUPPORTED

    def __init__(
        self,
        *,
        customer_id: str,
        access_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        login_customer_id: str | None = None,
        local_services: bool = False,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        retrier: Retrier | None = None,
        **retry_options: Any,
    ):
        self.customer_id = normalise_customer_id(customer_id)
        self.login_customer_id = normalise_customer_id(login_customer_id) if login_customer_id else None
        if (access_token is None) == (token_provider is None):
            raise ValueError("give exactly one of access_token and token_provider")
        self._token = Secret(access_token) if access_token is not None else None
        self._token_provider = token_provider
        self.local_services = bool(local_services)
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._retrier = retrier or Retrier(**retry_options)
        #: The account's currency and time zone as `verify()` read them.
        self.currency: str | None = None
        self.time_zone: str | None = None
        self.degraded_metrics: set[str] = set()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "GoogleAdsConnector":
        """Build from GOOGLE_ADS_CUSTOMER_ID and GOOGLE_ADS_ACCESS_TOKEN (GOOGLE_ADS_LOGIN_CUSTOMER_ID, GOOGLE_ADS_API_BASE_URL optional)."""
        env = os.environ if env is None else env
        missing = [n for n in ("GOOGLE_ADS_CUSTOMER_ID", "GOOGLE_ADS_ACCESS_TOKEN") if not env.get(n)]
        if missing:
            raise ValueError("set %s in the environment" % " and ".join(missing))
        if env.get("GOOGLE_ADS_API_BASE_URL"):
            kwargs.setdefault("base_url", env["GOOGLE_ADS_API_BASE_URL"])
        if env.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID"):
            kwargs.setdefault("login_customer_id", env["GOOGLE_ADS_LOGIN_CUSTOMER_ID"])
        if env.get("GOOGLE_ADS_LOCAL_SERVICES", "").lower() in ("1", "true", "yes"):
            kwargs.setdefault("local_services", True)
        return cls(customer_id=env["GOOGLE_ADS_CUSTOMER_ID"], access_token=env["GOOGLE_ADS_ACCESS_TOKEN"], **kwargs)

    def __repr__(self) -> str:
        return "GoogleAdsConnector(customer_id=%r, base_url=%r)" % (self.customer_id, self._base)

    @property
    def external_account(self) -> str:
        return self.customer_id

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ transport

    def _bearer(self) -> str:
        token = self._token.reveal() if self._token is not None else self._token_provider()
        if not token or not isinstance(token, str):
            raise AuthExpired("no access token is available")
        return token

    def _classify(self, response: httpx.Response) -> Retry | None:
        try:
            body: Any = _decimal_json(response.text)
        except ValueError:
            body = None
        err = _read_error(body, response.status_code)
        if err is None and response.status_code == 200:
            return None
        if err is None:
            err = _GoogleError(None, "", "", response.status_code, None)
        status, http = err.status, err.http
        if status == "UNAUTHENTICATED" or (not status and http == 401):
            raise AuthExpired("the access token was rejected (%s)" % err.text())
        if status == "PERMISSION_DENIED" or (not status and http == 403):
            raise AuthRevoked("no access to customer %s (%s)" % (self.customer_id, err.text()))
        if status in _RETRY_STATUS or (not status and (http == 429 or http >= 500)):
            return Retry(err.delay if err.delay is not None else retry_after(response), err.text())
        raise UpstreamBroken("Google Ads refused the request (%s)" % err.text())

    async def _search(self, query: str, page_token: str | None) -> tuple[dict, int, int]:
        url = "%s/%s/customers/%s/googleAds:search" % (self._base, API_VERSION, self.customer_id)
        payload: dict[str, str] = {"query": query}
        if page_token:
            payload["pageToken"] = page_token

        async def send() -> httpx.Response:
            headers = {"Authorization": "Bearer " + self._bearer(), "Content-Type": "application/json"}
            if self.login_customer_id:
                headers["login-customer-id"] = self.login_customer_id
            return await self._client.post(url, json=payload, headers=headers)

        response, calls, waits = await self._retrier.run(send, self._classify)
        try:
            body = _decimal_json(response.text)
        except ValueError:
            raise UpstreamBroken("Google Ads answered 200 with a body that is not JSON") from None
        if not isinstance(body, dict):
            raise UpstreamBroken("Google Ads answered 200 with %s, not an object" % type(body).__name__)
        return body, calls, waits

    # ------------------------------------------------------------------ parsing

    @staticmethod
    def _query(start: date, end: date) -> str:
        return ("SELECT segments.date, customer.currency_code, metrics.cost_micros, metrics.impressions, metrics.clicks, "
                "metrics.conversions, metrics.conversions_value FROM customer "
                "WHERE segments.date >= '%s' AND segments.date <= '%s'" % (start.isoformat(), (end - timedelta(days=1)).isoformat()))

    @staticmethod
    def _lead_query(start: date, end: date) -> str:
        return ("SELECT local_services_lead.creation_date_time, local_services_lead.lead_type FROM local_services_lead "
                "WHERE local_services_lead.creation_date_time >= '%s 00:00:00' AND local_services_lead.creation_date_time < '%s 00:00:00'"
                % (start.isoformat(), end.isoformat()))

    def _points(self, payload: Mapping[str, Any], start: date, end: date, seen: set[date], currencies: set[str]) -> list[CanonicalPoint]:
        rows = payload.get("results", [])
        if not isinstance(rows, list):
            raise UpstreamBroken("results is not a list")
        raw = {"source": "search", "resource": "customer"}
        points: list[CanonicalPoint] = []
        for row in rows:
            if not isinstance(row, dict):
                raise UpstreamBroken("a row is %s, not an object" % type(row).__name__)
            segments, customer, metrics = row.get("segments"), row.get("customer"), row.get("metrics")
            if not isinstance(segments, dict) or not isinstance(customer, dict) or not isinstance(metrics, dict):
                raise UpstreamBroken("a row has no segments, customer or metrics object")
            text = segments.get("date")
            if not isinstance(text, str) or not _DATE_RE.match(text):
                raise UpstreamBroken("a row's date is %r, not YYYY-MM-DD" % (text,))
            try:
                day = date.fromisoformat(text)
            except ValueError:
                raise UpstreamBroken("a row's date %s is not a calendar day" % text) from None
            if not start <= day < end:
                raise UpstreamBroken("a row for %s is outside the window %s to %s" % (day, start, end))
            if day in seen:
                # Two rows for one day would be written as one, silently keeping either.
                raise UpstreamBroken("two rows for %s" % day)
            currency = customer.get("currencyCode")
            if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
                # Never a default: money without its real currency is the bug this connector exists to avoid.
                raise UpstreamBroken("a row for %s has no currency_code" % day)
            currencies.add(currency)
            if len(currencies) > 1:
                raise UpstreamBroken("rows name two currencies: %s" % ", ".join(sorted(currencies)))
            for key in ("costMicros", "impressions", "clicks"):
                if key not in metrics:
                    raise UpstreamBroken("a row for %s has no %s" % (day, key))
            seen.add(day)
            points += [
                CanonicalPoint("ad_spend", day, micros_to_minor(metrics["costMicros"], currency, "cost_micros on %s" % day), currency, raw),
                CanonicalPoint("ad_impressions", day, whole_number(metrics["impressions"], "impressions on %s" % day), None, raw),
                CanonicalPoint("ad_clicks", day, whole_number(metrics["clicks"], "clicks on %s" % day), None, raw),
                # A double the response may leave out when it is zero (UNVERIFIED): every metric was selected, so absent reads as 0.
                CanonicalPoint("ad_conversions", day, milli_units(metrics.get("conversions", 0), "conversions on %s" % day), None, raw),
                CanonicalPoint("ad_conversion_value", day,
                               minor_units(metrics.get("conversionsValue", 0), currency, "conversions_value on %s" % day), currency, raw),
            ]
        return points

    def _lead_days(self, payload: Mapping[str, Any], start: date, end: date, counts: dict[date, int]) -> None:
        rows = payload.get("results", [])
        if not isinstance(rows, list):
            raise UpstreamBroken("results is not a list")
        for row in rows:
            lead = row.get("localServicesLead") if isinstance(row, dict) else None
            stamp = lead.get("creationDateTime") if isinstance(lead, dict) else None
            m = _STAMP_RE.match(stamp) if isinstance(stamp, str) else None
            if not m:
                raise UpstreamBroken("a lead's creation_date_time is %r, not YYYY-MM-DD HH:MM:SS" % (stamp,))
            try:
                day = date.fromisoformat(m.group(1))
            except ValueError:
                raise UpstreamBroken("a lead's date %s is not a calendar day" % m.group(1)) from None
            if not start <= day < end:
                raise UpstreamBroken("a lead for %s is outside the window %s to %s" % (day, start, end))
            counts[day] = counts.get(day, 0) + 1

    @staticmethod
    def _next_token(payload: Mapping[str, Any], current: str | None, tokens: set[str]) -> str | None:
        token = payload.get("nextPageToken")
        if token in (None, ""):
            return None
        if not isinstance(token, str):
            raise UpstreamBroken("nextPageToken is %s, not a string" % type(token).__name__)
        if token == current or token in tokens:
            # A page that points back at one already read would loop for ever, spending the quota.
            raise UpstreamBroken("paging does not advance: the page token came back")
        tokens.add(token)
        return token

    # ------------------------------------------------------------------ public

    async def verify(self) -> str:
        """The account itself: status, currency and time zone, the cheapest authenticated call."""
        body, _, _ = await self._search(
            "SELECT customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone, customer.status FROM customer", None)
        rows = body.get("results")
        customer = rows[0].get("customer") if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        if not isinstance(customer, dict):
            raise UpstreamBroken("the account query returned no customer")
        status = customer.get("status")
        if status != "ENABLED":
            raise UpstreamBroken("customer %s is not enabled (status %s)" % (self.customer_id, status))
        currency = customer.get("currencyCode")
        if isinstance(currency, str) and _CURRENCY_RE.match(currency):
            self.currency = currency
        if isinstance(customer.get("timeZone"), str):
            self.time_zone = customer["timeZone"]
        return "Google Ads customer %s%s" % (self.customer_id, " (%s)" % ", ".join(
            x for x in (self.currency, self.time_zone) if x) if self.currency or self.time_zone else "")

    def _check_window(self, start: date, end: date) -> None:
        if end <= start:
            raise ValueError("the window must hold at least one day")

    async def fetch(self, *, window_start: date, window_end: date, cursor: str | None = None) -> AsyncIterator[FetchResult]:
        """One canonical row per day and metric for [window_start, window_end), page by page.

        The window is half open here and GAQL's `<=` is inclusive, so the end is moved back one day. The cursor is
        `m:<nextPageToken>` inside the account query, and `l:` once it is finished and only the leads remain.
        """
        self._check_window(window_start, window_end)
        if cursor and not cursor.startswith(("m:", "l:")):
            raise ValueError("this cursor was not issued by the Google Ads connector")
        if cursor != "l:":
            token = cursor[2:] if cursor and cursor.startswith("m:") else None
            seen: set[date] = set()
            currencies: set[str] = set()
            tokens: set[str] = set()
            pages = 0
            while True:
                pages += 1
                if pages > MAX_PAGES:
                    raise UpstreamBroken("more than %d pages in one window" % MAX_PAGES)
                payload, calls, waits = await self._search(self._query(window_start, window_end), token)
                points = self._points(payload, window_start, window_end, seen, currencies)
                following = self._next_token(payload, token, tokens)
                if following is None and self.local_services:
                    yield FetchResult(points=points, cursor="l:", api_calls=calls, throttle_waits=waits)
                    break
                yield FetchResult(points=points, cursor="m:" + following if following else None, api_calls=calls, throttle_waits=waits)
                if following is None:
                    return
                token = following
        # Local Services Ads leads: counted over every page before anything is written, since one day can span two pages.
        counts: dict[date, int] = {}
        token = None
        tokens = set()
        calls_total = waits_total = pages = 0
        while True:
            pages += 1
            if pages > MAX_PAGES:
                raise UpstreamBroken("more than %d pages of leads in one window" % MAX_PAGES)
            payload, calls, waits = await self._search(self._lead_query(window_start, window_end), token)
            calls_total, waits_total = calls_total + calls, waits_total + waits
            self._lead_days(payload, window_start, window_end, counts)
            token = self._next_token(payload, token, tokens)
            if token is None:
                break
        raw = {"source": "search", "resource": "local_services_lead"}
        day = window_start
        points = []
        while day < window_end:
            points.append(CanonicalPoint("ad_leads", day, counts.get(day, 0), None, raw))
            day += timedelta(days=1)
        yield FetchResult(points=points, cursor=None, api_calls=calls_total, throttle_waits=waits_total)

