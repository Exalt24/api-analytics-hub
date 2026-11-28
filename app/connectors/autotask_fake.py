"""A stand-in Autotask REST API server on 127.0.0.1, for tests.

It speaks real HTTP to the connector and implements what the saved pages describe (docs/MSP_SOURCES.md, section Autotask, pages in docs/vendor-contract/autotask/):

  * `GET /atservicesrest/v1.0/zoneInformation?user=` answers `zoneName`, `url`, `webUrl` and `ci` with no credential, as the zone information page says.
  * Every other request needs the headers `Username`, `Secret` and `APIIntegrationcode` (the security page's example); anything else is a 401 ("Failed authentication to the REST API will
    result in the return of a 401 error").
  * `GET .../ThresholdInformation` with `externalRequestThreshold`, `requestThresholdTimeframe` and `currentTimeframeRequestCount`; `.../Tickets/entityInformation` and
    `.../Tickets/entityInformation/fields` with picklist values shaped as the entityInformation page shows them.
  * `GET .../Tickets/query` and `.../TimeEntries/query` with `search={"MaxRecords", "IncludeFields", "filter"}`: the operators and, or, eq, noteq, gt, gte, lt, lte, exist, notExist, in and
    notIn; records in id order, at most 500 a page ("Autotask returns only 500 records at once, sorted by the id value"); `items` and `pageDetails` with `count`, `requestCount`,
    `prevPageUrl` and `nextPageUrl`; a next page at `.../query/next?paging=...&search=...`; and a 400 when more than a page matches and `IncludeFields` leaves `id` out ("For search
    results that contain more than 500 results, you must include Id as a field").
  * 429 once `throttle_after` authenticated requests have been answered (the thread limiting page: "The second call fails with a 429 error response").

Its own choices, UNVERIFIED against Autotask and listed in docs/MSP_SOURCES.md: the shape of an error body, the `paging` value inside `nextPageUrl` (an opaque id here), the order of the
fields, whether a 429 carries `Retry-After` (here only when a test sets one), the picklist values, the status of a refused hourly threshold (none is refused here), and that a field name
matches whatever its case.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from .fake_http import Answer, FakeHTTP, Seen
from .harvest_fake import DecimalJSON

API = "/atservicesrest/v1.0"


def stamp(moment: datetime | None) -> str | None:
    """Autotask answers UTC with no zone and milliseconds."""
    return None if moment is None else moment.replace(tzinfo=None).isoformat(timespec="milliseconds")


def _plist(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    return [{"value": v, "label": label, "isDefaultValue": i == 0, "sortOrder": i, "parentValue": "", "isActive": True, "isSystem": True} for i, (v, label) in enumerate(pairs)]


DEFAULT_PICKLISTS = {
    "status": _plist(("1", "New"), ("5", "Complete"), ("8", "In Progress")),
    "priority": _plist(("1", "High"), ("2", "Medium"), ("3", "Low")),
    "ticketCategory": _plist(("1", "Standard"), ("2", "RMM Alerts"), ("3", "Change Request")),
    "issueType": _plist(("10", "Email"), ("11", "Network"), ("12", "Workstation")),
}


class FakeAutotask(FakeHTTP):
    def __init__(self, *, username: str = "api@example.com", secret: str = "s3cret-value", code: str = "ASHJKLFDKAHKASLFH85LSA905H", threshold: int = 10000,
                 used: int = 0, page_cap: int = 500, throttle_after: int | None = None, retry_after: int | None = None):
        super().__init__()
        self.username, self.secret, self.code = username, secret, code
        self.threshold, self.used = threshold, used
        self.page_cap, self.throttle_after, self.retry_after = page_cap, throttle_after, retry_after
        self.tickets: list[dict[str, Any]] = []
        self.entries: list[dict[str, Any]] = []
        self.picklists = {k: list(v) for k, v in DEFAULT_PICKLISTS.items()}
        self.answered = 0
        self.fail_on: set[int] = set()
        #: The url the zone answer names; None names this server.
        self.zone_url: str | None = None
        #: A url for `nextPageUrl` to name instead of the real one, for the test that checks the credentials never leave.
        self.next_override: str | None = None

    @property
    def api_url(self) -> str:
        return self.url + "/atservicesrest"

    # ------------------------------------------------------------------ data

    def add_ticket(self, tid: int, created: datetime, *, title: str = "Printer is offline", category: int | None = 1, status: int = 1, priority: int | None = 2,
                   issue: int | None = 10, company: int | None = 5, completed: datetime | None = None, resolved: datetime | None = None,
                   first_response: datetime | None = None, first_response_due: datetime | None = None, resolved_due: datetime | None = None,
                   due: datetime | None = None) -> dict[str, Any]:
        row = {"id": tid, "title": title, "createDate": stamp(created), "completedDate": stamp(completed), "resolvedDateTime": stamp(resolved), "resolvedDueDateTime": stamp(resolved_due),
               "dueDateTime": stamp(due), "firstResponseDateTime": stamp(first_response), "firstResponseDueDateTime": stamp(first_response_due), "status": 5 if completed else status,
               "priority": priority, "ticketCategory": category, "issueType": issue, "companyID": company, "description": "x" * 40, "resolution": None, "userDefinedFields": []}
        self.tickets.append(row)
        return row

    def add_time(self, eid: int, worked: datetime, hours: Any, *, ticket: int | None = None, resource: int = 7) -> dict[str, Any]:
        row = {"id": eid, "ticketID": ticket, "taskID": None, "resourceID": resource, "dateWorked": stamp(worked), "hoursWorked": hours, "summaryNotes": "notes"}
        self.entries.append(row)
        return row

    # ------------------------------------------------------------------ routing

    def answer(self, seen: Seen) -> Answer:
        if seen.method != "GET":
            return 405, {}, {"errors": ["method not allowed"]}
        if seen.path == API + "/zoneInformation":
            if seen.query.get("user") != self.username:
                return 200, {}, {"zoneName": "Unknown", "url": "", "webUrl": "", "ci": 0}
            return 200, {}, {"zoneName": "America East", "url": (self.zone_url or self.api_url) + "/", "webUrl": self.url + "/", "ci": 20264}
        h = seen.headers
        if h.get("username") != self.username or h.get("secret") != self.secret or h.get("apiintegrationcode") != self.code:
            return 401, {}, {"errors": ["Unauthorized"]}
        self.answered += 1
        if self.answered in self.fail_on:
            return 503, {}, {"errors": ["Service unavailable"]}
        if self.throttle_after is not None and self.answered > self.throttle_after:
            return 429, ({"Retry-After": str(self.retry_after)} if self.retry_after is not None else {}), {"errors": ["thread count exceeded"]}
        if not seen.path.startswith(API + "/"):
            return 404, {}, {"errors": ["not found"]}
        route = seen.path[len(API):]
        if route == "/ThresholdInformation":
            return 200, {}, {"externalRequestThreshold": self.threshold, "requestThresholdTimeframe": 60, "currentTimeframeRequestCount": self.used + self.answered}
        if route == "/Tickets/entityInformation":
            return 200, {}, {"info": {"name": "Ticket", "canCreate": True, "canDelete": False, "canQuery": True, "canUpdate": True, "hasUserDefinedFields": True,
                                      "supportsWebhookCallouts": True}}
        if route == "/Tickets/entityInformation/fields":
            fields = [{"name": name, "dataType": "integer", "length": 0, "isRequired": False, "isReadOnly": False, "isQueryable": True, "isReference": False,
                       "referenceEntityType": "", "isPickList": True, "picklistValues": values, "picklistParentValueField": "", "isSupportedWebhookField": True}
                      for name, values in self.picklists.items()]
            fields.append({"name": "title", "dataType": "string", "length": 255, "isRequired": True, "isReadOnly": False, "isQueryable": True, "isReference": False,
                           "referenceEntityType": "", "isPickList": False, "picklistValues": None, "picklistParentValueField": "", "isSupportedWebhookField": True})
            return 200, {}, {"fields": fields}
        for entity, rows in (("Tickets", self.tickets), ("TimeEntries", self.entries)):
            if route in ("/%s/query" % entity, "/%s/query/next" % entity):
                return self._query(entity, route.endswith("/next"), rows, seen)
        return 404, {}, {"errors": ["not found"]}

    # ------------------------------------------------------------------ the query language

    @staticmethod
    def _naive_utc(text: str) -> datetime:
        moment = datetime.fromisoformat(text)
        return moment if moment.tzinfo is None else moment.astimezone(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _value(row: dict[str, Any], field: str) -> Any:
        for key, value in row.items():
            if key.lower() == str(field).lower():
                return value
        raise KeyError(field)

    def _match(self, row: dict[str, Any], expr: dict[str, Any]) -> bool:
        op = expr["op"]
        if op in ("and", "or"):
            results = [self._match(row, item) for item in expr["items"]]
            return all(results) if op == "and" else any(results)
        value = self._value(row, expr["field"])
        if op == "exist":
            return value is not None
        if op == "notExist":
            return value is None
        want = expr["value"]
        if op in ("in", "notIn"):
            return (value in want) == (op == "in")
        if value is None:
            return op == "noteq"
        if isinstance(value, str) and isinstance(want, str):
            try:
                value, want = self._naive_utc(value), self._naive_utc(want)
            except ValueError:
                pass
        return {"eq": value == want, "noteq": value != want, "gt": value > want, "gte": value >= want, "lt": value < want, "lte": value <= want}[op]

    def _query(self, entity: str, is_next: bool, rows: list[dict[str, Any]], seen: Seen) -> Answer:
        try:
            search = json.loads(seen.query["search"])
        except (KeyError, ValueError):
            return 400, {}, {"errors": ["Invalid query syntax."]}
        if len(seen.query["search"]) > 2048:
            return 400, {}, {"errors": ["The query string is longer than 2048 characters."]}
        size = search.get("MaxRecords", 500)
        if not isinstance(size, int) or not 1 <= size <= 500:
            return 400, {}, {"errors": ["MaxRecords must be 1 to 500."]}
        size = min(size, self.page_cap)
        try:
            matched = [r for r in sorted(rows, key=lambda r: r["id"]) if all(self._match(r, f) for f in search.get("filter", []))]
        except (KeyError, TypeError, ValueError):
            return 400, {}, {"errors": ["Invalid query syntax."]}
        include = search.get("IncludeFields")
        if include is not None and len(matched) > size and "id" not in [str(f).lower() for f in include]:
            return 400, {}, {"errors": ["For search results that contain more than %d results, you must include Id as a field." % size]}
        after = 0
        if is_next:
            try:
                after = json.loads(seen.query["paging"])["nextIds"][0]
            except (KeyError, ValueError, IndexError, TypeError):
                return 400, {}, {"errors": ["Invalid paging."]}
        chunk = [r for r in matched if r["id"] > after][:size]
        more = bool(chunk) and chunk[-1]["id"] < matched[-1]["id"]
        if include is not None:
            wanted = {str(f).lower() for f in include}
            chunk = [{k: v for k, v in r.items() if k.lower() in wanted} for r in chunk]
        nxt = None
        if more:
            nxt = self.next_override or "%s%s/%s/query/next?%s" % (self.url, API, entity, urlencode({"paging": json.dumps({"pageSize": size, "previousIds": [after], "nextIds": [chunk[-1]["id"]]}, separators=(",", ":")),
                                                                                                           "search": seen.query["search"]}))
        body = {"items": chunk, "pageDetails": {"count": len(chunk), "requestCount": size, "prevPageUrl": None, "nextPageUrl": nxt}}
        return 200, {}, json.dumps(body, cls=DecimalJSON).encode("utf-8")
