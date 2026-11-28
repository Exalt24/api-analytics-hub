"""A stand-in ConnectWise Manage API server on 127.0.0.1, for tests.

It speaks real HTTP to the connector and implements what the saved pages describe (docs/MSP_SOURCES.md, section ConnectWise), which are NOT ConnectWise's own pages (the
developer portal needs a login and refuses a free mailbox address at registration):

  * `Authorization: Basic base64(company+publicKey:privateKey)` and a `clientId` header on every request (the OpenAPI file marks `clientId` required, the blog post shows both).
  * `GET {api_path}/system/info`, `/service/tickets`, `/service/tickets/{id}` and `/time/entries`.
  * Forward-only paging when the request carries `pagination-type: forward-only`: records in id order, `pageSize`, a `pageId` that acts "like an additional condition of Id > pageId"
    (the blog's words), and a `Link: <url>; rel="next"` header while more remain. Without that header: navigable paging by `page`, with `rel="next"` and `rel="last"`.
  * 429 with a `Retry-After` header once `throttle_after` requests have been answered.

Its own choices, UNVERIFIED against ConnectWise and written down in docs/MSP_SOURCES.md: the 401 for a wrong key or a missing `clientId` (third party sources say 401, no saved page does),
the `Retry-After` on a 429 (no saved page says ConnectWise sends one), the shape of `/system/info`, the page cap a test sets, and the `conditions` parameter, which it records and
IGNORES (the grammar is on no saved page, so it implements none).
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from .fake_http import Answer, FakeHTTP, Seen
from .harvest_fake import DecimalJSON


def iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeConnectWise(FakeHTTP):
    def __init__(self, *, company: str = "acme", public_key: str = "pubkey", private_key: str = "privkey", client_id: str = "client-guid-1234",
                 api_path: str = "/v4_6_release/apis/3.0", throttle_after: int | None = None, retry_after: int = 7, page_cap: int = 1000):
        super().__init__()
        self.company, self.public_key, self.private_key, self.client_id = company, public_key, private_key, client_id
        self.api_path = api_path
        self.throttle_after, self.retry_after, self.page_cap = throttle_after, retry_after, page_cap
        self.tickets: list[dict[str, Any]] = []
        self.entries: list[dict[str, Any]] = []
        #: What `GET /service/tickets/{id}` answers over the list's values: the current SLA times of an active ticket.
        self.fresh: dict[int, dict[str, Any]] = {}
        self.answered = 0
        self.fail_on: set[int] = set()
        #: A URL for `Link rel="next"` to name instead of the real one, for the test that checks the keys never leave.
        self.next_override: str | None = None
        #: Set to send `Link` on the last page too, naming the first page again (a changed cursor that loops).
        self.loop_back = False

    @property
    def api_url(self) -> str:
        return self.url + self.api_path

    @property
    def basic(self) -> str:
        return base64.b64encode(("%s+%s:%s" % (self.company, self.public_key, self.private_key)).encode()).decode()

    # ------------------------------------------------------------------ data

    def add_ticket(self, tid: int, opened: datetime, *, summary: str = "Printer is offline", board: str = "Service Board", ttype: str | None = "Incident",
                   priority: str | None = "Priority 3 - Normal", status: str = "New", company: str | None = "Acme Co", closed_at: datetime | None = None,
                   resolved_at: datetime | None = None, responded_at: datetime | None = None, respond_goal: datetime | None = None,
                   resolution_goal: datetime | None = None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": tid, "summary": summary, "recordType": "ServiceTicket", "board": {"id": 1, "name": board}, "status": {"id": 2, "name": status},
            "company": None if company is None else {"id": 5, "identifier": "ACME", "name": company},
            "type": None if ttype is None else {"id": 3, "name": ttype},
            "priority": None if priority is None else {"id": 4, "name": priority, "sort": 3},
            "closedFlag": closed_at is not None, "actualHours": 0.0,
            "_info": {"lastUpdated": iso(opened), "updatedBy": "kim", "dateEntered": iso(opened), "enteredBy": "kim"}}
        if closed_at is not None:
            row["closedDate"] = iso(closed_at)
        for key, value in (("dateResolved", resolved_at), ("dateResponded", responded_at), ("respondByGoalUTC", respond_goal), ("resolutionGoalUTC", resolution_goal)):
            if value is not None:
                row[key] = iso(value)
        self.tickets.append(row)
        return row

    def add_time(self, eid: int, start: datetime, hours: Any, *, ticket: int | None = None, member: str = "Kim Allen") -> dict[str, Any]:
        row: dict[str, Any] = {"id": eid, "member": {"id": 9, "identifier": "KAllen", "name": member}, "timeStart": iso(start), "actualHours": hours,
                               "billableOption": "Billable", "_info": {"lastUpdated": iso(start)}}
        if ticket is not None:
            row["ticket"] = {"id": ticket, "summary": "x"}
            row["chargeToId"], row["chargeToType"] = ticket, "ServiceTicket"
        self.entries.append(row)
        return row

    # ------------------------------------------------------------------ routing

    def answer(self, seen: Seen) -> Answer:
        if seen.method != "GET":
            return 405, {}, {"code": "MethodNotAllowed", "message": "method not allowed"}
        if seen.headers.get("authorization") != "Basic " + self.basic or seen.headers.get("clientid") != self.client_id:
            return 401, {}, {"code": "Unauthorized", "message": "Unauthorized"}
        self.answered += 1
        if self.answered in self.fail_on:
            return 503, {}, {"code": "ServiceUnavailable", "message": "unavailable"}
        if self.throttle_after is not None and self.answered > self.throttle_after:
            return 429, {"Retry-After": str(self.retry_after)}, {"code": "TooManyRequests", "message": "slow down"}
        path = seen.path
        if not path.startswith(self.api_path + "/"):
            return 404, {}, {"code": "NotFound", "message": "not found"}
        route = path[len(self.api_path):]
        if route == "/system/info":
            return 200, {}, {"version": "v2026.1.1", "isCloud": True, "serverTimeZone": "Eastern Standard Time"}
        if route == "/service/tickets":
            return self._list(route, self.tickets, seen)
        if route == "/time/entries":
            return self._list(route, self.entries, seen)
        if route.startswith("/service/tickets/"):
            tail = route.rsplit("/", 1)[1]
            for row in self.tickets:
                if str(row["id"]) == tail:
                    return 200, {}, json.dumps({**row, **self.fresh.get(row["id"], {})}, cls=DecimalJSON).encode("utf-8")
            return 404, {}, {"code": "NotFound", "message": "ticket not found"}
        return 404, {}, {"code": "NotFound", "message": "not found"}

    def _list(self, route: str, rows: list[dict], seen: Seen) -> Answer:
        try:
            size = int(seen.query.get("pageSize", "25"))
        except ValueError:
            size = -1
        if not 1 <= size <= 1000:
            return 400, {}, {"code": "InvalidParameter", "message": "pageSize must be 1 to 1000"}
        size = min(size, self.page_cap)
        rows = sorted(rows, key=lambda r: r["id"])
        forward = seen.headers.get("pagination-type") == "forward-only"
        base = self.api_url + route
        if forward:
            after = int(seen.query.get("pageId", "0"))
            chunk = [r for r in rows if r["id"] > after][:size]
            more = bool(chunk) and chunk[-1]["id"] < rows[-1]["id"]
            links = []
            if more or self.loop_back:
                nxt = self.next_override or "%s?%s" % (base, urlencode({"pageSize": size, "page": 1, "pageId": chunk[-1]["id"] if more else 0}))
                links.append('<%s>; rel="next"' % nxt)
        else:
            page = int(seen.query.get("page", "1"))
            chunk = rows[(page - 1) * size: page * size]
            last = max(1, -(-len(rows) // size))
            links = []
            if page < last:
                links.append('<%s?%s>; rel="next"' % (base, urlencode({"pageSize": size, "page": page + 1})))
            links.append('<%s?%s>; rel="last"' % (base, urlencode({"pageSize": size, "page": last})))
        headers = {"Link": ", ".join(links)} if links else {}
        return 200, headers, json.dumps(chunk, cls=DecimalJSON).encode("utf-8")
