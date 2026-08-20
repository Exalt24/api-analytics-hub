"""Shopify Admin GraphQL connector.

WHY THE PACING LOGIC LOOKS LIKE THIS, because it is the whole point of the file.

Shopify's Admin GraphQL API is rate limited by CALCULATED QUERY COST against a
leaky bucket, not by requests per second. On a standard plan the bucket holds
1000 points and refills at 50 points per second, and a single query may not cost
more than 1000 points regardless of plan. Crucially, EVERY response carries
`extensions.cost.throttleStatus` with `maximumAvailable`, `currentlyAvailable`
and `restoreRate`.

So the correct pace is READ FROM THE RESPONSE, never assumed. I learned that the
expensive way on a different platform: I measured its documented limit once, got
a number twelve times higher than the docs, built for it, and watched the same
header read the documented value later the same day. Both readings were correct;
the limit varied by load, exactly as its docs said. A single reading of a varying
value is not a measurement.

Two Shopify specifics worth encoding rather than discovering in production:

  * The bucket is checked against the REQUESTED cost before execution, then
    refunded the difference between requested and actual afterwards. So a query
    that fetches fewer nodes than it asked for costs less than it reserved, and
    pacing on requested cost alone over-throttles.
  * `currentlyAvailable` can drop without you spending it, because the bucket is
    per app per shop and another process may share it. Treating it as
    authoritative rather than tracking spend locally is the only safe read.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Mapping

import httpx

from .base import (
    AuthExpired,
    AuthRevoked,
    ProtectedDataDenied,
    CanonicalPoint,
    Connector,
    FetchResult,
    RateLimited,
    UpstreamBroken,
)

# Pinned deliberately. An unpinned "latest" means Shopify can change a field's
# shape under a running sync, and the failure looks like bad data rather than a
# version bump.
API_VERSION = "2026-07"

# Keep a third of the bucket in reserve. Not superstition: the bucket is shared
# per app per shop, so another process can spend concurrently, and a query is
# rejected on REQUESTED cost before execution. Running the bucket to zero means
# the next legitimate query is refused rather than merely slowed.
BUCKET_RESERVE_RATIO = 0.33

ORDERS_QUERY = """
query Orders($first: Int!, $after: String, $q: String!) {
  orders(first: $first, after: $after, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      createdAt
      processedAt
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      totalRefundedSet { shopMoney { amount currencyCode } }
      customer { id }
    }
  }
}
"""

#: The same query with the protected field removed. Kept as a separate literal
#: rather than assembled from fragments because a query string that is built at
#: runtime is a query nobody has ever read in full.
ORDERS_QUERY_NO_CUSTOMER = """
query Orders($first: Int!, $after: String, $q: String!) {
  orders(first: $first, after: $after, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      createdAt
      processedAt
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      totalRefundedSet { shopMoney { amount currencyCode } }
    }
  }
}
"""

SHOP_QUERY = "query { shop { name myshopifyDomain currencyCode } }"


def _money_to_minor(amount: str | float | int, currency: str) -> int:
    """Shopify returns money as a decimal STRING. Parse it as Decimal and scale.

    float(amount) is the bug this avoids: "19.99" becomes 19.989999999999998 and
    a day's worth of orders drifts by cents. Decimal keeps it exact, and the
    result is an integer in minor units from here on.
    """
    # Zero-decimal currencies exist (JPY, KRW) and scaling them by 100 invents
    # money that is not there.
    exponent = 0 if currency.upper() in _ZERO_DECIMAL else 2
    return int((Decimal(str(amount)) * (10**exponent)).to_integral_value())


_ZERO_DECIMAL = frozenset(
    {"JPY", "KRW", "VND", "CLP", "ISK", "XAF", "XOF", "BIF", "DJF", "GNF", "KMF",
     "MGA", "PYG", "RWF", "UGX", "VUV", "XPF"}
)


class ShopifyConnector(Connector):
    platform = "shopify"
    supported_metrics = frozenset(
        {"orders", "gross_sales", "refunds", "net_sales", "avg_order_value", "customers"}
    )

    def __init__(
        self,
        *,
        shop_domain: str,
        access_token: str,
        client: httpx.AsyncClient | None = None,
        page_size: int = 100,
    ):
        # myshopify.com domain, not the storefront domain. Getting this wrong
        # yields a 404 that reads like a malformed query.
        self.shop_domain = shop_domain.replace("https://", "").strip("/")
        self._token = access_token
        self._page_size = page_size
        self._client = client or httpx.AsyncClient(timeout=30.0)
        #: Last throttle status Shopify reported. None until the first response,
        #: which is why the first call is allowed through unpaced.
        self._throttle: dict[str, float] | None = None
        #: Latched off the first time Shopify refuses the protected customer
        #: field, so the retry happens once rather than on every page.
        self._customer_field_allowed = True
        #: Metrics this connector had to stop reporting mid-run. A caller writing
        #: snapshots must know the difference between "zero" and "not available".
        self.degraded_metrics: set[str] = set()

    @property
    def available_metrics(self) -> frozenset[str]:
        """What this connector can actually deliver right now.

        supported_metrics is a static claim about the platform; this is a live
        statement about this shop and this app's approvals, and the two diverge
        the moment a field is withheld.
        """
        return frozenset(self.supported_metrics) - self.degraded_metrics

    # ------------------------------------------------------------------ transport

    @property
    def _endpoint(self) -> str:
        return f"https://{self.shop_domain}/admin/api/{API_VERSION}/graphql.json"

    async def _wait_for_budget(self, requested_cost: float) -> int:
        """Sleep only as long as the bucket actually needs to refill.

        Returns how many times we had to wait, which the sync run records: a job
        that is slow because it is being throttled looks identical to a job that
        is slow because it is broken, unless you count.
        """
        waits = 0
        while self._throttle:
            available = self._throttle["currentlyAvailable"]
            maximum = self._throttle["maximumAvailable"]
            restore = max(self._throttle["restoreRate"], 1.0)
            floor = maximum * BUCKET_RESERVE_RATIO

            if available - requested_cost >= floor:
                return waits

            deficit = (floor + requested_cost) - available
            # Never sleep longer than refilling the whole bucket would take; a
            # bigger number means the arithmetic is wrong, not that we should nap.
            delay = min(deficit / restore, maximum / restore)
            waits += 1
            await asyncio.sleep(delay)
            # Assume the refill happened rather than re-querying, then let the
            # next real response correct us. Polling to learn the budget spends
            # the budget.
            self._throttle["currentlyAvailable"] = min(
                maximum, available + delay * restore
            )
        return waits

    async def _graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> tuple[dict, int]:
        # Estimate before the first response teaches us the real cost. Page size
        # is the dominant term in Shopify's static analysis.
        estimated = float(self._page_size)
        waits = await self._wait_for_budget(estimated)

        try:
            r = await self._client.post(
                self._endpoint,
                headers={
                    "X-Shopify-Access-Token": self._token,
                    "Content-Type": "application/json",
                },
                content=json.dumps({"query": query, "variables": dict(variables or {})}),
            )
        except httpx.HTTPError as exc:
            raise RateLimited(detail=f"transport failure: {exc}") from exc

        # 401 vs 402 vs 403 mean genuinely different things here and conflating
        # them makes a billing problem look like a bug in our auth code.
        if r.status_code == 401:
            raise AuthExpired("access token rejected")
        if r.status_code == 403:
            raise AuthRevoked("app lacks scope or was uninstalled")
        if r.status_code == 402:
            raise AuthRevoked("shop is frozen or unpaid, no data will be served")
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            raise RateLimited(
                retry_after_seconds=float(retry_after) if retry_after else None,
                detail="HTTP 429 from Shopify",
            )
        if r.status_code >= 500:
            raise RateLimited(detail=f"Shopify {r.status_code}")
        if r.status_code != 200:
            raise UpstreamBroken(f"unexpected status {r.status_code}: {r.text[:200]}")

        payload = r.json()

        # Record what Shopify says about the bucket BEFORE handling errors, so a
        # THROTTLED response still teaches us the real budget.
        cost = (payload.get("extensions") or {}).get("cost") or {}
        throttle = cost.get("throttleStatus")
        if throttle:
            self._throttle = {
                "maximumAvailable": float(throttle["maximumAvailable"]),
                "currentlyAvailable": float(throttle["currentlyAvailable"]),
                "restoreRate": float(throttle["restoreRate"]),
            }
            # The actual cost is authoritative and is refunded against the
            # requested cost, so pacing on the request estimate over-throttles.
            self._page_size_cost = cost.get("actualQueryCost")

        # GraphQL reports throttling as a 200 with an error body, which is the
        # single most common way a Shopify integration silently returns nothing.
        errors = payload.get("errors") or []
        for err in errors:
            code = ((err.get("extensions") or {}).get("code") or "").upper()
            if code == "THROTTLED":
                raise RateLimited(detail="GraphQL THROTTLED")
            if code in {"ACCESS_DENIED", "UNAUTHORIZED"}:
                message = str(err.get("message"))
                doc = str((err.get("extensions") or {}).get("documentation") or "")
                # Shopify labels both failures ACCESS_DENIED. The protected-data
                # one is identifiable by the documentation link it carries, which
                # is more reliable than matching prose that can be reworded.
                if "protected-customer-data" in doc or "protected customer data" in message.lower():
                    raise ProtectedDataDenied(message)
                raise AuthRevoked(message)
        if errors and not payload.get("data"):
            raise UpstreamBroken(f"GraphQL errors: {errors[:2]}")

        data = payload.get("data")
        if data is None:
            raise UpstreamBroken("response carried no data block")
        return data, waits

    # ------------------------------------------------------------------ public

    async def verify(self) -> str:
        data, _ = await self._graphql(SHOP_QUERY)
        shop = data.get("shop") or {}
        name = shop.get("name")
        if not name:
            raise UpstreamBroken("shop query returned no name")
        return f"{name} ({shop.get('myshopifyDomain')})"

    async def fetch(
        self,
        *,
        window_start: date,
        window_end: date,
        cursor: str | None = None,
    ) -> AsyncIterator[FetchResult]:
        """Aggregate orders into one canonical row per day.

        Aggregation happens HERE rather than in SQL over raw orders because the
        canonical store is daily facts, and keeping per-order rows for every
        platform would mean a different table shape per vendor. The raw payload
        is still attached so a mapping mistake is recoverable.
        """
        # Shopify's search syntax is inclusive on both ends of created_at when
        # given dates, and the window is half-open everywhere else in this
        # system, so the end is decremented to keep one meaning of "window".
        # Filter on the SAME field we bucket on. Filtering on created_at while
        # bucketing on processed_at silently drops backdated orders: they fall
        # outside the created_at window, so the day they belong to reads as zero
        # rather than as missing.
        q = (
            f"processed_at:>={window_start.isoformat()} "
            f"processed_at:<={(window_end - timedelta(days=1)).isoformat()}"
        )

        daily: dict[date, dict[str, Any]] = {}
        calls = 0
        waits_total = 0
        after = cursor

        while True:
            try:
                data, waits = await self._graphql(
                    ORDERS_QUERY if self._customer_field_allowed else ORDERS_QUERY_NO_CUSTOMER,
                    {"first": self._page_size, "after": after, "q": q},
                )
            except ProtectedDataDenied:
                if not self._customer_field_allowed:
                    # Already stripped to the minimum and still refused, so the
                    # Order object itself is withheld and there is nothing to
                    # degrade to.
                    raise
                # Retry once without the protected field. Latching the flag means
                # one wasted call per connector lifetime, not one per page.
                self._customer_field_allowed = False
                self.degraded_metrics.add("customers")
                continue
            calls += 1
            waits_total += waits

            orders = (data.get("orders") or {})
            nodes = orders.get("nodes") or []
            page = orders.get("pageInfo") or {}

            for node in nodes:
                # processedAt is what the platform attributes the sale to, and
                # is the only one of the two that an import or a backfill can
                # set. createdAt is the fallback, not the default, and a missing
                # both is a hard error rather than an implicit today.
                created = node.get("processedAt") or node.get("createdAt")
                if not created:
                    raise UpstreamBroken("order with neither processedAt nor createdAt")
                # Shopify returns ISO-8601 with an offset. Convert to UTC before
                # taking the date, otherwise a shop in +13 books orders on the
                # wrong day for a third of its trading hours.
                day = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(
                    timezone.utc
                ).date()

                bucket = daily.setdefault(
                    day,
                    {"orders": 0, "gross": 0, "refunds": 0, "currency": None, "customers": set()},
                )

                gross_set = ((node.get("currentTotalPriceSet") or {}).get("shopMoney") or {})
                currency = gross_set.get("currencyCode")
                if currency:
                    if bucket["currency"] and bucket["currency"] != currency:
                        # Multi-currency shops exist; mixing them into one total
                        # is silently wrong, so refuse rather than average.
                        raise UpstreamBroken(
                            f"two currencies on {day}: {bucket['currency']} and {currency}"
                        )
                    bucket["currency"] = currency
                    bucket["gross"] += _money_to_minor(gross_set.get("amount", "0"), currency)

                refund_set = ((node.get("totalRefundedSet") or {}).get("shopMoney") or {})
                if refund_set.get("amount") and currency:
                    bucket["refunds"] += _money_to_minor(refund_set["amount"], currency)

                bucket["orders"] += 1
                cust = (node.get("customer") or {}).get("id")
                if cust:
                    bucket["customers"].add(cust)

            after = page.get("endCursor")
            if not page.get("hasNextPage"):
                break
            # Yield partial progress so a later failure resumes here rather than
            # re-fetching the whole window.
            yield FetchResult(points=[], cursor=after, api_calls=calls, throttle_waits=waits_total)
            calls = 0
            waits_total = 0

        points: list[CanonicalPoint] = []
        for day, b in sorted(daily.items()):
            cur = b["currency"] or "USD"
            net = b["gross"] - b["refunds"]
            points.append(CanonicalPoint("orders", day, b["orders"], None, {"source": "orders"}))
            points.append(CanonicalPoint("gross_sales", day, b["gross"], cur))
            points.append(CanonicalPoint("refunds", day, b["refunds"], cur))
            points.append(CanonicalPoint("net_sales", day, net, cur))
            if b["orders"]:
                # Integer division on purpose: an average of minor units is still
                # minor units, and carrying a fraction of a cent into the store
                # would break the integer-money invariant for a rounding nicety.
                points.append(
                    CanonicalPoint("avg_order_value", day, b["gross"] // b["orders"], cur)
                )
            if self._customer_field_allowed:
                points.append(
                    CanonicalPoint("customers", day, len(b["customers"]), None,
                                   {"note": "distinct customers on this day, not lifetime"})
                )
            # else: emit nothing. A zero here would be indistinguishable from a
            # day with no buyers, and writing that into a dated snapshot makes a
            # permissions problem look like a business result.

        yield FetchResult(
            points=points, cursor=None, api_calls=calls, throttle_waits=waits_total
        )
