"""Write an approved price to Shopify and to Zoho Inventory. Both adapters take an injectable httpx client and sleep, so the
tests run them against fake vendors with no network and no waiting.

WHAT IS AND IS NOT KNOWN. The requests below are written from the vendors' official pages, saved under docs/vendor-contract/
(README there lists the URLs and the lines quoted here). They have NEVER been sent to a live Shopify store or a live Zoho
Inventory organisation; the only server they have talked to is fakes.py, which was written from the same pages and so cannot
catch a misreading of them. The first run against a real account is the real test.

SHOPIFY. Admin GraphQL, API version 2026-10 (the page lists "2026-10 latest" on 2026-10-30; the hub's read connector pins
2026-07 and this write path is pinned separately). The endpoint, from the 2026-10 reference:
    "GraphQL queries are executed by sending POST HTTP requests to the endpoint:"
    "https://{store_name}.myshopify.com/admin/api/2026-10/graphql.json"
the token header: "Every GraphQL Admin API request carries an access token in the X-Shopify-Access-Token header."
and the mutation, from productVariantsBulkUpdate:
    "Requires write_products access scope."
    "Updates multiple product variants for a single product in one operation."
    "The ID of the product associated with the variants to update."   (productId: ID!)
    "An array of product variants to update."                         (variants: [ProductVariantsBulkInput!]!)
and ProductVariantsBulkInput: "Specifies the product variant to update or delete." (id: ID) and "The price of the variant." (price: Money).
A rejected change comes back as userErrors in the mutation payload (the payload section lists "userErrors"), and the reference says
"Many errors that would typically return a 4xx or 5xx status code, return an HTTP 200 errors response instead." The page lists a
"Throttled" sample but the saved copy does not show its body; the THROTTLED code this adapter retries on is the one the hub's read
connector (app/connectors/shopify.py) met against a real store. UNVERIFIED from the saved pages: the page's own example selects
productVariants { id metafields ... }, and reading `price` back from a ProductVariant is this adapter's assumption (the saved pages
do not show that type). None of it has been run live.

ZOHO INVENTORY. The item update, from the Items page ("Update an item"):
    "Update the details of an item."   "OAuth Scope : ZohoInventory.items.UPDATE"
    PUT https://www.zohoapis.com/inventory/v1/items/{item_id}?organization_id=...  with
    "--header 'Authorization: Zoho-oauthtoken 1000.41d9...'"   and a JSON body that includes
    "name  string  (Required)  Name of the Item."   and   "rate  double  Sales price of the Item."
    success: {"code": 0, "message": "Item details have been updated"}  (the page's example reads "Item details have been updated")
The organisation id: "The parameter organization_id along with the organization ID should be sent in with every API request".
The data centre: "Zoho Inventory is hosted at multiple data centers, and therefore available on different domains." and the table row
    "Australia  .com.au  https://www.zohoapis.com.au/inventory/"
so an Australian account uses https://www.zohoapis.com.au, and its token server is https://accounts.zoho.com.au (the multi-DC
OAuth page lists "Australia (AU)  https://accounts.zoho.com.au"). Rate limits: "You can make 100 requests per minute per
organization." and "An error with HTTP status code 429 will be returned if the number of requests per minute per organization exceeds 100."
The access token comes from the refresh token flow: "POST {accounts-server-url}/oauth/v2/token" with client_id, client_secret,
grant_type=refresh_token and refresh_token, answering access_token, api_domain, expires_in 3600; "Using a refresh token, your app
can create up to ten access tokens in a span of ten minutes", so the token is kept until a minute before it expires.
(That OAuth page shows the header as "Authorization: Bearer"; the Inventory page's examples show Zoho-oauthtoken, and this follows the Inventory page.)

IDEMPOTENCY. Both calls SET an absolute price, so repeating one is harmless; sync.py also records per shop that a side was applied and
never calls an applied side again. The proposal's key is passed in and logged, but neither vendor's page (as read) offers a
request-level idempotency key for these two calls, so none is sent and none is claimed.

ERRORS. 408, 429 and 5xx (and a dropped connection, and Shopify's THROTTLED) are retried with exponential backoff, honouring a
numeric Retry-After, then raise VendorTransient. Any other 4xx raises VendorRejected carrying the vendor's own message.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable, Protocol

import httpx

from .money import format_minor

SHOPIFY_API_VERSION = "2026-10"
MAX_ATTEMPTS = 4
BASE_DELAY = 0.5
MAX_DELAY = 30.0

#: region -> (accounts server, API domain), all from the saved Zoho pages (docs/vendor-contract/zoho-oauth-multi-dc and zoho-inventory-introduction)
ZOHO_REGIONS = {
    "com": ("https://accounts.zoho.com", "https://www.zohoapis.com"),
    "eu": ("https://accounts.zoho.eu", "https://www.zohoapis.eu"),
    "in": ("https://accounts.zoho.in", "https://www.zohoapis.in"),
    "com.au": ("https://accounts.zoho.com.au", "https://www.zohoapis.com.au"),
    "jp": ("https://accounts.zoho.jp", "https://www.zohoapis.jp"),
    "ca": ("https://accounts.zohocloud.ca", "https://www.zohoapis.ca"),
    "sa": ("https://accounts.zoho.sa", "https://www.zohoapis.sa"),
}


class VendorTransient(Exception):
    """Worth trying again later: a 408, 429, 5xx, a dropped connection or Shopify's THROTTLED. Raised after the retries ran out."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class VendorRejected(Exception):
    """The vendor said no and saying it again will not change that (a 4xx other than 408 and 429, or userErrors)."""

    def __init__(self, status: int | None, message: str):
        super().__init__("%s: %s" % (status if status is not None else "error", message))
        self.status = status
        self.message = message


@dataclass(frozen=True)
class PriceTarget:
    """The ids each shop uses for one catalogue product."""
    sku: str
    shopify_product_gid: str | None = None
    shopify_variant_gid: str | None = None
    zoho_item_id: str | None = None
    zoho_item_name: str | None = None


@dataclass(frozen=True)
class AdapterResult:
    vendor: str
    detail: str


class PriceAdapter(Protocol):
    name: str

    def applies_to(self, target: PriceTarget) -> bool: ...
    def describe(self, target: PriceTarget, price_minor: int, currency: str) -> dict[str, Any]: ...
    async def set_price(self, target: PriceTarget, price_minor: int, currency: str, idempotency_key: str) -> AdapterResult: ...


Sleep = Callable[[float], Awaitable[None]]


async def with_retry(op: Callable[[], Awaitable[Any]], *, sleep: Sleep = asyncio.sleep, max_attempts: int = MAX_ATTEMPTS,
                     base_delay: float = BASE_DELAY) -> Any:
    """Run `op` until it stops raising VendorTransient. The wait is Retry-After if the vendor gave one, else base * 2^n, never over MAX_DELAY."""
    for attempt in range(max_attempts):
        try:
            return await op()
        except VendorTransient as exc:
            if attempt == max_attempts - 1:
                raise VendorTransient("%s (gave up after %d attempts)" % (exc, max_attempts), exc.retry_after) from exc
            await sleep(min(exc.retry_after if exc.retry_after is not None else base_delay * (2 ** attempt), MAX_DELAY))
    raise AssertionError("unreachable")  # pragma: no cover


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    try:
        return max(0.0, float(raw)) if raw else None
    except ValueError:  # an HTTP date; fall back to the backoff
        return None


def _message(resp: httpx.Response) -> str:
    """The vendor's own words from an error body, best effort, short."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200] or "HTTP %d" % resp.status_code
    if isinstance(body, dict):
        for key in ("message", "errors", "error", "error_description"):
            if body.get(key):
                v = body[key]
                if isinstance(v, list):
                    v = "; ".join(str(x.get("message", x)) if isinstance(x, dict) else str(x) for x in v)
                return str(v)[:300]
    return resp.text[:200]


def _check_status(resp: httpx.Response) -> None:
    code = resp.status_code
    if code in (408, 429) or code >= 500:
        raise VendorTransient("HTTP %d" % code, _retry_after(resp))
    if 400 <= code < 500:
        raise VendorRejected(code, _message(resp))


# ----------------------------------------------------------------------------------------------------- Shopify

SHOPIFY_MUTATION = """mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id price }
    userErrors { field message }
  }
}"""


class ShopifyPriceAdapter:
    name = "shopify"

    def __init__(self, client: httpx.AsyncClient, shop_domain: str, access_token: str, *, api_version: str = SHOPIFY_API_VERSION,
                 sleep: Sleep = asyncio.sleep, max_attempts: int = MAX_ATTEMPTS):
        if not shop_domain.endswith(".myshopify.com"):
            raise ValueError("shop_domain must be the store's .myshopify.com domain")
        self._client, self._token, self._sleep, self._attempts = client, access_token, sleep, max_attempts
        self.endpoint = "https://%s/admin/api/%s/graphql.json" % (shop_domain, api_version)

    @classmethod
    def from_env(cls, client: httpx.AsyncClient, env: dict[str, str] | None = None) -> "ShopifyPriceAdapter":
        e = os.environ if env is None else env
        return cls(client, e["SHOPIFY_SHOP_DOMAIN"], e["SHOPIFY_ADMIN_TOKEN"])

    def applies_to(self, target: PriceTarget) -> bool:
        return bool(target.shopify_product_gid and target.shopify_variant_gid)

    def _variables(self, target: PriceTarget, price_minor: int, currency: str) -> dict[str, Any]:
        return {"productId": target.shopify_product_gid,
                "variants": [{"id": target.shopify_variant_gid, "price": format_minor(price_minor, currency)}]}

    def describe(self, target: PriceTarget, price_minor: int, currency: str) -> dict[str, Any]:
        """What a real run would send. The token is never part of it."""
        return {"vendor": self.name, "method": "POST", "url": self.endpoint, "variables": self._variables(target, price_minor, currency)}

    async def set_price(self, target: PriceTarget, price_minor: int, currency: str, idempotency_key: str) -> AdapterResult:
        variables = self._variables(target, price_minor, currency)
        sent = variables["variants"][0]["price"]

        async def once() -> AdapterResult:
            try:
                resp = await self._client.post(
                    self.endpoint, json={"query": SHOPIFY_MUTATION, "variables": variables},
                    headers={"X-Shopify-Access-Token": self._token, "Content-Type": "application/json"})
            except httpx.TransportError as exc:
                raise VendorTransient("connection failed: %s" % type(exc).__name__) from exc
            _check_status(resp)
            try:
                body = resp.json()
            except ValueError as exc:
                raise VendorRejected(resp.status_code, "response was not JSON") from exc
            errors = body.get("errors") or []
            if errors:
                if any(((e.get("extensions") or {}).get("code") or "").upper() == "THROTTLED" for e in errors if isinstance(e, dict)):
                    raise VendorTransient("GraphQL THROTTLED")
                raise VendorRejected(resp.status_code, "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors)[:300])
            payload = ((body.get("data") or {}).get("productVariantsBulkUpdate")) or {}
            user_errors = payload.get("userErrors") or []
            if user_errors:
                raise VendorRejected(resp.status_code, "; ".join("%s: %s" % (".".join(map(str, u.get("field") or [])) or "input", u.get("message")) for u in user_errors)[:300])
            echoed = payload.get("productVariants") or []
            # a success we cannot see the new price in is not a success we can record
            if not any(v.get("id") == target.shopify_variant_gid and Decimal(str(v.get("price"))) == Decimal(sent) for v in echoed):
                raise VendorRejected(resp.status_code, "response did not confirm the new price %s" % sent)
            return AdapterResult(self.name, "price set to %s" % sent)

        return await with_retry(once, sleep=self._sleep, max_attempts=self._attempts)


# ----------------------------------------------------------------------------------------------------- Zoho

class ZohoTokenSource:
    """Access tokens from a refresh token, cached until a minute before they expire (the vendor limits how many can be issued)."""

    def __init__(self, client: httpx.AsyncClient, accounts_url: str, client_id: str, client_secret: str, refresh_token: str, *,
                 clock: Callable[[], float] = time.monotonic, sleep: Sleep = asyncio.sleep, max_attempts: int = MAX_ATTEMPTS):
        self._client, self._accounts = client, accounts_url.rstrip("/")
        self._creds = {"client_id": client_id, "client_secret": client_secret, "grant_type": "refresh_token", "refresh_token": refresh_token}
        self._clock, self._sleep, self._attempts = clock, sleep, max_attempts
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self.refreshes = 0

    async def token(self, *, force: bool = False) -> str:
        async with self._lock:
            if force or self._token is None or self._clock() >= self._expires_at - 60:
                await with_retry(self._refresh, sleep=self._sleep, max_attempts=self._attempts)
            assert self._token is not None
            return self._token

    async def _refresh(self) -> None:
        try:
            resp = await self._client.post(self._accounts + "/oauth/v2/token", params=self._creds)
        except httpx.TransportError as exc:
            raise VendorTransient("token server unreachable: %s" % type(exc).__name__) from exc
        _check_status(resp)
        try:
            body = resp.json()
        except ValueError as exc:
            raise VendorRejected(resp.status_code, "token response was not JSON") from exc
        # Zoho can answer a refused refresh with HTTP 200 and {"error": "invalid_code"}; the page lists these as "Error codes"
        if body.get("error") or not body.get("access_token"):
            raise VendorRejected(resp.status_code, "token refresh refused: %s" % (body.get("error") or "no access_token in the answer"))
        self._token = body["access_token"]
        self._expires_at = self._clock() + float(body.get("expires_in", 3600))
        self.refreshes += 1


class ZohoInventoryAdapter:
    name = "zoho"

    def __init__(self, client: httpx.AsyncClient, tokens: ZohoTokenSource, organization_id: str, api_domain: str, *,
                 sleep: Sleep = asyncio.sleep, max_attempts: int = MAX_ATTEMPTS):
        self._client, self._tokens, self._org = client, tokens, organization_id
        self._api = api_domain.rstrip("/")
        self._sleep, self._attempts = sleep, max_attempts

    @classmethod
    def from_env(cls, client: httpx.AsyncClient, env: dict[str, str] | None = None) -> "ZohoInventoryAdapter":
        e = os.environ if env is None else env
        region = e["ZOHO_REGION"]
        if region not in ZOHO_REGIONS:
            raise ValueError("ZOHO_REGION must be one of %s (an Australian account is com.au)" % ", ".join(sorted(ZOHO_REGIONS)))
        accounts, api = ZOHO_REGIONS[region]
        tokens = ZohoTokenSource(client, accounts, e["ZOHO_CLIENT_ID"], e["ZOHO_CLIENT_SECRET"], e["ZOHO_REFRESH_TOKEN"])
        return cls(client, tokens, e["ZOHO_ORGANIZATION_ID"], api)

    def applies_to(self, target: PriceTarget) -> bool:
        return bool(target.zoho_item_id and target.zoho_item_name)

    def _url(self, target: PriceTarget) -> str:
        return "%s/inventory/v1/items/%s" % (self._api, target.zoho_item_id)

    def _body(self, target: PriceTarget, price_minor: int, currency: str) -> dict[str, Any]:
        # "rate  double": a JSON number, so the exact decimal text goes through float only at this edge. "name" is Required by the update.
        return {"name": target.zoho_item_name, "rate": float(Decimal(format_minor(price_minor, currency)))}

    def describe(self, target: PriceTarget, price_minor: int, currency: str) -> dict[str, Any]:
        return {"vendor": self.name, "method": "PUT", "url": self._url(target), "query": {"organization_id": self._org},
                "body": self._body(target, price_minor, currency)}

    async def set_price(self, target: PriceTarget, price_minor: int, currency: str, idempotency_key: str) -> AdapterResult:
        body = self._body(target, price_minor, currency)
        refreshed_for_401 = False

        async def once() -> AdapterResult:
            nonlocal refreshed_for_401
            token = await self._tokens.token()
            try:
                resp = await self._client.put(self._url(target), params={"organization_id": self._org}, json=body,
                                              headers={"Authorization": "Zoho-oauthtoken " + token})
            except httpx.TransportError as exc:
                raise VendorTransient("connection failed: %s" % type(exc).__name__) from exc
            if resp.status_code == 401 and not refreshed_for_401:
                # an access token lives an hour; a 401 may only mean it ran out, so ask for a new one once and go again
                refreshed_for_401 = True
                await self._tokens.token(force=True)
                return await once()
            _check_status(resp)
            try:
                payload = resp.json()
            except ValueError as exc:
                raise VendorRejected(resp.status_code, "response was not JSON") from exc
            if payload.get("code") != 0:
                raise VendorRejected(resp.status_code, "code %s: %s" % (payload.get("code"), payload.get("message")))
            return AdapterResult(self.name, "rate set to %s" % body["rate"])

        return await with_retry(once, sleep=self._sleep, max_attempts=self._attempts)
