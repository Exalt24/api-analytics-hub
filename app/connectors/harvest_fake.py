"""A stand-in Harvest v2 API server on 127.0.0.1, for tests.

It speaks real HTTP to the connector and implements what the saved pages describe (docs/HARVEST_GRAPH_SOURCES.md, section Harvest):

  * `Authorization: Bearer <token>`, `Harvest-Account-Id` and a `User-Agent` on every request; a missing User-Agent is a 400 (the overview page says so), a token it does
    not know is a 401, an account id that does not match is a 403.
  * `GET /v2/users/me`.
  * `GET /v2/projects` (`per_page`, `is_active`), a 403 for a key that is not an administrator or manager (`projects_forbidden=True`).
  * `GET /v2/time_entries` with `from` and `to` (both inclusive, on the page), `per_page` (1 to 2000, a larger value is a 422 "Invalid per_page parameter"), sorted by
    `spent_date`, answering `time_entries`, `per_page`, `total_pages`, `total_entries`, `page`, `next_page`, `previous_page` and `links` with `first`, `next`, `previous`, `last`.
  * 429 with a `Retry-After` header once `throttle_after` requests have been answered (the page: "The number of seconds until the throttle is lifted is sent via the
    Retry-After HTTP header").

It is a stand-in written from documentation, not a recording of Harvest. Its own choices, marked UNVERIFIED in the sources page: the cursor value inside `links.next`
(an opaque offset here), the exact wording of an error body, the order of the fields, and the pages being cut at `per_page` records.
"""
from __future__ import annotations

import base64
import json
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from .fake_http import Answer, FakeHTTP, Seen


class DecimalJSON(json.JSONEncoder):
    """Harvest sends hours and rates as JSON numbers. A Decimal is written as one, never as a string."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


class FakeHarvest(FakeHTTP):
    def __init__(self, *, token: str = "pat-token", account_id: str = "1234567", throttle_after: int | None = None, retry_after: int = 7,
                 projects_forbidden: bool = False, page_cap: int = 2000):
        super().__init__()
        self.token, self.account_id = token, account_id
        self.throttle_after, self.retry_after = throttle_after, retry_after
        self.projects_forbidden = projects_forbidden
        #: The most records a page holds, whatever per_page asks for, so a test can force several pages out of a few entries.
        self.page_cap = page_cap
        #: Ordinals (1 based, over the requests that passed authentication) that answer 503, so a test can fail a request in the middle of a run.
        self.fail_on: set[int] = set()
        self.entries: list[dict[str, Any]] = []
        self.projects: list[dict[str, Any]] = []
        self.answered = 0
        #: Set to a list of paths to make `links.next` name another place, for the test that checks the token never leaves.
        self.next_override: str | None = None
        self.broken_total: int | None = None

    # ------------------------------------------------------------------ data

    def add_project(self, pid: int, name: str, *, currency: str = "USD", budget: Decimal | int | None = None, active: bool = True, monthly: bool = False) -> None:
        self.projects.append({"id": pid, "name": name, "currency": currency, "is_active": active, "is_billable": True, "budget": budget,
                              "budget_by": "project" if budget is not None else "none", "budget_is_monthly": monthly, "cost_budget": None})

    def add_entry(self, eid: int, day: date, hours: Decimal | str, *, project: int = 1, billable: bool = True, rate: Decimal | str | int | None = 100,
                  rounded: Decimal | str | None = None, user: int = 7) -> None:
        h = Decimal(str(hours))
        self.entries.append({"id": eid, "spent_date": day.isoformat(), "user": {"id": user, "name": "Kim Allen"}, "client": {"id": 5, "name": "ABC Corp"},
                             "project": {"id": project, "name": "Marketing Website"}, "task": {"id": 3, "name": "Design"},
                             "hours": h, "rounded_hours": Decimal(str(rounded)) if rounded is not None else h, "notes": "Adding CSS", "is_running": False,
                             "billable": billable, "budgeted": True,
                             "billable_rate": None if rate is None else Decimal(str(rate)), "cost_rate": Decimal("50.0")})

    # ------------------------------------------------------------------ routing

    def answer(self, seen: Seen) -> Answer:
        if seen.method != "GET":
            return 405, {}, {"message": "method not allowed"}
        if not seen.headers.get("user-agent", "").strip() or seen.headers.get("user-agent", "").startswith("python-httpx"):
            return 400, {}, {"error": "invalid_request", "error_description": "a User-Agent header with the name of your application and a contact is required"}
        auth = seen.headers.get("authorization", "")
        if auth != "Bearer " + self.token:
            return 401, {}, {"error": "invalid_token", "error_description": "The access token provided is invalid"}
        if seen.headers.get("harvest-account-id") != self.account_id:
            return 403, {}, {"message": "Your access token does not have access to this account"}
        self.answered += 1
        if self.answered in self.fail_on:
            return 503, {}, {"message": "Service unavailable"}
        if self.throttle_after is not None and self.answered > self.throttle_after:
            return 429, {"Retry-After": str(self.retry_after)}, {"message": "Rate limit exceeded"}
        if seen.path == "/v2/users/me":
            return 200, {}, {"id": 7, "first_name": "Kim", "last_name": "Allen", "email": "kim@example.com"}
        if seen.path == "/v2/projects":
            if self.projects_forbidden:
                return 403, {}, {"message": "You do not have permission to view projects"}
            return self._list("projects", self.projects, seen)
        if seen.path == "/v2/time_entries":
            rows = sorted(self.entries, key=lambda e: (e["spent_date"], e["id"]))
            lo, hi = seen.query.get("from"), seen.query.get("to")
            if lo:
                rows = [e for e in rows if e["spent_date"] >= lo]
            if hi:
                rows = [e for e in rows if e["spent_date"] <= hi]
            return self._list("time_entries", rows, seen)
        return 404, {}, {"message": "Not found"}

    def _list(self, key: str, rows: list[dict], seen: Seen) -> Answer:
        try:
            per_page = int(seen.query.get("per_page", "2000"))
        except ValueError:
            per_page = -1
        if not 1 <= per_page <= 2000:
            return 422, {}, {"message": "Invalid per_page parameter"}
        offset = 0
        if "cursor" in seen.query:
            offset = int(base64.urlsafe_b64decode(seen.query["cursor"] + "==").decode("ascii"))
        size = min(per_page, self.page_cap)
        chunk = rows[offset:offset + size]
        end = offset + size
        total_pages = max(1, -(-len(rows) // size))
        base = "%s%s" % (self.url, seen.path)
        extra = {k: v for k, v in seen.query.items() if k in ("from", "to", "is_active")}

        def link(**q: Any) -> str:
            return "%s?%s" % (base, urlencode({**extra, **q}))

        nxt = None
        if end < len(rows):
            cursor = base64.urlsafe_b64encode(str(end).encode("ascii")).decode("ascii").rstrip("=")
            nxt = self.next_override or link(cursor=cursor, per_page=per_page, ref="next_cursor")
        body = {key: chunk, "per_page": per_page, "total_pages": total_pages, "total_entries": self.broken_total if self.broken_total is not None else len(rows),
                "next_page": (offset // size + 2) if nxt else None, "previous_page": (offset // size) if offset else None,
                "page": offset // size + 1,
                "links": {"first": link(page=1, per_page=per_page, ref="first"), "next": nxt, "previous": None,
                          "last": link(page=total_pages, per_page=per_page, ref="last")}}
        return 200, {}, json.dumps(body, cls=DecimalJSON).encode("utf-8")
