"""Fake Shopify and Zoho servers for the sync adapters, as httpx transports: no socket, no network, nothing real.

They were written from the same vendor pages the adapters were (docs/vendor-contract/), so they check that a request has the
shape those pages describe (the path, the auth header, the organization_id query parameter, the mutation and its variables, the
body fields) and answer the way those pages say a vendor answers. They cannot find a misreading shared by both sides; only a
live account can. Each fake records every call and can be told to fail a number of times first:

    shop = FakeShopify(); shop.script(429, 429)        # the next two calls are throttled, the third works
    zoho = FakeZoho();    zoho.script(500, (422, {"code": 1001, "message": "Rate is invalid"}))
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .adapters import SHOPIFY_API_VERSION


def _scripted(entry: Any) -> httpx.Response:
    if isinstance(entry, int):
        return httpx.Response(entry, json={"errors": "scripted %d" % entry}, headers={"Retry-After": "0"} if entry == 429 else {})
    status, body = entry[0], entry[1]
    return httpx.Response(status, json=body, headers=entry[2] if len(entry) > 2 else {})


class _Fake:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._script: list[Any] = []

    def script(self, *entries: Any) -> None:
        """Answers for the next calls, in order: an int is a status, (status, json_body[, headers]) a full answer. Then normal service."""
        self._script.extend(entries)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        raise NotImplementedError


class FakeShopify(_Fake):
    """Holds variant prices. `shop_domain` and `token` are what a correct request must carry."""

    def __init__(self, shop_domain: str = "demo-shop.myshopify.com", token: str = "shpat_invented_for_tests") -> None:
        super().__init__()
        self.shop_domain, self.token = shop_domain, token
        self.prices: dict[str, str] = {}
        self.accepted = 0          # how many price changes this fake has accepted, so a test can say "at most once"

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.calls.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        if self._script:
            return _scripted(self._script.pop(0))
        if request.url.host != self.shop_domain or request.url.path != "/admin/api/%s/graphql.json" % SHOPIFY_API_VERSION:
            return httpx.Response(404, json={"errors": "Not Found"})
        if request.headers.get("X-Shopify-Access-Token") != self.token:
            return httpx.Response(401, json={"errors": "[API] Invalid API key or access token (unrecognized login or wrong password)"})
        if "productVariantsBulkUpdate" not in body.get("query", ""):
            return httpx.Response(200, json={"errors": [{"message": "unexpected query"}]})
        v = body.get("variables") or {}
        variants = v.get("variants") or []
        if not str(v.get("productId", "")).startswith("gid://shopify/Product/") or not variants:
            return httpx.Response(200, json={"errors": [{"message": "Variable $productId of type ID! was provided invalid value"}]})
        errors = []
        for i, var in enumerate(variants):
            try:
                if Decimal(var["price"]) < 0:
                    raise ValueError
            except Exception:
                errors.append({"field": ["variants", str(i), "price"], "message": "Price must be greater than or equal to 0"})
        if errors:
            return httpx.Response(200, json={"data": {"productVariantsBulkUpdate": {"productVariants": None, "userErrors": errors}}})
        for var in variants:
            self.prices[var["id"]] = var["price"]
        self.accepted += 1
        return httpx.Response(200, json={"data": {"productVariantsBulkUpdate": {
            "productVariants": [{"id": var["id"], "price": var["price"]} for var in variants], "userErrors": []}}})


class FakeZoho(_Fake):
    """Holds item rates and issues tokens. A token is good until `expire_tokens()` is called."""

    def __init__(self, organization_id: str = "10234695", accounts_host: str = "accounts.zoho.com.au", api_host: str = "www.zohoapis.com.au",
                 refresh_token: str = "1000.invented.refresh", client_id: str = "1000.INVENTED", client_secret: str = "invented-secret") -> None:
        super().__init__()
        self.org, self.accounts_host, self.api_host = organization_id, accounts_host, api_host
        self.refresh_token, self.client_id, self.client_secret = refresh_token, client_id, client_secret
        self.rates: dict[str, float] = {}
        self.accepted = 0
        self.tokens_issued = 0
        self._valid: str | None = None

    def expire_tokens(self) -> None:
        self._valid = None

    def _handle(self, request: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        body = json.loads(request.content) if request.content else None
        self.calls.append({"method": request.method, "url": str(request.url), "headers": dict(request.headers), "query": q, "body": body})
        if self._script:
            return _scripted(self._script.pop(0))
        if request.url.host == self.accounts_host and request.url.path == "/oauth/v2/token":
            if (q.get("grant_type"), q.get("refresh_token"), q.get("client_id"), q.get("client_secret")) != (
                    "refresh_token", self.refresh_token, self.client_id, self.client_secret):
                return httpx.Response(200, json={"error": "invalid_code"})
            self.tokens_issued += 1
            self._valid = "1000.invented.access.%d" % self.tokens_issued
            return httpx.Response(200, json={"access_token": self._valid, "api_domain": "https://www.zohoapis.com.au",
                                             "token_type": "Bearer", "expires_in": 3600})
        if request.url.host != self.api_host or not request.url.path.startswith("/inventory/v1/items/"):
            return httpx.Response(404, json={"code": 5, "message": "Invalid URL Passed"})
        if request.headers.get("Authorization") != "Zoho-oauthtoken %s" % self._valid or self._valid is None:
            return httpx.Response(401, json={"code": 57, "message": "You are not authorized to perform this operation"})
        if q.get("organization_id") != self.org:
            return httpx.Response(400, json={"code": 6, "message": "Invalid value passed for organization_id"})
        item_id = request.url.path.rsplit("/", 1)[-1]
        if request.method != "PUT" or not body or not body.get("name") or not isinstance(body.get("rate"), (int, float)):
            return httpx.Response(400, json={"code": 2, "message": "Invalid value passed for rate or name"})
        self.rates[item_id] = body["rate"]
        self.accepted += 1
        return httpx.Response(200, json={"code": 0, "message": "Item details have been updated", "item": {"item_id": item_id, "rate": body["rate"]}})
