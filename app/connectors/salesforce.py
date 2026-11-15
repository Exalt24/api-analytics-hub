"""Salesforce connector: leads and opportunities per day, and the lead rows the matching step reads.

The eleventh connector behind the contract in `base.py`, and the first for a CRM. It reads the REST API's SOQL query resource and writes five daily keys:

    sf_leads_created          Lead rows by `CreatedDate`, the UTC day
    sf_leads_converted        Lead rows with `IsConverted` true, by `ConvertedDate` (a date, no time zone)
    sf_opportunities_created  Opportunity rows by `CreatedDate`, the UTC day
    sf_opportunities_won      Opportunity rows with `IsWon` true, by `CloseDate`
    sf_won_amount             the `Amount` of those won opportunities (money, in the connection's currency)

`leads()` returns the Lead rows themselves (Id, CreatedDate, Email, Phone, MobilePhone, LeadSource, IsConverted) for `app/lead_match.py`, which dedupes them
and attributes them to a call and a source.

It has been run only against the stand-in server in `salesforce_fake.py`, never against a live org: no Salesforce org is held (a Developer Edition sign-up asks
for a human check). Every fact about the calls, with the page it came from and what is not confirmed, is in docs/AD_AND_BUSINESS_SOURCES.md (section Salesforce).

WHAT IT CALLS. `GET {instance}/services/data/vXX.X/query?q=<SOQL>` with `Authorization: Bearer` and `Sforce-Query-Options: batchSize=N`, then each
`nextRecordsUrl` until `done` is true. A `nextRecordsUrl` that is not a path under `/services/data/` is never followed, so the token cannot be sent to another host.
`GET .../sobjects/{Object}/describe` is read once per object first: it proves the fields the queries need exist and are visible to this user, and tells a
multi-currency org (a `CurrencyIsoCode` field) from a single-currency one. Datetime and date literals are written unquoted, as the SOQL date format page gives them.

THE TOKEN. Either an already issued `access_token` (no refresh: a 401 is `AuthExpired`) or the OAuth 2.0 refresh token flow: `POST {login}/services/oauth2/token`
with `grant_type=refresh_token` and the client id, secret and refresh token in the BODY (the page says never the query string). The response carries no expiry, so
the token is refreshed when a request is answered 401, once, behind a lock (the page warns that simultaneous requests with one refresh token fail), and the
request repeated once. If the connected app has refresh token rotation on, the response carries a NEW refresh token and the old one is dead: it is passed to
`on_rotate` and held in `rotated_refresh_token`, and persisting it is the caller's job. `invalid_grant`, `inactive_user` and `invalid_client*` mean a human must
reconnect: `AuthRevoked`.

LIMITS. Every data response carries `Sforce-Limit-Info: api-usage=used/limit`. The connector stops with `RateLimited` BEFORE a call when fewer than `min_remaining`
calls are left, so a sync never starves the org's other users, and a 403 `REQUEST_LIMIT_EXCEEDED` is `RateLimited` at once (the limit rolls, waiting a minute does not
help). 500, 502 and 503 are retried with a bound.

MONEY. `Amount` is a JSON number read as an exact decimal; it may be null (counted as won, adds 0) and may be negative. In a multi-currency org each row names its
`CurrencyIsoCode`; a row in a currency other than the connection's is refused, never added in.

THE TOKEN AND THE ORG'S DATA never appear in an error message: only the HTTP status and Salesforce's `errorCode` (an upper case word) are quoted.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Callable, Mapping
from urllib.parse import urlparse

import httpx

from app.crypto import Secret
from app.lead_match import Lead

from .base import AuthExpired, AuthRevoked, CanonicalPoint, Connector, FetchResult, RateLimited, UpstreamBroken
from .common import Retrier, Retry, minor_units, retry_after
from .ga4 import settled_window_end  # noqa: F401 - the same window rule, re-exported for the sync script

LOGIN_URL = "https://login.salesforce.com"
API_VERSION = "v62.0"
METRICS = ("sf_leads_created", "sf_leads_converted", "sf_opportunities_created", "sf_opportunities_won", "sf_won_amount")
SUPPORTED = frozenset(METRICS)
#: "The default is 2,000; the minimum is 200, and the maximum is 2,000."
MAX_BATCH = 2000
MAX_PAGES = 500
#: Stop before a call when fewer than this many of the org's daily calls are left.
DEFAULT_MIN_REMAINING = 50

_ID_RE = re.compile(r"^[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?(Z|[+-]\d{4}|[+-]\d{2}:\d{2})$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_VERSION_RE = re.compile(r"^v\d{2,3}\.0$")
_ERROR_CODE_RE = re.compile(r"^[A-Za-z_]{1,60}$")
_LIMIT_RE = re.compile(r"api-usage=(\d+)/(\d+)")
_OAUTH_CODE_RE = re.compile(r"^[a-z_]{1,40}$")
#: The field and type each query depends on, per object (the describe check).
REQUIRED_FIELDS: dict[str, dict[str, str]] = {
    "Lead": {"Id": "id", "CreatedDate": "datetime", "IsConverted": "boolean", "ConvertedDate": "date", "Email": "email", "Phone": "phone",
             "MobilePhone": "phone", "LeadSource": "picklist"},
    "Opportunity": {"Id": "id", "CreatedDate": "datetime", "IsWon": "boolean", "CloseDate": "date", "Amount": "currency"},
}


def _decimal_json(text: str) -> Any:
    return json.loads(text, parse_float=Decimal)


def _error_codes(body: Any) -> list[str]:
    """Salesforce's REST error body is a list of {message, errorCode}; a single object is read too. Only the code is kept."""
    items = body if isinstance(body, list) else [body] if isinstance(body, dict) else []
    codes = []
    for item in items:
        code = item.get("errorCode") if isinstance(item, dict) else None
        if isinstance(code, str) and _ERROR_CODE_RE.match(code):
            codes.append(code)
    return codes


def parse_datetime_utc(text: Any, what: str) -> datetime:
    """Salesforce's JSON datetime (`2026-10-01T12:34:56.000+0000`) as an aware UTC datetime."""
    m = _DATETIME_RE.match(text) if isinstance(text, str) else None
    if not m:
        raise UpstreamBroken("%s is not a Salesforce datetime" % what)
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    zone = m.group(8)
    if zone == "Z":
        offset = timedelta(0)
    else:
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        offset = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    try:
        local = datetime(year, month, day, hour, minute, second, tzinfo=timezone(offset))
    except ValueError:
        raise UpstreamBroken("%s is not a calendar datetime" % what) from None
    return local.astimezone(timezone.utc)


def parse_date(text: Any, what: str) -> date:
    if not isinstance(text, str) or not _DATE_RE.match(text):
        raise UpstreamBroken("%s is not a YYYY-MM-DD date" % what)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise UpstreamBroken("%s is not a calendar date" % what) from None


def _soql_datetime(day: date) -> str:
    return day.isoformat() + "T00:00:00Z"


def _checked_url(value: str, what: str, *, https_only_unless_loopback: bool = True) -> str:
    parsed = urlparse(value.strip())
    loopback = parsed.hostname in ("127.0.0.1", "localhost")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("%s must be a bare https address such as https://acme.my.salesforce.com, got %r" % (what, value))
    if parsed.scheme != "https" and not (loopback and parsed.scheme == "http"):
        raise ValueError("%s must use https" % what)
    return "%s://%s" % (parsed.scheme, parsed.netloc)


class SalesforceTokens:
    """The OAuth 2.0 refresh token flow, behind one lock. See the module note."""

    def __init__(self, *, client_id: str, client_secret: str, refresh_token: str, login_url: str, client: httpx.AsyncClient, retrier: Retrier,
                 on_rotate: Callable[[str], None] | None = None):
        self._id = client_id
        self._secret = Secret(client_secret)
        self._refresh = Secret(refresh_token)
        self._login = login_url
        self._client = client
        self._retrier = retrier
        self._on_rotate = on_rotate
        self._access: str | None = None
        self._lock = asyncio.Lock()
        self.refresh_calls = 0
        self.rotated_refresh_token: str | None = None

    @staticmethod
    def _classify(response: httpx.Response) -> Retry | None:
        if response.status_code == 200:
            return None
        try:
            body = response.json()
        except ValueError:
            body = None
        code = body.get("error") if isinstance(body, dict) else None
        code = code if isinstance(code, str) and _OAUTH_CODE_RE.match(code) else ""
        http = response.status_code
        if code in ("invalid_grant", "inactive_user", "invalid_client", "invalid_client_id", "unauthorized_client"):
            raise AuthRevoked("the refresh token or the app was refused (%s), a person must reconnect" % code)
        if http == 429 or http >= 500:
            return Retry(retry_after(response), "HTTP %d %s" % (http, code))
        raise UpstreamBroken("the token endpoint refused the request (HTTP %d %s)" % (http, code))

    async def _mint(self) -> str:
        async def send() -> httpx.Response:
            return await self._client.post(
                self._login + "/services/oauth2/token",
                data={"grant_type": "refresh_token", "client_id": self._id, "client_secret": self._secret.reveal(), "refresh_token": self._refresh.reveal()},
                headers={"Accept": "application/json"})

        response, _, _ = await self._retrier.run(send, self._classify)
        self.refresh_calls += 1
        try:
            body = response.json()
        except ValueError:
            raise UpstreamBroken("the token endpoint answered 200 with a body that is not JSON") from None
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise UpstreamBroken("the token endpoint answered 200 with no access_token")
        if body.get("token_type") != "Bearer":
            raise UpstreamBroken("the token endpoint answered a token_type that is not Bearer")
        rotated = body.get("refresh_token")
        if rotated is not None:
            if not isinstance(rotated, str) or not rotated:
                raise UpstreamBroken("the token endpoint answered a refresh_token that is not text")
            self._refresh = Secret(rotated)            # the old one is dead the moment this arrives
            self.rotated_refresh_token = rotated
            if self._on_rotate is not None:
                self._on_rotate(rotated)
        self._access = token
        return token

    async def access_token(self) -> str:
        async with self._lock:
            return self._access if self._access is not None else await self._mint()

    async def refresh_after(self, rejected: str) -> str:
        """A new token after `rejected` was refused. If another request already replaced it, that one is used and no second refresh is made."""
        async with self._lock:
            if self._access is not None and self._access != rejected:
                return self._access
            return await self._mint()


class SalesforceConnector(Connector):
    platform = "salesforce"
    supported_metrics = SUPPORTED

    def __init__(
        self,
        *,
        instance_url: str,
        currency: str,
        access_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        login_url: str = LOGIN_URL,
        api_version: str = API_VERSION,
        batch_size: int = MAX_BATCH,
        min_remaining: int = DEFAULT_MIN_REMAINING,
        on_rotate: Callable[[str], None] | None = None,
        client: httpx.AsyncClient | None = None,
        retrier: Retrier | None = None,
        **retry_options: Any,
    ):
        if not _CURRENCY_RE.match(str(currency)):
            raise ValueError("currency must be an ISO 4217 code such as USD (Salesforce does not tell a single-currency org's currency in the rows)")
        refresh_parts = (client_id, client_secret, refresh_token)
        have_refresh = [p is not None for p in refresh_parts]
        if access_token is not None and any(have_refresh) or access_token is None and not all(have_refresh):
            raise ValueError("give either access_token, or client_id, client_secret and refresh_token together")
        if not _VERSION_RE.match(api_version):
            raise ValueError("api_version must look like v62.0, got %r" % (api_version,))
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_BATCH:
            raise ValueError("batch_size must be 1 to %d" % MAX_BATCH)
        if isinstance(min_remaining, bool) or not isinstance(min_remaining, int) or min_remaining < 0:
            raise ValueError("min_remaining must be a whole number of at least 0")
        self.currency = str(currency)
        self._base = _checked_url(instance_url, "instance_url")
        self._api = "/services/data/%s" % api_version
        self._batch = batch_size
        self._min_remaining = min_remaining
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._retrier = retrier or Retrier(**retry_options)
        self._static = Secret(access_token) if access_token is not None else None
        self._tokens = (SalesforceTokens(client_id=client_id, client_secret=client_secret, refresh_token=refresh_token,
                                         login_url=_checked_url(login_url, "login_url"), client=self._client, retrier=self._retrier, on_rotate=on_rotate)
                        if access_token is None else None)
        self.api_usage: tuple[int, int] | None = None
        self._describes: dict[str, dict[str, dict]] = {}
        #: Calls and waits spent outside a query (the describes), added to the next fetch's totals so api_calls counts every request.
        self._spent = [0, 0]
        self.degraded_metrics: set[str] = set()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "SalesforceConnector":
        """Build from SALESFORCE_INSTANCE_URL, SALESFORCE_CURRENCY and the three refresh flow secrets (SALESFORCE_LOGIN_URL for a stand-in)."""
        env = os.environ if env is None else env
        names = ("SALESFORCE_INSTANCE_URL", "SALESFORCE_CURRENCY", "SALESFORCE_CLIENT_ID", "SALESFORCE_CLIENT_SECRET", "SALESFORCE_REFRESH_TOKEN")
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise ValueError("set %s in the environment" % ", ".join(missing))
        if env.get("SALESFORCE_LOGIN_URL"):
            kwargs.setdefault("login_url", env["SALESFORCE_LOGIN_URL"])
        return cls(instance_url=env["SALESFORCE_INSTANCE_URL"], currency=env["SALESFORCE_CURRENCY"], client_id=env["SALESFORCE_CLIENT_ID"],
                   client_secret=env["SALESFORCE_CLIENT_SECRET"], refresh_token=env["SALESFORCE_REFRESH_TOKEN"], **kwargs)

    def __repr__(self) -> str:
        return "SalesforceConnector(instance=%r)" % self._base

    @property
    def external_account(self) -> str:
        return urlparse(self._base).netloc

    @property
    def refresh_rotated(self) -> bool:
        return bool(self._tokens and self._tokens.rotated_refresh_token)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ transport

    async def _bearer(self) -> str:
        token = self._static.reveal() if self._static is not None else await self._tokens.access_token()
        if not token or not isinstance(token, str):
            raise AuthExpired("no access token is available")
        return token

    def _note_limit(self, response: httpx.Response) -> None:
        m = _LIMIT_RE.search(response.headers.get("Sforce-Limit-Info", ""))
        if m and int(m.group(2)) > 0:
            self.api_usage = (int(m.group(1)), int(m.group(2)))

    def _classify(self, response: httpx.Response) -> Retry | None:
        self._note_limit(response)
        if response.status_code == 200:
            return None
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        codes = _error_codes(body)
        http = response.status_code
        text = ("HTTP %d %s" % (http, " ".join(codes[:2]))).strip()
        if http == 401:
            raise AuthExpired("the access token was refused (%s)" % text)
        if http == 403 and "REQUEST_LIMIT_EXCEEDED" in codes:
            raise RateLimited(None, "the org's daily API request limit is used up (%s)" % text)
        if http == 403:
            raise AuthRevoked("this user is not allowed to read that (%s)" % text)
        if http in (500, 502, 503):
            return Retry(retry_after(response), text)
        raise UpstreamBroken("Salesforce refused the request (%s)" % text)

    def _pace(self) -> None:
        if self.api_usage is not None and self._min_remaining:
            used, limit = self.api_usage
            if limit - used < self._min_remaining:
                raise RateLimited(None, "only %d of the org's %d daily API calls are left, stopping to leave them to the org's other users" % (max(limit - used, 0), limit))

    async def _get(self, path: str, params: Mapping[str, str] | None = None) -> tuple[Any, int, int]:
        self._pace()
        if not path.startswith("/services/data/"):
            raise UpstreamBroken("refusing to call a path outside /services/data/")
        url = self._base + path
        calls = waits = 0
        for attempt in (0, 1):
            token = await self._bearer()

            async def send() -> httpx.Response:
                return await self._client.get(url, params=params, headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                                                                          "Sforce-Query-Options": "batchSize=%d" % self._batch})
            try:
                response, c, w = await self._retrier.run(send, self._classify)
            except AuthExpired:
                if attempt == 0 and self._tokens is not None:
                    await self._tokens.refresh_after(token)
                    continue
                raise
            calls, waits = calls + c, waits + w
            try:
                body = _decimal_json(response.text)
            except ValueError:
                raise UpstreamBroken("Salesforce answered 200 with a body that is not JSON") from None
            return body, calls, waits
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ describe

    async def _describe(self, name: str) -> dict[str, dict]:
        """The object's fields by name, after checking the fields the queries need exist with the right type."""
        if name in self._describes:
            return self._describes[name]
        body, c, w = await self._get("%s/sobjects/%s/describe" % (self._api, name))
        self._spent[0] += c
        self._spent[1] += w
        if not isinstance(body, dict) or body.get("name") != name or not isinstance(body.get("fields"), list):
            raise UpstreamBroken("the describe answer for %s is not an object with its name and a list of fields" % name)
        fields: dict[str, dict] = {}
        for field in body["fields"]:
            if isinstance(field, dict) and isinstance(field.get("name"), str):
                fields[field["name"]] = field
        for field_name, kind in REQUIRED_FIELDS[name].items():
            found = fields.get(field_name)
            if found is None:
                raise UpstreamBroken("%s has no field %s visible to this user (field level security or a different object)" % (name, field_name))
            if field_name != "Id" and found.get("type") != kind and not (kind in ("email", "phone", "picklist") and found.get("type") in ("string", kind)):
                raise UpstreamBroken("%s.%s is a %s field, the connector reads a %s" % (name, field_name, found.get("type"), kind))
        self._describes[name] = fields
        return fields

    async def verify(self) -> str:
        """Describe Lead and Opportunity, and check the lead to opportunity relationship the conversion counts rely on."""
        lead = await self._describe("Lead")
        await self._describe("Opportunity")
        link = lead.get("ConvertedOpportunityId")
        if not isinstance(link, dict) or link.get("referenceTo") != ["Opportunity"]:
            raise UpstreamBroken("Lead.ConvertedOpportunityId does not point at Opportunity")
        return "Salesforce org at %s" % self.external_account

    # ------------------------------------------------------------------ query

    async def _query(self, soql: str, expected: str) -> tuple[list[dict], int, int]:
        body, calls, waits = await self._get("%s/query" % self._api, {"q": soql})
        records: list[dict] = []
        seen_urls: set[str] = set()
        for _ in range(MAX_PAGES):
            if not isinstance(body, dict) or not isinstance(body.get("records"), list) or not isinstance(body.get("done"), bool) \
                    or isinstance(body.get("totalSize"), bool) or not isinstance(body.get("totalSize"), int):
                raise UpstreamBroken("the query answer lacks totalSize, done or records")
            records += body["records"]
            if body["done"]:
                if body.get("nextRecordsUrl"):
                    raise UpstreamBroken("the query says done and also names a next page")
                break
            nxt = body.get("nextRecordsUrl")
            if not isinstance(nxt, str) or not nxt.startswith("/services/data/") or nxt in seen_urls or ".." in nxt:
                raise UpstreamBroken("the query is not done and names no usable next page")
            seen_urls.add(nxt)
            body, c, w = await self._get(nxt)
            calls, waits = calls + c, waits + w
        else:
            raise UpstreamBroken("more than %d pages in one query" % MAX_PAGES)
        if body["totalSize"] != len(records):
            raise UpstreamBroken("totalSize says %d records and %d arrived" % (body["totalSize"], len(records)))
        ids: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise UpstreamBroken("a record is %s, not an object" % type(record).__name__)
            attributes = record.get("attributes")
            if not isinstance(attributes, dict) or attributes.get("type") != expected:
                raise UpstreamBroken("a record is not a %s" % expected)
            ident = record.get("Id")
            if not isinstance(ident, str) or not _ID_RE.match(ident):
                raise UpstreamBroken("a %s has no usable Id" % expected)
            if ident in ids:
                raise UpstreamBroken("%s %s appears twice" % (expected, ident))
            ids.add(ident)
        return records, calls, waits

    @staticmethod
    def _range(field: str, start: date, end: date, *, as_datetime: bool) -> str:
        lo, hi = (_soql_datetime(start), _soql_datetime(end)) if as_datetime else (start.isoformat(), end.isoformat())
        return "%s >= %s AND %s < %s" % (field, lo, field, hi)

    @staticmethod
    def _day_of(record: dict, field: str, what: str, start: date, end: date, *, as_datetime: bool) -> date:
        raw = record.get(field)
        day = parse_datetime_utc(raw, what).date() if as_datetime else parse_date(raw, what)
        if not start <= day < end:
            raise UpstreamBroken("%s is outside the window" % what)
        return day

    async def fetch(self, *, window_start: date, window_end: date, cursor: str | None = None) -> AsyncIterator[FetchResult]:
        """Five canonical rows per day for [window_start, window_end), written once every query is read.

        There is no resume cursor (a query locator is not safe to store), so a cursor passed in is refused rather than ignored. A day with nothing is
        written as zeros because every query was answered for the whole window.
        """
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        if cursor:
            raise ValueError("the Salesforce connector issues no cursor, so none can be resumed")
        refreshed_before = self._tokens.refresh_calls if self._tokens else 0
        await self._describe("Lead")
        opp_fields = await self._describe("Opportunity")
        multi = "CurrencyIsoCode" in opp_fields
        s, e = window_start, window_end
        counts: dict[tuple[str, date], int] = {}
        amounts: dict[date, int] = {}
        calls, waits = self._spent
        self._spent = [0, 0]

        def bump(key: str, day: date) -> None:
            counts[(key, day)] = counts.get((key, day), 0) + 1

        rows, c, w = await self._query("SELECT Id, CreatedDate FROM Lead WHERE %s ORDER BY CreatedDate, Id" % self._range("CreatedDate", s, e, as_datetime=True), "Lead")
        calls, waits = calls + c, waits + w
        for r in rows:
            bump("sf_leads_created", self._day_of(r, "CreatedDate", "a lead's CreatedDate", s, e, as_datetime=True))
        rows, c, w = await self._query(
            "SELECT Id, IsConverted, ConvertedDate FROM Lead WHERE IsConverted = true AND %s ORDER BY ConvertedDate, Id" % self._range("ConvertedDate", s, e, as_datetime=False), "Lead")
        calls, waits = calls + c, waits + w
        for r in rows:
            if r.get("IsConverted") is not True:
                raise UpstreamBroken("a lead in the converted query is not converted")
            bump("sf_leads_converted", self._day_of(r, "ConvertedDate", "a lead's ConvertedDate", s, e, as_datetime=False))
        rows, c, w = await self._query("SELECT Id, CreatedDate FROM Opportunity WHERE %s ORDER BY CreatedDate, Id" % self._range("CreatedDate", s, e, as_datetime=True), "Opportunity")
        calls, waits = calls + c, waits + w
        for r in rows:
            bump("sf_opportunities_created", self._day_of(r, "CreatedDate", "an opportunity's CreatedDate", s, e, as_datetime=True))
        fields = "Id, IsWon, CloseDate, Amount" + (", CurrencyIsoCode" if multi else "")
        rows, c, w = await self._query(
            "SELECT %s FROM Opportunity WHERE IsWon = true AND %s ORDER BY CloseDate, Id" % (fields, self._range("CloseDate", s, e, as_datetime=False)), "Opportunity")
        calls, waits = calls + c, waits + w
        for r in rows:
            if r.get("IsWon") is not True:
                raise UpstreamBroken("an opportunity in the won query is not won")
            day = self._day_of(r, "CloseDate", "an opportunity's CloseDate", s, e, as_datetime=False)
            if multi and r.get("CurrencyIsoCode") != self.currency:
                raise UpstreamBroken("opportunity %s is in %s and this connection sums %s: refusing to add across currencies"
                                     % (r["Id"], r.get("CurrencyIsoCode") if isinstance(r.get("CurrencyIsoCode"), str) else "an unnamed currency", self.currency))
            amount = r.get("Amount")
            bump("sf_opportunities_won", day)
            if amount is not None:
                amounts[day] = amounts.get(day, 0) + minor_units(amount, self.currency, "Amount of opportunity %s" % r["Id"], allow_negative=True)
        raw = {"source": "query", "instance": self.external_account, "api": self._api.rsplit("/", 1)[1]}
        points: list[CanonicalPoint] = []
        day = window_start
        while day < window_end:
            for key in METRICS[:4]:
                points.append(CanonicalPoint(key, day, counts.get((key, day), 0), None, raw))
            points.append(CanonicalPoint("sf_won_amount", day, amounts.get(day, 0), self.currency, raw))
            day += timedelta(days=1)
        calls += (self._tokens.refresh_calls - refreshed_before) if self._tokens else 0
        yield FetchResult(points=points, cursor=None, api_calls=calls, throttle_waits=waits)

    async def leads(self, *, window_start: date, window_end: date) -> list[Lead]:
        """The Lead rows created in [window_start, window_end), as `Lead` records for the matching step."""
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        await self._describe("Lead")
        rows, _, _ = await self._query(
            "SELECT Id, CreatedDate, Email, Phone, MobilePhone, LeadSource, IsConverted FROM Lead WHERE %s ORDER BY CreatedDate, Id"
            % self._range("CreatedDate", window_start, window_end, as_datetime=True), "Lead")
        out = []
        for r in rows:
            created = parse_datetime_utc(r.get("CreatedDate"), "a lead's CreatedDate")
            if not window_start <= created.date() < window_end:
                raise UpstreamBroken("a lead's CreatedDate is outside the window")
            for field in ("Email", "Phone", "MobilePhone", "LeadSource"):
                if r.get(field) is not None and not isinstance(r[field], str):
                    raise UpstreamBroken("a lead's %s is not text" % field)
            if not isinstance(r.get("IsConverted"), bool):
                raise UpstreamBroken("a lead's IsConverted is not true or false")
            out.append(Lead(r["Id"], created, r.get("Email"), tuple(p for p in (r.get("Phone"), r.get("MobilePhone")) if p), r.get("LeadSource"), r["IsConverted"]))
        return out
