"""Shopmonkey connector: work orders opened and invoiced per day, for a repair shop.

The ninth connector behind the contract in `base.py`, for the shop management software an RV repair shop runs on. It reads the v3 orders
list and writes two daily keys: `work_orders` (orders created that day: the jobs booked) and `work_order_revenue` (the `totalCostCents` of
orders invoiced that day). It has been run only against the stand-in server in `shopmonkey_fake.py`, never against a live shop: no account is
held. Every fact about the calls, with the page it came from and what is not confirmed, is in docs/AD_AND_BUSINESS_SOURCES.md.

WHAT IT CALLS. `GET {base}/v3/order?limit=..&skip=..` with `Authorization: Bearer`, the envelope `{"success", "data": [...], "meta": {"hasMore",
"total"}}`, and `GET /v3/auth/api_key/status` for `verify()`. `hasMore` ends the paging.

THE WINDOW IS CUT HERE, NOT BY THE SERVER. The List Orders page names a `where` filter and an `orderby` and gives no syntax for either, so
none is invented: the connector pages through the shop's orders and keeps the ones whose dates fall in the window. That reads every order on each
sync, so it is bounded (`MAX_PAGES` pages of `page_size`) and a shop beyond the bound is refused rather than silently cut. The first thing to
replace if a shop is large is this scan, once a real shop shows the filter syntax.

TWO DATES, TWO KEYS. A job is booked on the day its order is created (`createdDate`) and earns on the day it is invoiced (`invoicedDate`, with
`invoiced` true). Each key is attributed to its own date, so a day's `work_orders` and `work_order_revenue` are not the same orders; the combined
ad spend view uses only `work_orders`. A deleted order is not counted. The cents are in the shop's currency, which the pages do not name: the
caller states it (`currency`), and a zero decimal currency is refused because the field is named cents.

DAYS ARE SHOP DAYS. The dates are timestamps; they are cut at midnight in `time_zone` (default UTC; a US shop sets its own IANA zone).

THE WINDOW IS ACCUMULATED, THEN WRITTEN, like Square and QuickBooks: it yields once at the end and offers no resume cursor, so it cannot write half a day.
A day with no order is written as zeros, because the shop's whole list was read.

THE TOKEN. An already issued API key, held in a `Secret`, sent only in the `Authorization` header. No URL, query or response body is put in an error message.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.crypto import Secret

from .base import AuthExpired, AuthRevoked, CanonicalPoint, Connector, FetchResult, UpstreamBroken
from .common import BIGINT_MAX, Retrier, Retry, minor_exponent, retry_after
from .ga4 import settled_window_end  # noqa: F401 - the same window rule, re-exported for the sync script

BASE_URL = "https://api.shopmonkey.cloud"
API_PATH = "/v3"
SUPPORTED = frozenset({"work_orders", "work_order_revenue"})
DEFAULT_PAGE_SIZE = 100
MAX_PAGES = 500

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_LOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ShopmonkeyConnector(Connector):
    platform = "shopmonkey"
    supported_metrics = SUPPORTED

    def __init__(
        self,
        *,
        currency: str,
        access_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        location_id: str | None = None,
        time_zone: str = "UTC",
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        retrier: Retrier | None = None,
        **retry_options: Any,
    ):
        if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
            # The pages never name the shop's currency and a default would be a guess written beside every figure.
            raise ValueError("currency must be the shop's ISO 4217 code, such as USD")
        if minor_exponent(currency) != 2:
            raise ValueError("work_order_revenue is read from cents, so only two decimal currencies are supported")
        if (access_token is None) == (token_provider is None):
            raise ValueError("give exactly one of access_token and token_provider")
        if location_id is not None and not _LOCATION_RE.match(str(location_id)):
            raise ValueError("location_id must be a Shopmonkey location id or None")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        try:
            self._zone = ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("time_zone must be an IANA zone such as America/New_York, got %r" % (time_zone,)) from None
        self.currency = currency
        self.time_zone = time_zone
        self.location_id = location_id
        self._token = Secret(access_token) if access_token is not None else None
        self._token_provider = token_provider
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._page_size = page_size
        self._retrier = retrier or Retrier(**retry_options)
        self.degraded_metrics: set[str] = set()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "ShopmonkeyConnector":
        """Build from SHOPMONKEY_API_KEY and SHOPMONKEY_CURRENCY (SHOPMONKEY_LOCATION_ID, SHOPMONKEY_TIME_ZONE, SHOPMONKEY_API_BASE_URL optional)."""
        env = os.environ if env is None else env
        missing = [n for n in ("SHOPMONKEY_API_KEY", "SHOPMONKEY_CURRENCY") if not env.get(n)]
        if missing:
            raise ValueError("set %s in the environment" % " and ".join(missing))
        for var, key in (("SHOPMONKEY_API_BASE_URL", "base_url"), ("SHOPMONKEY_LOCATION_ID", "location_id"), ("SHOPMONKEY_TIME_ZONE", "time_zone")):
            if env.get(var):
                kwargs.setdefault(key, env[var])
        return cls(currency=env["SHOPMONKEY_CURRENCY"], access_token=env["SHOPMONKEY_API_KEY"], **kwargs)

    def __repr__(self) -> str:
        return "ShopmonkeyConnector(location_id=%r, base_url=%r)" % (self.location_id, self._base)

    @property
    def external_account(self) -> str:
        """A key is per shop, so a connection names the shop's location when one is given, else the shop as a whole."""
        return self.location_id or "all-locations"

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
        http = response.status_code
        if http == 200:
            return None
        text = "HTTP %d" % http
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("code"), str):
            text += " " + body["code"][:30]
        if http == 401:
            raise AuthExpired("the API key was rejected (%s)" % text)
        if http == 403:
            raise AuthRevoked("the API key may not read this (%s)" % text)
        if http == 429 or http >= 500:
            return Retry(retry_after(response), text)
        raise UpstreamBroken("Shopmonkey refused the request (%s)" % text)

    async def _get(self, path: str, params: Mapping[str, str]) -> tuple[dict, int, int]:
        url = self._base + API_PATH + path

        async def send() -> httpx.Response:
            return await self._client.get(url, params=params, headers={"Authorization": "Bearer " + self._bearer()})

        response, calls, waits = await self._retrier.run(send, self._classify)
        try:
            body = response.json()
        except ValueError:
            raise UpstreamBroken("Shopmonkey answered 200 with a body that is not JSON") from None
        if not isinstance(body, dict):
            raise UpstreamBroken("Shopmonkey answered 200 with %s, not an object" % type(body).__name__)
        if body.get("success") is not True:
            # The pages: "if not successful, will be false and message will contain a human readable error message".
            raise UpstreamBroken("Shopmonkey answered 200 with success false")
        return body, calls, waits

    # ------------------------------------------------------------------ public

    async def verify(self) -> str:
        """The API key's status, the cheapest authenticated call the Quickstart gives."""
        await self._get("/auth/api_key/status", {})
        return "Shopmonkey API key (%s%s)" % (self.currency, ", location " + self.location_id if self.location_id else "")

    def _local_day(self, value: Any, what: str) -> date:
        if not isinstance(value, str):
            raise UpstreamBroken("%s is %s, not a timestamp" % (what, type(value).__name__))
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise UpstreamBroken("%s is not an ISO 8601 timestamp" % what) from None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(self._zone).date()

    def _add(self, orders: Any, start: date, end: date, ids: set[str], opened: dict[date, int], earned: dict[date, int]) -> None:
        if not isinstance(orders, list):
            raise UpstreamBroken("data is not a list")
        for order in orders:
            if not isinstance(order, dict):
                raise UpstreamBroken("an order is %s, not an object" % type(order).__name__)
            ident = order.get("id")
            if not isinstance(ident, str) or not ident:
                raise UpstreamBroken("an order has no id")
            if ident in ids:
                continue                                    # an order repeated across pages is one order
            ids.add(ident)
            if order.get("deleted") is True:
                continue
            if self.location_id is not None and order.get("locationId") != self.location_id:
                continue
            created = self._local_day(order.get("createdDate"), "createdDate of order %s" % ident)
            if start <= created < end:
                opened[created] = opened.get(created, 0) + 1
            if order.get("invoiced") is True and order.get("invoicedDate") is not None:
                invoiced = self._local_day(order["invoicedDate"], "invoicedDate of order %s" % ident)
                if start <= invoiced < end:
                    cents = order.get("totalCostCents")
                    if isinstance(cents, bool) or not isinstance(cents, int) or cents < 0 or cents > BIGINT_MAX:
                        raise UpstreamBroken("totalCostCents of invoiced order %s is %r, not a whole number of cents" % (ident, cents))
                    earned[invoiced] = earned.get(invoiced, 0) + cents

    async def fetch(self, *, window_start: date, window_end: date, cursor: str | None = None) -> AsyncIterator[FetchResult]:
        """Two canonical rows per shop day for [window_start, window_end), written once every order has been read.

        There is no resume cursor (see the module note), so a cursor passed in is refused rather than ignored.
        """
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        if cursor:
            raise ValueError("the Shopmonkey connector issues no cursor, so none can be resumed")
        ids: set[str] = set()
        opened: dict[date, int] = {}
        earned: dict[date, int] = {}
        skip, calls, waits = 0, 0, 0
        for _ in range(MAX_PAGES):
            body, c, w = await self._get("/order", {"limit": str(self._page_size), "skip": str(skip)})
            calls, waits = calls + c, waits + w
            orders = body.get("data")
            self._add(orders, window_start, window_end, ids, opened, earned)
            meta = body.get("meta")
            more = meta.get("hasMore") if isinstance(meta, dict) else None
            if not isinstance(more, bool):
                raise UpstreamBroken("meta.hasMore is %r, not a boolean" % (more,))
            if not more:
                break
            if not orders:
                # hasMore with an empty page would loop for ever, spending the rate limit.
                raise UpstreamBroken("paging does not advance: an empty page says there are more")
            skip += len(orders)
            yield FetchResult(points=[], cursor=None, api_calls=calls, throttle_waits=waits)
            calls = waits = 0
        else:
            raise UpstreamBroken("more than %d pages of orders: the scan is bounded" % MAX_PAGES)
        raw = {"source": "order", "time_zone": self.time_zone, "location_id": self.location_id}
        points: list[CanonicalPoint] = []
        day = window_start
        while day < window_end:
            points += [CanonicalPoint("work_orders", day, opened.get(day, 0), None, raw),
                       CanonicalPoint("work_order_revenue", day, earned.get(day, 0), self.currency, raw)]
            day += timedelta(days=1)
        yield FetchResult(points=points, cursor=None, api_calls=calls, throttle_waits=waits)
