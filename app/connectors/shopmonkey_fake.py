"""A stand-in Shopmonkey API server on 127.0.0.1, for tests.

It speaks real HTTP to the connector and implements what the saved pages describe (docs/AD_AND_BUSINESS_SOURCES.md): `GET /v3/order`
with `limit` and `skip` and the envelope `{"success", "data": [...], "meta": {"hasMore", "total"}}`; the Order fields the connector reads
(`id`, `createdDate`, `invoiced`, `invoicedDate`, `totalCostCents`, `deleted`, `locationId`, `number`); `GET /v3/auth/api_key/status`;
the error body `{"success": false, "code", "message", "documentation_url"}`; the status codes of the Errors guide (401 for a missing or
wrong key, 403 for a key without permission, 429 with `Retry-After`); and `x-request-id` on every answer.

It is a stand-in written from documentation, not a recording of Shopmonkey. Its own choices, marked UNVERIFIED in the sources page: the
largest `limit` it accepts (`max_limit`, the pages name none), the order the list comes back in (creation order), that `where` and `orderby`
are not implemented (the pages give no syntax), and the shape of the key status answer.
"""
from __future__ import annotations

from typing import Any

from .fake_http import Answer, FakeHTTP, Seen


def error_body(code: str, message: str) -> dict:
    return {"success": False, "code": code, "message": message, "documentation_url": "https://shopmonkey.dev/error/" + code}


class FakeShopmonkey(FakeHTTP):
    def __init__(self, *, token: str = "test-token", max_limit: int = 100):
        super().__init__()
        self.token = token
        self.max_limit = max_limit
        #: Keys that are valid but may not read orders (a 403).
        self.forbidden_tokens: set[str] = set()
        self.orders: list[dict[str, Any]] = []

    def add_order(self, order_id: str, created: str, *, invoiced: str | None = None, total_cents: int | None = None,
                  deleted: bool = False, location_id: str = "loc1") -> dict:
        order = {
            "id": order_id, "number": "N" + order_id, "createdDate": created, "invoiced": invoiced is not None, "invoicedDate": invoiced,
            "totalCostCents": total_cents if total_cents is not None else (0 if invoiced is None else 1), "deleted": deleted,
            "locationId": location_id, "status": "Open", "archived": False,
        }
        with self._lock:
            self.orders.append(order)
        return order

    def answer(self, seen: Seen) -> Answer:
        headers = {"x-request-id": "FAKEREQUEST"}
        if seen.method != "GET" or seen.path not in ("/v3/order", "/v3/auth/api_key/status"):
            return 404, headers, error_body("API-404", "Not found.")
        auth = seen.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or (auth != "Bearer " + self.token and auth[7:] not in self.forbidden_tokens):
            return 401, headers, error_body("API-401", "Unauthorized.")
        if auth[7:] in self.forbidden_tokens:
            return 403, headers, error_body("API-403", "Forbidden.")
        if seen.path == "/v3/auth/api_key/status":
            return 200, headers, {"success": True, "data": {"status": "active"}}
        q = seen.query
        limit, skip = q.get("limit", "100"), q.get("skip", "0")
        if not (limit.isdigit() and 1 <= int(limit) <= self.max_limit and skip.isdigit()):
            return 400, headers, error_body("API-400", "limit must be 1 to %d and skip a whole number." % self.max_limit)
        start, size = int(skip), int(limit)
        page = self.orders[start: start + size]
        return 200, headers, {"success": True, "data": page, "meta": {"hasMore": start + size < len(self.orders), "total": len(self.orders)}}
