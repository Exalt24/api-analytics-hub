"""Square connector: completed card and cash payments per day for one location.

The seventh connector behind the contract in `base.py`, and the first for a business system rather than a store or an
ad account. It reads the Payments API and writes three daily keys: `payments` (how many completed payments), `payments_total`
(their total money, tips included) and `payments_refunded` (the refunded part of those payments). It has been run only against
the stand-in server in `square_fake.py`, never against a live Square account: no account is held. Every fact about the calls,
with the page it came from and what is not confirmed, is in docs/AD_AND_BUSINESS_SOURCES.md.

WHAT IT CALLS. `GET {base}/v2/payments` with `begin_time`, `end_time` (RFC 3339), `location_id`, `sort_order=ASC`, `limit=100`
and the pagination `cursor`; `GET {base}/v2/locations` once, for the location's time zone and currency. The `Square-Version`
header pins the API version in every request and `Authorization: Bearer` carries the token.

A DAY IS A LOCATION DAY. Square stamps a payment in UTC (`created_at`). A shop's day ends at its own midnight, so the window is cut
at local midnight in the location's IANA time zone (the Locations API gives `timezone`), the request asks for that range in UTC, and a
payment is counted on the local date of its `created_at`. Cutting at UTC midnight would move every US evening payment to the next day.

ONLY COMPLETED PAYMENTS. `status` is one of APPROVED, PENDING, COMPLETED, CANCELED or FAILED. Only COMPLETED is a payment the shop
has: an approved card payment is not yet captured, and a canceled or failed one never was. A refund is counted on the day of the
PAYMENT it refunds, because `refunded_money` sits on the payment and says nothing about when the refund happened; the Refunds API
would give that date and is not read here (a stated limit, docs/AD_AND_BUSINESS_SOURCES.md).

THE WINDOW IS ACCUMULATED, THEN WRITTEN. A day's total needs every payment of the day, and one day can span two pages, so the
connector reads every page and yields once at the end. It offers no resume cursor: a failure part way writes nothing for the window
and the rerun reads it whole. That is cheap for one shop's payments, and it can never write half a day.

MONEY. Amounts are integers in the smallest unit already (`amount` in cents), with a `currency`. The location's currency is the one
the Locations page says all its transactions use; a payment in another currency, or two currencies, is refused. A day with no
completed payment is written as zeros, because the query was answered for the whole window.

THE TOKEN. An already issued access token, held in a `Secret`, sent only in the `Authorization` header. No URL, query or response
body is put in an error message.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.crypto import Secret

from .base import AuthExpired, AuthRevoked, CanonicalPoint, Connector, FetchResult, UpstreamBroken
from .common import BIGINT_MAX, Retrier, Retry, retry_after
from .ga4 import settled_window_end  # noqa: F401 - the same window rule, re-exported for the sync script

BASE_URL = "https://connect.squareup.com"
#: Pinned in every request. 2026-09-16 is the newest version the List payments page read (2026-11-06) shows.
API_VERSION = "2026-09-16"
SUPPORTED = frozenset({"payments", "payments_total", "payments_refunded"})
PAGE_LIMIT = 100
#: 1,000 pages of 100 is 100,000 payments in one window: a window that large is cut by the caller, not read here.
MAX_PAGES = 1000

_LOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

_EXPIRED_CODES = frozenset({"ACCESS_TOKEN_EXPIRED", "UNAUTHORIZED"})
_REVOKED_CODES = frozenset({"ACCESS_TOKEN_REVOKED", "CLIENT_DISABLED", "FORBIDDEN", "INSUFFICIENT_SCOPES"})


def _first_error(body: Any) -> dict | None:
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return errors[0]
    return None


def _money(value: Any, what: str) -> tuple[int, str]:
    """(amount, currency) of a Square Money object. The amount is an integer of the smallest unit, never a float or text."""
    if not isinstance(value, dict):
        raise UpstreamBroken("%s is not a money object" % what)
    amount, currency = value.get("amount"), value.get("currency")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise UpstreamBroken("%s amount is %s, not an integer" % (what, type(amount).__name__))
    if amount < 0 or amount > BIGINT_MAX:
        raise UpstreamBroken("%s amount %s is outside what this hub stores" % (what, amount))
    if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
        raise UpstreamBroken("%s has no currency" % what)
    return amount, currency


class SquareConnector(Connector):
    platform = "square"
    supported_metrics = SUPPORTED

    def __init__(
        self,
        *,
        location_id: str,
        access_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        time_zone: str | None = None,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        retrier: Retrier | None = None,
        **retry_options: Any,
    ):
        if not _LOCATION_RE.match(str(location_id).strip()):
            raise ValueError("location_id must be the Square location id, got %r" % (location_id,))
        if (access_token is None) == (token_provider is None):
            raise ValueError("give exactly one of access_token and token_provider")
        self.location_id = str(location_id).strip()
        self._token = Secret(access_token) if access_token is not None else None
        self._token_provider = token_provider
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._retrier = retrier or Retrier(**retry_options)
        #: The location's IANA time zone and currency: given, or read from the Locations API by verify() or the first fetch.
        self.time_zone: str | None = time_zone
        self.currency: str | None = None
        self.degraded_metrics: set[str] = set()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "SquareConnector":
        """Build from SQUARE_LOCATION_ID and SQUARE_ACCESS_TOKEN (and SQUARE_API_BASE_URL for a stand-in)."""
        env = os.environ if env is None else env
        missing = [n for n in ("SQUARE_LOCATION_ID", "SQUARE_ACCESS_TOKEN") if not env.get(n)]
        if missing:
            raise ValueError("set %s in the environment" % " and ".join(missing))
        if env.get("SQUARE_API_BASE_URL"):
            kwargs.setdefault("base_url", env["SQUARE_API_BASE_URL"])
        return cls(location_id=env["SQUARE_LOCATION_ID"], access_token=env["SQUARE_ACCESS_TOKEN"], **kwargs)

    def __repr__(self) -> str:
        return "SquareConnector(location_id=%r, base_url=%r)" % (self.location_id, self._base)

    @property
    def external_account(self) -> str:
        return self.location_id

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
        if response.status_code == 200:
            return None
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        err = _first_error(body) or {}
        category, code = str(err.get("category") or ""), str(err.get("code") or "")
        text = ("%s %s" % (category or "HTTP %d" % response.status_code, code)).strip()[:80]
        http = response.status_code
        if code in _EXPIRED_CODES or (not code and http == 401):
            raise AuthExpired("the access token was rejected (%s)" % text)
        if code in _REVOKED_CODES or (not code and http == 403) or (category == "AUTHENTICATION_ERROR" and http == 403):
            raise AuthRevoked("no access to location %s (%s)" % (self.location_id, text))
        if code == "RATE_LIMITED" or http == 429 or http >= 500 or category == "API_ERROR":
            return Retry(retry_after(response), text)
        raise UpstreamBroken("Square refused the request (%s)" % text)

    async def _get(self, path: str, params: Mapping[str, str]) -> tuple[dict, int, int]:
        url = self._base + path

        async def send() -> httpx.Response:
            headers = {"Authorization": "Bearer " + self._bearer(), "Square-Version": API_VERSION, "Content-Type": "application/json"}
            return await self._client.get(url, params=params, headers=headers)

        response, calls, waits = await self._retrier.run(send, self._classify)
        try:
            body = response.json()
        except ValueError:
            raise UpstreamBroken("Square answered 200 with a body that is not JSON") from None
        if not isinstance(body, dict):
            raise UpstreamBroken("Square answered 200 with %s, not an object" % type(body).__name__)
        if body.get("errors"):
            # Square documents an errors array on a failure; a 200 that carries one is a failure the status did not show.
            raise UpstreamBroken("Square answered 200 with errors in the body")
        return body, calls, waits

    # ------------------------------------------------------------------ the location

    async def _locate(self) -> tuple[int, int]:
        """Read the location's time zone, currency and status. Returns (api calls, waits)."""
        body, calls, waits = await self._get("/v2/locations", {})
        locations = body.get("locations")
        found = next((loc for loc in locations if isinstance(loc, dict) and loc.get("id") == self.location_id), None) \
            if isinstance(locations, list) else None
        if found is None:
            raise AuthRevoked("location %s is not among the locations this token can read" % self.location_id)
        if found.get("status") not in (None, "ACTIVE"):
            raise UpstreamBroken("location %s is not active (status %s)" % (self.location_id, found.get("status")))
        currency = found.get("currency")
        if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
            raise UpstreamBroken("location %s has no currency" % self.location_id)
        self.currency = currency
        zone = found.get("timezone")
        if isinstance(zone, str) and zone and self.time_zone is None:
            self.time_zone = zone
        return calls, waits

    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.time_zone or "")
        except (ZoneInfoNotFoundError, ValueError):
            raise UpstreamBroken("the location has no usable IANA time zone (%r)" % (self.time_zone,)) from None

    async def verify(self) -> str:
        await self._locate()
        return "Square location %s (%s, %s)" % (self.location_id, self.currency, self.time_zone or "no time zone")

    # ------------------------------------------------------------------ fetch

    @staticmethod
    def _boundary(day: date, zone: ZoneInfo) -> datetime:
        return datetime.combine(day, time(0, 0), tzinfo=zone).astimezone(timezone.utc)

    @staticmethod
    def _stamp(text: Any, what: str) -> datetime:
        if not isinstance(text, str):
            raise UpstreamBroken("%s is %s, not an RFC 3339 timestamp" % (what, type(text).__name__))
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise UpstreamBroken("%s is not an RFC 3339 timestamp" % what) from None
        if stamp.tzinfo is None:
            raise UpstreamBroken("%s carries no offset" % what)
        return stamp.astimezone(timezone.utc)

    async def fetch(self, *, window_start: date, window_end: date, cursor: str | None = None) -> AsyncIterator[FetchResult]:
        """Three canonical rows per local day for [window_start, window_end), written once the whole window is read.

        There is no resume cursor (see the module note), so a cursor passed in is refused rather than ignored.
        """
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        if cursor:
            raise ValueError("the Square connector issues no cursor, so none can be resumed")
        calls = waits = 0
        if self.currency is None or self.time_zone is None:
            c, w = await self._locate()
            calls, waits = calls + c, waits + w
        zone = self._zone()
        begin, end = self._boundary(window_start, zone), self._boundary(window_end, zone)
        counts: dict[date, list[int]] = {}
        ids: set[str] = set()
        page_cursor: str | None = None
        cursors: set[str] = set()
        for _ in range(MAX_PAGES):
            params = {"begin_time": begin.isoformat().replace("+00:00", "Z"), "end_time": end.isoformat().replace("+00:00", "Z"),
                      "location_id": self.location_id, "sort_order": "ASC", "limit": str(PAGE_LIMIT)}
            if page_cursor:
                params["cursor"] = page_cursor
            body, c, w = await self._get("/v2/payments", params)
            calls, waits = calls + c, waits + w
            self._add(body.get("payments", []), begin, end, zone, counts, ids)
            following = body.get("cursor")
            if following in (None, ""):
                break
            if not isinstance(following, str):
                raise UpstreamBroken("cursor is %s, not a string" % type(following).__name__)
            if following == page_cursor or following in cursors:
                raise UpstreamBroken("paging does not advance: the cursor came back")
            cursors.add(following)
            page_cursor = following
            # Nothing is written until the window is whole; an empty page keeps the lease renewed and the call count honest.
            yield FetchResult(points=[], cursor=None, api_calls=calls, throttle_waits=waits)
            calls = waits = 0
        else:
            raise UpstreamBroken("more than %d pages in one window" % MAX_PAGES)
        assert self.currency is not None
        raw = {"source": "payments", "status": "COMPLETED", "location_id": self.location_id, "time_zone": self.time_zone}
        points: list[CanonicalPoint] = []
        day = window_start
        while day < window_end:
            n, total, refunded = counts.get(day, [0, 0, 0])
            points += [CanonicalPoint("payments", day, n, None, raw),
                       CanonicalPoint("payments_total", day, total, self.currency, raw),
                       CanonicalPoint("payments_refunded", day, refunded, self.currency, raw)]
            day += timedelta(days=1)
        yield FetchResult(points=points, cursor=None, api_calls=calls, throttle_waits=waits)

    def _add(self, payments: Any, begin: datetime, end: datetime, zone: ZoneInfo, counts: dict[date, list[int]], ids: set[str]) -> None:
        if not isinstance(payments, list):
            raise UpstreamBroken("payments is not a list")
        for payment in payments:
            if not isinstance(payment, dict):
                raise UpstreamBroken("a payment is %s, not an object" % type(payment).__name__)
            ident = payment.get("id")
            if not isinstance(ident, str) or not ident:
                raise UpstreamBroken("a payment has no id")
            stamp = self._stamp(payment.get("created_at"), "created_at of a payment")
            if stamp < begin or stamp > end:
                raise UpstreamBroken("a payment is outside the requested range")
            # A payment at exactly `end` (the next day's first instant, if end_time is inclusive) has the next local date, which is not in the window.
            if ident in ids:
                continue                                    # a payment repeated across pages is one payment
            ids.add(ident)
            if payment.get("status") != "COMPLETED":
                continue
            total, currency = _money(payment.get("total_money"), "total_money")
            refunded = 0
            if payment.get("refunded_money") is not None:
                refunded, refund_currency = _money(payment["refunded_money"], "refunded_money")
                if refund_currency != currency:
                    raise UpstreamBroken("a payment refunds in another currency than it was taken in")
            if self.currency is not None and currency != self.currency:
                raise UpstreamBroken("a payment is in %s but the location's currency is %s" % (currency, self.currency))
            day = stamp.astimezone(zone).date()
            row = counts.setdefault(day, [0, 0, 0])
            row[0] += 1
            row[1] += total
            row[2] += refunded
