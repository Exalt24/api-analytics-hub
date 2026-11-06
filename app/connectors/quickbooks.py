"""QuickBooks Online connector: invoices per day, by the invoice date.

The eighth connector behind the contract in `base.py`, for a business system. It reads the Accounting API's query endpoint
and writes three daily keys: `invoices` (how many invoices carry that `TxnDate`), `invoiced_amount` (the sum of their `TotalAmt`) and
`invoice_open_balance` (the sum of their `Balance` as of the sync, so it falls as customers pay and a later sync restates it). It has
been run only against the stand-in server in `quickbooks_fake.py`, never against a live company or the Intuit sandbox: no account is held.
Every fact about the calls, with the page it came from and what is not confirmed, is in docs/AD_AND_BUSINESS_SOURCES.md.

WHAT IT CALLS. `GET {base}/v3/company/{realmId}/query?query=<select>` with `Accept: application/json`, the query
`SELECT * FROM Invoice WHERE TxnDate >= '..' AND TxnDate < '..' ORDERBY TxnDate, Id STARTPOSITION n MAXRESULTS 1000`, and the
response's `QueryResponse.Invoice` list. The maximum a response holds is 1,000, so a longer result is paged by `STARTPOSITION`. The
order is named so that a page boundary cannot move a row from one page to the next. `GET .../companyinfo/{realmId}` is the cheap
authenticated call behind `verify()`.

THE FAULT IS IN THE BODY. The errors page says a 200 can carry a `Fault`, and gives its types: `ValidationFault` (fix the
request), `SystemFault` (their servers), `AuthenticationFault` and `AuthorizationFault`. The body is read before the status, and a
fault is classified by its type, so a `Fault` inside a 200 is an expired token and not an empty success.

MONEY. `TotalAmt` and `Balance` are JSON numbers, so the response is parsed with `parse_float=Decimal`. The currency is the invoice's own
`CurrencyRef.value`; an invoice without one is refused, and so is a window whose invoices name two. A voided invoice (`PrivateNote` holds "Voided"
and every amount is zeroed, per the Void operation page) is not counted: it is not an invoice the shop issued.

THE WINDOW IS ACCUMULATED, THEN WRITTEN. A day's total needs every invoice dated that day and a day can span two pages, so the connector reads all
pages and yields once, as the Square connector does. A day with no invoice is written as zeros, because the query was answered for the whole window.

RATE LIMITS. 500 requests a minute per realm, 10 a second, and "wait 60 seconds" on a 429, per the limits page. A wait longer than `backoff_cap`
is handed back as `RateLimited` for the runner to own.

THE TOKEN. An already issued OAuth 2.0 access token, held in a `Secret`, sent only in the `Authorization` header. No URL, query or response body is put
in an error message. Getting or refreshing a token is not built.
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
from .common import Retrier, Retry, minor_units, retry_after
from .ga4 import settled_window_end  # noqa: F401 - the same window rule, re-exported for the sync script

BASE_URL = "https://quickbooks.api.intuit.com"
SANDBOX_URL = "https://sandbox-quickbooks.api.intuit.com"
SUPPORTED = frozenset({"invoices", "invoiced_amount", "invoice_open_balance"})
#: "The maximum number of entities that can be returned in a response is 1,000."
MAX_RESULTS = 1000
MAX_PAGES = 200

_REALM_RE = re.compile(r"^\d{1,25}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _decimal_json(text: str) -> Any:
    return json.loads(text, parse_float=Decimal)


def _fault_of(body: Any) -> dict | None:
    """The Fault object of a body, wherever the JSON form puts it (top level or inside QueryResponse)."""
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("Fault"), dict):
        return body["Fault"]
    inner = body.get("QueryResponse")
    if isinstance(inner, dict) and isinstance(inner.get("Fault"), dict):
        return inner["Fault"]
    return None


def _fault_text(fault: dict) -> str:
    errors = fault.get("Error")
    first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    return ("%s code %s: %s" % (fault.get("type") or "Fault", first.get("code", "?"), str(first.get("Message") or "")[:100])).strip()[:200]


class QuickBooksConnector(Connector):
    platform = "quickbooks"
    supported_metrics = SUPPORTED

    def __init__(
        self,
        *,
        realm_id: str,
        access_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        minor_version: int | None = None,
        currency: str | None = None,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        retrier: Retrier | None = None,
        **retry_options: Any,
    ):
        if not _REALM_RE.match(str(realm_id).strip()):
            raise ValueError("realm_id must be the numeric company (realm) id, got %r" % (realm_id,))
        if (access_token is None) == (token_provider is None):
            raise ValueError("give exactly one of access_token and token_provider")
        if minor_version is not None and (isinstance(minor_version, bool) or not isinstance(minor_version, int) or minor_version < 1):
            raise ValueError("minor_version must be a positive integer or None")
        if currency is not None and not _CURRENCY_RE.match(str(currency)):
            raise ValueError("currency must be an ISO 4217 code such as USD, or None")
        self.realm_id = str(realm_id).strip()
        #: The company's home currency when the caller knows it. It is what a window with no invoice at all is written in, and any
        #: invoice in another currency is refused against it. Without it, a currency is learned from the first invoice.
        self.currency = currency
        self._token = Secret(access_token) if access_token is not None else None
        self._token_provider = token_provider
        self._minor = minor_version
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        # The limits page says to wait 60 seconds after a 429; the default cap is below that, so the runner owns that wait.
        self._retrier = retrier or Retrier(**retry_options)
        self.company: str | None = None
        self.degraded_metrics: set[str] = set()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "QuickBooksConnector":
        """Build from QUICKBOOKS_REALM_ID and QUICKBOOKS_ACCESS_TOKEN (and QUICKBOOKS_API_BASE_URL for the sandbox or a stand-in)."""
        env = os.environ if env is None else env
        missing = [n for n in ("QUICKBOOKS_REALM_ID", "QUICKBOOKS_ACCESS_TOKEN") if not env.get(n)]
        if missing:
            raise ValueError("set %s in the environment" % " and ".join(missing))
        if env.get("QUICKBOOKS_API_BASE_URL"):
            kwargs.setdefault("base_url", env["QUICKBOOKS_API_BASE_URL"])
        if env.get("QUICKBOOKS_CURRENCY"):
            kwargs.setdefault("currency", env["QUICKBOOKS_CURRENCY"])
        return cls(realm_id=env["QUICKBOOKS_REALM_ID"], access_token=env["QUICKBOOKS_ACCESS_TOKEN"], **kwargs)

    def __repr__(self) -> str:
        return "QuickBooksConnector(realm_id=%r, base_url=%r)" % (self.realm_id, self._base)

    @property
    def external_account(self) -> str:
        return self.realm_id

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
        fault = _fault_of(body)
        http = response.status_code
        if fault is None and http == 200:
            return None
        kind = str(fault.get("type")) if fault else ""
        text = _fault_text(fault) if fault else "HTTP %d" % http
        if kind == "AuthenticationFault" or (not kind and http == 401):
            raise AuthExpired("the access token was rejected (%s)" % text)
        if kind == "AuthorizationFault" or (not kind and http == 403):
            raise AuthRevoked("no access to company %s (%s)" % (self.realm_id, text))
        if kind == "SystemFault" or http == 429 or http >= 500:
            # The limits page: "If you see this, wait 60 seconds before retrying the request."
            wait = retry_after(response)
            return Retry(wait if wait is not None else (60.0 if http == 429 else None), text)
        raise UpstreamBroken("QuickBooks refused the request (%s)" % text)

    async def _get(self, path: str, params: Mapping[str, str]) -> tuple[dict, int, int]:
        url = "%s/v3/company/%s%s" % (self._base, self.realm_id, path)
        query = dict(params)
        if self._minor is not None:
            query["minorversion"] = str(self._minor)

        async def send() -> httpx.Response:
            return await self._client.get(url, params=query, headers={"Authorization": "Bearer " + self._bearer(), "Accept": "application/json"})

        response, calls, waits = await self._retrier.run(send, self._classify)
        try:
            body = _decimal_json(response.text)
        except ValueError:
            raise UpstreamBroken("QuickBooks answered 200 with a body that is not JSON") from None
        if not isinstance(body, dict):
            raise UpstreamBroken("QuickBooks answered 200 with %s, not an object" % type(body).__name__)
        return body, calls, waits

    # ------------------------------------------------------------------ public

    async def verify(self) -> str:
        """The company itself, the cheapest authenticated call."""
        body, _, _ = await self._get("/companyinfo/%s" % self.realm_id, {})
        info = body.get("CompanyInfo")
        if not isinstance(info, dict) or not isinstance(info.get("CompanyName"), str):
            raise UpstreamBroken("the company query returned no CompanyInfo")
        self.company = info["CompanyName"]
        return "QuickBooks company %s (%s)" % (self.realm_id, self.company)

    @staticmethod
    def _query(start: date, end: date, position: int) -> str:
        return ("SELECT * FROM Invoice WHERE TxnDate >= '%s' AND TxnDate < '%s' ORDERBY TxnDate, Id STARTPOSITION %d MAXRESULTS %d"
                % (start.isoformat(), end.isoformat(), position, MAX_RESULTS))

    def _add(self, invoices: Any, start: date, end: date, ids: set[str], totals: dict[date, list[int]], currencies: set[str]) -> None:
        if not isinstance(invoices, list):
            raise UpstreamBroken("Invoice is not a list")
        for invoice in invoices:
            if not isinstance(invoice, dict):
                raise UpstreamBroken("an invoice is %s, not an object" % type(invoice).__name__)
            ident = invoice.get("Id")
            if not isinstance(ident, str) or not ident:
                raise UpstreamBroken("an invoice has no Id")
            text = invoice.get("TxnDate")
            if not isinstance(text, str) or not _DATE_RE.match(text):
                raise UpstreamBroken("an invoice's TxnDate is %r, not YYYY-MM-DD" % (text,))
            try:
                day = date.fromisoformat(text)
            except ValueError:
                raise UpstreamBroken("an invoice's TxnDate %s is not a calendar day" % text) from None
            if not start <= day < end:
                raise UpstreamBroken("an invoice dated %s is outside the window %s to %s" % (day, start, end))
            if ident in ids:
                raise UpstreamBroken("invoice %s appears twice" % ident)
            ids.add(ident)
            ref = invoice.get("CurrencyRef")
            currency = ref.get("value") if isinstance(ref, dict) else None
            if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
                raise UpstreamBroken("invoice %s has no CurrencyRef" % ident)
            currencies.add(currency)
            if len(currencies) > 1:
                raise UpstreamBroken("invoices name two currencies: %s" % ", ".join(sorted(currencies)))
            total = minor_units(invoice.get("TotalAmt"), currency, "TotalAmt of invoice %s" % ident)
            balance = minor_units(invoice.get("Balance", 0), currency, "Balance of invoice %s" % ident)
            if "Voided" in str(invoice.get("PrivateNote") or "") and total == 0:
                continue
            row = totals.setdefault(day, [0, 0, 0])
            row[0] += 1
            row[1] += total
            row[2] += balance

    async def fetch(self, *, window_start: date, window_end: date, cursor: str | None = None) -> AsyncIterator[FetchResult]:
        """Three canonical rows per day for [window_start, window_end), written once the whole window is read.

        The window is half open and the query's `<` is exclusive, so the end is used as it is. There is no resume cursor (see the module
        note), so a cursor passed in is refused rather than ignored.
        """
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        if cursor:
            raise ValueError("the QuickBooks connector issues no cursor, so none can be resumed")
        ids: set[str] = set()
        totals: dict[date, list[int]] = {}
        currencies: set[str] = {self.currency} if self.currency else set()
        position, calls, waits = 1, 0, 0
        for _ in range(MAX_PAGES):
            body, c, w = await self._get("/query", {"query": self._query(window_start, window_end, position)})
            calls, waits = calls + c, waits + w
            inner = body.get("QueryResponse")
            if not isinstance(inner, dict):
                raise UpstreamBroken("QueryResponse is not an object")
            invoices = inner.get("Invoice", [])
            self._add(invoices, window_start, window_end, ids, totals, currencies)
            if len(invoices) < MAX_RESULTS:
                break
            position += MAX_RESULTS
            yield FetchResult(points=[], cursor=None, api_calls=calls, throttle_waits=waits)
            calls = waits = 0
        else:
            raise UpstreamBroken("more than %d pages in one window" % MAX_PAGES)
        currency = next(iter(currencies), None)
        raw = {"source": "query", "entity": "Invoice", "realm_id": self.realm_id}
        points: list[CanonicalPoint] = []
        day = window_start
        while day < window_end:
            n, amount, balance = totals.get(day, [0, 0, 0])
            points.append(CanonicalPoint("invoices", day, n, None, raw))
            if currency is not None:
                # With no invoice in the window and no configured currency there is none to put beside a zero, and money without one is refused.
                points += [CanonicalPoint("invoiced_amount", day, amount, currency, raw),
                           CanonicalPoint("invoice_open_balance", day, balance, currency, raw)]
            day += timedelta(days=1)
        yield FetchResult(points=points, cursor=None, api_calls=calls, throttle_waits=waits)
