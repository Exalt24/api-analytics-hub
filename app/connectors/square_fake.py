"""A stand-in Square API server on 127.0.0.1, for tests.

It speaks real HTTP to the connector and implements what the saved pages describe (docs/AD_AND_BUSINESS_SOURCES.md):
`GET /v2/payments` with `begin_time` (inclusive), `end_time`, `sort_order`, `cursor`, `location_id` and `limit` (the
default and maximum are 100, and a larger value is ignored and replaced by 100); the `payments` array and the `cursor` that is
absent on the last page; the Payment object's `id`, `created_at`, `status`, `total_money`, `amount_money`, `refunded_money`
and `location_id` with Money as `{"amount": int, "currency": "USD"}`; `GET /v2/locations` with `id`, `name`, `status`, `timezone`
and `currency`; the `errors` array (`category`, `code`, `detail`) with the 401 codes of the errors page and a 429 `RATE_LIMITED`.

It is a stand-in written from documentation, not a recording of Square. Its own choices, marked UNVERIFIED in the sources page: that
`end_time` is inclusive (`end_inclusive` flips it), the form of its cursors, that a `Square-Version` header is optional, the status
it puts on a bad parameter (400), and the text of its messages.
"""
from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from typing import Any

from .fake_http import Answer, FakeHTTP, Seen

_PAGE_LIMIT = 100


def errors_body(category: str, code: str, detail: str = "stand-in") -> dict:
    return {"errors": [{"category": category, "code": code, "detail": detail}]}


def _cursor(offset: int) -> str:
    return base64.b64encode(("sq:%d" % offset).encode()).decode()


def _offset(cursor: str) -> int | None:
    try:
        text = base64.b64decode(cursor.encode(), validate=True).decode()
    except ValueError:
        return None
    m = re.fullmatch(r"sq:(\d+)", text)
    return int(m.group(1)) if m else None


def _parse(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


class FakeSquare(FakeHTTP):
    def __init__(self, *, token: str = "test-token", end_inclusive: bool = True):
        super().__init__()
        self.token = token
        self.end_inclusive = end_inclusive
        #: location id -> {"name", "status", "timezone", "currency"}
        self.locations: dict[str, dict[str, str]] = {}
        #: Every payment the seller has, as Square's Payment objects.
        self.payments: list[dict[str, Any]] = []
        #: Page size used instead of 100, so a test can page without hundreds of payments.
        self.page_limit = _PAGE_LIMIT

    def set_location(self, location_id: str, *, name: str = "Stand-in shop", status: str = "ACTIVE",
                     timezone_name: str = "America/New_York", currency: str = "USD") -> None:
        with self._lock:
            self.locations[location_id] = {"id": location_id, "name": name, "status": status, "timezone": timezone_name, "currency": currency}

    def add_payment(self, payment_id: str, created_at: str, *, location_id: str, total: int, refunded: int | None = None,
                    status: str = "COMPLETED", currency: str = "USD", tip: int = 0) -> dict:
        payment: dict[str, Any] = {
            "id": payment_id, "created_at": created_at, "updated_at": created_at, "status": status, "location_id": location_id,
            "amount_money": {"amount": total - tip, "currency": currency}, "total_money": {"amount": total, "currency": currency},
            "source_type": "CARD",
        }
        if tip:
            payment["tip_money"] = {"amount": tip, "currency": currency}
        if refunded is not None:
            payment["refunded_money"] = {"amount": refunded, "currency": currency}
        with self._lock:
            self.payments.append(payment)
        return payment

    # ------------------------------------------------------------------ the request

    def answer(self, seen: Seen) -> Answer:
        if seen.method != "GET" or seen.path not in ("/v2/payments", "/v2/locations"):
            return 404, {}, errors_body("INVALID_REQUEST_ERROR", "NOT_FOUND", "Not found.")
        if seen.headers.get("authorization") != "Bearer " + self.token:
            return 401, {}, errors_body("AUTHENTICATION_ERROR", "UNAUTHORIZED", "This request could not be authorized.")
        if seen.path == "/v2/locations":
            return 200, {}, {"locations": [dict(v) for v in self.locations.values()]}
        return self._payments(seen.query)

    def _payments(self, q: dict[str, str]) -> Answer:
        location = q.get("location_id")
        if location is not None and location not in self.locations:
            return 404, {}, errors_body("INVALID_REQUEST_ERROR", "NOT_FOUND", "Location not found.")
        begin = _parse(q["begin_time"]) if "begin_time" in q else None
        end = _parse(q["end_time"]) if "end_time" in q else None
        if ("begin_time" in q and begin is None) or ("end_time" in q and end is None):
            return 400, {}, errors_body("INVALID_REQUEST_ERROR", "INVALID_TIME", "Times must be RFC 3339.")
        if q.get("sort_order", "DESC") not in ("ASC", "DESC"):
            return 400, {}, errors_body("INVALID_REQUEST_ERROR", "INVALID_ENUM_VALUE", "sort_order must be ASC or DESC.")
        limit = self.page_limit
        if "limit" in q:
            if not q["limit"].isdigit() or int(q["limit"]) < 1:
                return 400, {}, errors_body("INVALID_REQUEST_ERROR", "VALUE_TOO_LOW", "limit must be positive.")
            limit = min(int(q["limit"]), self.page_limit) if int(q["limit"]) <= _PAGE_LIMIT else self.page_limit
        selected = []
        for p in self.payments:
            if location is not None and p["location_id"] != location:
                continue
            stamp = _parse(p["created_at"])
            if begin is not None and stamp < begin:
                continue
            if end is not None and (stamp > end if self.end_inclusive else stamp >= end):
                continue
            selected.append(p)
        selected.sort(key=lambda p: _parse(p["created_at"]), reverse=q.get("sort_order", "DESC") == "DESC")
        offset = 0
        if "cursor" in q:
            offset = _offset(q["cursor"])
            if offset is None:
                return 400, {}, errors_body("INVALID_REQUEST_ERROR", "INVALID_CURSOR", "The cursor is invalid.")
        body: dict[str, Any] = {}
        page = selected[offset: offset + limit]
        if page:
            body["payments"] = page
        if offset + limit < len(selected):
            body["cursor"] = _cursor(offset + limit)
        return 200, {}, body
