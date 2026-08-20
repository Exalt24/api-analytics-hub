"""Storefront API client, which is a genuinely different API from the Admin one.

WHY A SEPARATE MODULE RATHER THAN A FLAG ON THE ADMIN CLIENT. They share a query
language and almost nothing else, and treating them as one thing is how a token
ends up in the wrong header:

  * DIFFERENT ENDPOINT: /api/<version>/graphql.json, not /admin/api/...
  * DIFFERENT HEADER: X-Shopify-Storefront-Access-Token, not
    X-Shopify-Access-Token. Sending an Admin token to the Storefront endpoint
    returns a generic 401 that says nothing about which header was wrong.
  * DIFFERENT TRUST MODEL, and this is the part that matters. A Storefront token is
    PUBLIC by design: it ships in browser JavaScript and anyone can read it. So it
    exposes only what a shopper may see, and it can create carts and checkouts. An
    Admin token in the same position would be a catastrophe.
  * DIFFERENT RATE LIMITING: the Storefront API is capped per IP address rather
    than by the Admin API's calculated-cost leaky bucket, so the Admin connector's
    pacing logic does not transfer at all. Pacing a public endpoint by a budget it
    does not have would be pure superstition.

WHAT IT IS FOR HERE. Reading the catalogue the way a headless front end would, so
the same store can be served by Liquid and by a Next.js front end without the
analytics side of the system caring which.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

API_VERSION = "2026-07"


class StorefrontError(RuntimeError):
    """The Storefront API refused or returned something unusable."""


@dataclass(frozen=True)
class StorefrontProduct:
    handle: str
    title: str
    price: str
    currency: str
    available: bool
    #: Specs read through the PUBLIC surface. A metafield is only visible to the
    #: Storefront API once it has storefront access granted, which is a per-
    #: definition setting and NOT implied by the metafield existing. A headless
    #: front end silently seeing null here is the most common metafield surprise.
    specs: dict[str, str]


PRODUCTS_QUERY = """
query Products($first: Int!, $identifiers: [HasMetafieldsIdentifier!]!) {
  products(first: $first) {
    nodes {
      handle
      title
      availableForSale
      priceRange { minVariantPrice { amount currencyCode } }
      metafields(identifiers: $identifiers) { key value }
    }
  }
}
"""

CART_CREATE = """
mutation CartCreate($lines: [CartLineInput!]!) {
  cartCreate(input: { lines: $lines }) {
    cart {
      id
      checkoutUrl
      totalQuantity
      cost { totalAmount { amount currencyCode } }
    }
    userErrors { field message }
  }
}
"""


class StorefrontClient:
    def __init__(
        self,
        shop_domain: str,
        public_token: str,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        if not public_token:
            raise ValueError("a storefront access token is required")
        self.shop_domain = shop_domain.replace("https://", "").strip("/")
        self._token = public_token
        self._client = client or httpx.AsyncClient(timeout=30.0)

    @property
    def _endpoint(self) -> str:
        return "https://%s/api/%s/graphql.json" % (self.shop_domain, API_VERSION)

    async def _gql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict:
        r = await self._client.post(
            self._endpoint,
            headers={
                # NOT X-Shopify-Access-Token. Getting this wrong yields a bare 401
                # with no hint that the header name was the problem.
                "X-Shopify-Storefront-Access-Token": self._token,
                "Content-Type": "application/json",
            },
            content=json.dumps({"query": query, "variables": dict(variables or {})}),
        )
        if r.status_code == 401:
            raise StorefrontError(
                "401 from the Storefront API. Usually the Admin token sent to the "
                "storefront endpoint, or a token for a different shop."
            )
        if r.status_code == 430 or r.status_code == 429:
            # 430 is Shopify's own "shop throttled" status on this API, and it is
            # per IP rather than per cost budget.
            raise StorefrontError("throttled by the Storefront API (per-IP limit)")
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            raise StorefrontError("GraphQL errors: %s" % payload["errors"][:2])
        data = payload.get("data")
        if data is None:
            raise StorefrontError("response carried no data block")
        return data

    async def products(
        self, first: int = 10, spec_keys: tuple[str, ...] = ()
    ) -> list[StorefrontProduct]:
        identifiers = [{"namespace": "specs", "key": k} for k in spec_keys]
        data = await self._gql(
            PRODUCTS_QUERY, {"first": first, "identifiers": identifiers}
        )
        out = []
        for node in (data.get("products") or {}).get("nodes") or []:
            money = ((node.get("priceRange") or {}).get("minVariantPrice") or {})
            specs = {}
            for mf in node.get("metafields") or []:
                # A metafield with no storefront access granted comes back as a
                # NULL ENTRY in the list, not as a missing key, so iterating
                # without this check raises on the first ungranted definition.
                if mf and mf.get("value") is not None:
                    specs[mf["key"]] = mf["value"]
            out.append(StorefrontProduct(
                handle=node.get("handle") or "",
                title=node.get("title") or "",
                price=str(money.get("amount") or ""),
                currency=money.get("currencyCode") or "",
                available=bool(node.get("availableForSale")),
                specs=specs,
            ))
        return out

    async def create_cart(self, merchandise_id: str, quantity: int = 1) -> dict:
        """Create a cart and return its checkout URL.

        Proves the token can do what a headless front end needs, which is the
        thing a read-only query does not: a cart is a WRITE through a public
        token, and it is exactly why the token's scope is limited to shopper-
        visible data.
        """
        data = await self._gql(CART_CREATE, {
            "lines": [{"merchandiseId": merchandise_id, "quantity": quantity}]
        })
        res = data.get("cartCreate") or {}
        if res.get("userErrors"):
            raise StorefrontError("cartCreate userErrors: %s" % res["userErrors"])
        cart = res.get("cart") or {}
        if not cart.get("checkoutUrl"):
            raise StorefrontError("cart created without a checkout URL")

        # A CART CAN BE CREATED EMPTY AND REPORT SUCCESS. Measured against the live
        # store: cartCreate returned a cart, a real checkout URL and no userErrors,
        # with totalQuantity 0, because the variant was not purchasable and the
        # line was silently dropped. A caller checking only for errors would hand a
        # shopper an empty checkout, which is a broken funnel that looks healthy in
        # every log.
        got = cart.get("totalQuantity")
        if got != quantity:
            raise StorefrontError(
                "cart created but holds %s of the %s units requested. The variant is "
                "probably not purchasable: unavailable, untracked with no inventory, "
                "or not published to the Online Store. Checkout URL was still "
                "returned, and no userErrors were reported."
                % (got, quantity)
            )
        return cart
