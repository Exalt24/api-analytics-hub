"""Autotask PSA connector: tickets and technician time, as daily figures and as ticket records for the MSP backlog analysis.

The fifteenth connector behind the contract in `base.py`. It reads the Autotask REST API's Tickets and TimeEntries and writes four daily keys:

    at_tickets_opened             tickets created that day (the UTC day of `createDate`)
    at_tickets_closed             tickets completed that day (the UTC day of `completedDate`)
    at_resolution_goals_missed    tickets whose resolution goal fell on that day and were resolved after it, or are still unresolved now
    at_time_seconds               technician time worked that day, in whole seconds (`hoursWorked` x 3600, on the UTC day of `dateWorked`)

`tickets()` and `time_entries()` return the same data as `TicketRecord` and `TimeRecord` (`app/msp/records.py`) for `app/msp/analysis.py`.

WRITTEN FROM AUTOTASK'S PUBLIC DEVELOPER HELP, AND TESTED ONLY AGAINST A STAND-IN. The pages (REST security and authentication, zone information, thresholds, basic and advanced
queries, pagination, the Tickets and TimeEntries entities, picklists, thread limiting) were read in Chromium on 2026-11-29 and saved in docs/vendor-contract/autotask/. Nothing here
ran against a live Autotask database: none is held. Every fact, its page and what the pages leave unstated is in docs/MSP_SOURCES.md (section Autotask).

WHAT IT CALLS. The zone first ("make a ZoneInformation request first ... The request does not require authentication"): `GET {webservices.autotask.net}/atservicesrest/v1.0/zoneInformation?user=`
gives the API `url` of the user's zone, which must be https on an `autotask.net` host (a lookup against a loopback stand-in may answer loopback) so the credentials are never sent anywhere
else. Then, every request with the headers the security page shows (`Username`, `Secret`, `APIIntegrationcode`, `Content-Type`):

  * `GET .../v1.0/ThresholdInformation`, once per `fetch`, to pace from what the platform reports: the sync stops before a call when fewer than `MIN_CALLS_LEFT` of the hourly
    external requests are left (the page: "limit of 10,000 on the number of external requests allowed per hour"; the count covers every integration on the database);
  * `GET .../Tickets/entityInformation/fields` for the picklists that turn a status, priority, category and issue type number into its label;
  * `GET .../Tickets/query?search={...}` and `.../TimeEntries/query?search={...}` with `MaxRecords` 500 ("The valid range of values for MaxRecords is 1 to 500") and `IncludeFields`
    (the ticket entity carries two 32,000 character rich text fields that a sync never needs), following `pageDetails.nextPageUrl` exactly as given ("Do not change the query method, the MaxRecords
    or IncludeFields values, or any filter criteria while using paging URLs") and never leaving the API host and path.

The ticket query is one OR group: created in the window, completed in the window, whose resolution goal falls in the window (a ticket completed long after its goal is a missed
goal on the goal's day, whenever it was completed), or not completed at all (`completedDate` `notExist`), so the open backlog is read whole. Time worked on
a ticket the group did not return (an older, completed one) is matched to it by a second query, `id` `in [...]`, in chunks of 100 (the connector's own bound).

REQUESTS ARE SERIAL, which the best practices page asks for ("Serialize your requests") and which keeps the connector inside the thread limit ("three per object endpoint and tracking
identifier"). A 429 (the status the thread limiting page names) and a 500, 502, 503 or 504 are retried with a bound, waiting for `Retry-After` if one is sent (no saved page says Autotask
sends one) and otherwise backing off; a wait longer than the retrier's cap goes to the runner. A 401 means the user name, secret or integration code is wrong: `AuthRevoked`.

WHAT THE PAGES DO NOT SAY, and what the connector does about it. The picklist value that means a ticket is complete is not on a saved page, so a ticket is closed when it has a
`completedDate`. Which field is the SLA resolution goal is not described: `resolvedDueDateTime` is the default (`resolution_due_field` can name `dueDateTime` instead). Company and
technician names need other entities, so a company is `company <id>` and a technician `resource <id>`. The status of a rejected hourly threshold is not stated.

The secret and the integration code never appear in an error message: only the HTTP status is quoted.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Callable, Mapping
from urllib.parse import urlparse

import httpx

from app.crypto import Secret
from app.msp.records import RecordError, TicketRecord, TimeRecord

from .base import AuthRevoked, CanonicalPoint, Connector, FetchResult, RateLimited, UpstreamBroken
from .common import Retrier, Retry, retry_after
from .msp_common import LOOPBACK, checked_base, days_of, decimal_json, parse_instant, seconds_from_hours, utc_day

__all__ = ["AutotaskConnector", "METRICS", "SUPPORTED", "MAX_RECORDS", "MIN_CALLS_LEFT"]

METRICS = ("at_tickets_opened", "at_tickets_closed", "at_resolution_goals_missed", "at_time_seconds")
SUPPORTED = frozenset(METRICS)
#: "The valid range of values for MaxRecords is 1 to 500 per page of results."
MAX_RECORDS = 500
MAX_PAGES = 5000
#: The connector's own bound: stop when fewer than this many of the hour's external requests are left. No page names a safe margin.
MIN_CALLS_LEFT = 200
#: The connector's own bound on one `id in [...]` list: the pages give a limit of 500 OR conditions and none for `in`.
ID_CHUNK = 100
ZONE_LOOKUP = "https://webservices.autotask.net"
API_PATH = "/atservicesrest/v1.0"
NO_CATEGORY = "(no category)"
TICKET_FIELDS = ["id", "title", "createDate", "completedDate", "resolvedDateTime", "resolvedDueDateTime", "dueDateTime", "firstResponseDateTime", "firstResponseDueDateTime",
                 "status", "priority", "ticketCategory", "issueType", "companyID"]
TIME_FIELDS = ["id", "ticketID", "resourceID", "dateWorked", "hoursWorked"]
PICKLISTS = ("status", "priority", "ticketCategory", "issueType")


def _is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _stamp(day: date) -> str:
    """"submit date-time values in the format yyyy-mm-ddThh:mm:ss.ms" (the introduction to calls page); the API's times are UTC."""
    return "%sT00:00:00.000" % day.isoformat()


def _search(obj: Mapping[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"))


class AutotaskConnector(Connector):
    platform = "autotask"
    supported_metrics = SUPPORTED

    def __init__(
        self,
        *,
        username: str,
        secret: str,
        integration_code: str,
        api_url: str | None = None,
        zone_lookup_url: str = ZONE_LOOKUP,
        resolution_due_field: str = "resolvedDueDateTime",
        client: httpx.AsyncClient | None = None,
        retrier: Retrier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        **retry_options: Any,
    ):
        for name, value in (("username", username), ("secret", secret), ("integration_code", integration_code)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("%s is required" % name)
        if "@" not in username:
            raise ValueError("username must be the API user's email address")
        if resolution_due_field not in ("resolvedDueDateTime", "dueDateTime"):
            raise ValueError("resolution_due_field must be resolvedDueDateTime or dueDateTime")
        self.username = username.strip()
        self._secret = Secret(secret)
        self._code = Secret(integration_code)
        self._lookup = checked_base(zone_lookup_url, what="zone_lookup_url", allow_path=False)
        self._api_root = checked_base(api_url, what="api_url", allow_path=True) if api_url else None
        if self._api_root is not None and urlparse(self._api_root).path != "/atservicesrest":
            raise ValueError("api_url must end in /atservicesrest, as the zone pages show, got %r" % (api_url,))
        self._resolution_field = resolution_due_field
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._retrier = retrier or Retrier(**retry_options)
        self._clock = clock
        self._spent = [0, 0]
        self._picklists: dict[str, dict[str, str]] | None = None
        self.degraded_metrics: set[str] = set()
        self.tickets_read: list[TicketRecord] = []
        self.time_read: list[TimeRecord] = []

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "AutotaskConnector":
        """Build from AUTOTASK_USERNAME, AUTOTASK_SECRET and AUTOTASK_INTEGRATION_CODE (AUTOTASK_API_URL and AUTOTASK_ZONE_LOOKUP_URL for a stand-in)."""
        import os

        env = os.environ if env is None else env
        names = ("AUTOTASK_USERNAME", "AUTOTASK_SECRET", "AUTOTASK_INTEGRATION_CODE")
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise ValueError("set %s in the environment" % ", ".join(missing))
        if env.get("AUTOTASK_API_URL"):
            kwargs.setdefault("api_url", env["AUTOTASK_API_URL"])
        if env.get("AUTOTASK_ZONE_LOOKUP_URL"):
            kwargs.setdefault("zone_lookup_url", env["AUTOTASK_ZONE_LOOKUP_URL"])
        return cls(username=env["AUTOTASK_USERNAME"], secret=env["AUTOTASK_SECRET"], integration_code=env["AUTOTASK_INTEGRATION_CODE"], **kwargs)

    def __repr__(self) -> str:
        return "AutotaskConnector(user=%r)" % self.username

    @property
    def external_account(self) -> str:
        return self.username.lower()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ transport

    @staticmethod
    def _classify(response: httpx.Response) -> Retry | None:
        http = response.status_code
        if http == 200:
            return None
        if http == 401:
            raise AuthRevoked("Autotask refused the user name, secret or integration code (HTTP 401)")
        if http == 429:
            return Retry(retry_after(response), "HTTP 429")
        if http in (500, 502, 503, 504):
            return Retry(retry_after(response), "HTTP %d" % http)
        raise UpstreamBroken("Autotask refused the request (HTTP %d)" % http)

    async def _send(self, url: str, params: Mapping[str, str] | None, *, authenticated: bool) -> Any:
        async def send() -> httpx.Response:
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            if authenticated:
                headers.update({"Username": self.username, "Secret": self._secret.reveal(), "APIIntegrationcode": self._code.reveal()})
            return await self._client.get(url, params=params, headers=headers)

        response, calls, waits = await self._retrier.run(send, self._classify)
        self._spent[0] += calls
        self._spent[1] += waits
        try:
            return decimal_json(response.text)
        except ValueError:
            raise UpstreamBroken("Autotask answered 200 with a body that is not JSON") from None

    async def _zone(self) -> str:
        """The API root of the user's zone. Looked up once, before anything is sent with a credential."""
        if self._api_root is not None:
            return self._api_root
        body = await self._send(self._lookup + "/atservicesrest/v1.0/zoneInformation", {"user": self.username}, authenticated=False)
        url = body.get("url") if isinstance(body, dict) else None
        if not isinstance(url, str):
            raise UpstreamBroken("the zone answer names no url")
        parsed = urlparse(url)
        lookup_loopback = urlparse(self._lookup).hostname in LOOPBACK
        trusted = (parsed.scheme == "https" and (parsed.hostname or "").endswith(".autotask.net")) or (lookup_loopback and parsed.hostname in LOOPBACK and parsed.scheme == "http")
        if not trusted or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/atservicesrest":
            raise UpstreamBroken("the zone answer names an address that is not an Autotask API, refusing to send the credentials there")
        self._api_root = "%s://%s/atservicesrest" % (parsed.scheme, parsed.netloc)
        return self._api_root

    async def _call(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        return await self._send((await self._zone()) + "/v1.0" + path, params, authenticated=True)

    def _checked_next(self, url: Any) -> str:
        root = urlparse(self._api_root or "")
        parsed = urlparse(url) if isinstance(url, str) else None
        if (parsed is None or (parsed.scheme, parsed.netloc) != (root.scheme, root.netloc) or ".." in parsed.path or not parsed.path.startswith(API_PATH + "/")):
            raise UpstreamBroken("the answer names a next page outside the Autotask API, refusing to send the credentials there")
        return url

    async def _query(self, entity: str, search: Mapping[str, Any]) -> list[dict]:
        """Every record of a query, paged by `pageDetails.nextPageUrl` as given. The page's own count must equal what arrived, and a repeated page or record is a broken answer."""
        records: list[dict] = []
        ids: set[int] = set()
        seen: set[str] = set()
        body = await self._call("/%s/query" % entity, {"search": _search(search)})
        for _ in range(MAX_PAGES):
            items = body.get("items") if isinstance(body, dict) else None
            details = body.get("pageDetails") if isinstance(body, dict) else None
            if not isinstance(items, list) or not isinstance(details, dict):
                raise UpstreamBroken("the %s answer has no items list and pageDetails" % entity)
            count = details.get("count")
            if _is_id(count) and count != len(items):
                raise UpstreamBroken("pageDetails says %d records and %d arrived" % (count, len(items)))
            if len(items) > MAX_RECORDS:
                raise UpstreamBroken("a page of %s holds %d records, more than the %d it was asked for" % (entity, len(items), MAX_RECORDS))
            for r in items:
                if not isinstance(r, dict) or not _is_id(r.get("id")):
                    raise UpstreamBroken("a record of %s has no usable id" % entity)
                if r["id"] in ids:
                    raise UpstreamBroken("record %d of %s appears twice" % (r["id"], entity))
                ids.add(r["id"])
                records.append(r)
            nxt = details.get("nextPageUrl")
            if nxt is None:
                return records
            nxt = self._checked_next(nxt)
            if nxt in seen:
                raise UpstreamBroken("a next page names itself")
            seen.add(nxt)
            body = await self._send(nxt, None, authenticated=True)
        raise UpstreamBroken("more than %d pages in one query" % MAX_PAGES)

    async def verify(self) -> str:
        body = await self._call("/Tickets/entityInformation")
        info = body.get("info") if isinstance(body, dict) else None
        if not isinstance(info, dict) or not isinstance(info.get("name"), str):
            raise UpstreamBroken("the entityInformation answer is not an entity description")
        return "Autotask API user %s" % self.username

    async def _check_headroom(self) -> None:
        body = await self._call("/ThresholdInformation")
        try:
            limit, used = body["externalRequestThreshold"], body["currentTimeframeRequestCount"]
        except (TypeError, KeyError):
            raise UpstreamBroken("the ThresholdInformation answer has no request threshold and count") from None
        if not _is_id(limit) or not _is_id(used) or limit < 1 or used < 0:
            raise UpstreamBroken("the ThresholdInformation answer is not whole numbers")
        if limit - used < MIN_CALLS_LEFT:
            raise RateLimited(None, "%d of the %d external requests this hour are used, and a sync needs more than %d left" % (used, limit, MIN_CALLS_LEFT))

    # ------------------------------------------------------------------ picklists

    async def _labels(self) -> dict[str, dict[str, str]]:
        """Field name -> picklist value -> label, from `Tickets/entityInformation/fields`."""
        if self._picklists is not None:
            return self._picklists
        body = await self._call("/Tickets/entityInformation/fields")
        fields = body.get("fields") if isinstance(body, dict) else None
        if not isinstance(fields, list):
            raise UpstreamBroken("the entityInformation/fields answer has no fields list")
        out: dict[str, dict[str, str]] = {name: {} for name in PICKLISTS}
        for f in fields:
            if not isinstance(f, dict) or f.get("name") not in PICKLISTS or not f.get("isPickList"):
                continue
            values = f.get("picklistValues")
            if not isinstance(values, list):
                raise UpstreamBroken("the %s picklist has no values list" % f["name"])
            for v in values:
                if not isinstance(v, dict) or not isinstance(v.get("value"), str) or not isinstance(v.get("label"), str):
                    raise UpstreamBroken("a %s picklist value has no value and label" % f["name"])
                out[f["name"]][v["value"]] = v["label"].strip()
        self._picklists = out
        return out

    @staticmethod
    def _label(labels: dict[str, dict[str, str]], field: str, value: Any) -> str | None:
        if value is None:
            return None
        if not _is_id(value):
            raise UpstreamBroken("the %s of a ticket is not a picklist number" % field)
        label = labels[field].get(str(value))
        return label if label else "%s %d" % (field, value)

    # ------------------------------------------------------------------ records

    def _ticket(self, r: dict, labels: dict[str, dict[str, str]]) -> TicketRecord:
        tid = r["id"]

        def when(key: str, *, required: bool = False) -> datetime | None:
            if r.get(key) is None:
                if required:
                    raise UpstreamBroken("ticket %d has no %s" % (tid, key))
                return None
            return parse_instant(r[key], "the %s of ticket %d" % (key, tid), naive_is_utc=True)

        title = r.get("title")
        if not isinstance(title, str):
            raise UpstreamBroken("the title of ticket %d is not text" % tid)
        status = self._label(labels, "status", r.get("status"))
        if status is None:
            raise UpstreamBroken("ticket %d has no status" % tid)
        company = r.get("companyID")
        if company is not None and not _is_id(company):
            raise UpstreamBroken("the companyID of ticket %d is not a number" % tid)
        completed = when("completedDate")
        try:
            return TicketRecord(
                source="autotask", ticket_id=str(tid), title=title.strip() or "(no title)",
                category=self._label(labels, "ticketCategory", r.get("ticketCategory")) or NO_CATEGORY,
                subcategory=self._label(labels, "issueType", r.get("issueType")),
                priority=self._label(labels, "priority", r.get("priority")), status=status,
                company=None if company is None else "company %d" % company,
                opened_at=when("createDate", required=True),  # type: ignore[arg-type]
                closed=completed is not None, closed_at=completed, resolved_at=when("resolvedDateTime"),
                first_response_at=when("firstResponseDateTime"), first_response_due_at=when("firstResponseDueDateTime"),
                resolution_due_at=when(self._resolution_field))
        except RecordError as exc:
            raise UpstreamBroken(str(exc)) from None

    def _time(self, r: dict) -> TimeRecord:
        eid = r["id"]
        worked = parse_instant(r.get("dateWorked"), "the dateWorked of time entry %d" % eid, naive_is_utc=True)
        seconds = seconds_from_hours(r.get("hoursWorked"), "the hoursWorked of time entry %d" % eid)
        resource = r.get("resourceID")
        if not _is_id(resource):
            raise UpstreamBroken("time entry %d names no resource" % eid)
        ticket = r.get("ticketID")
        if ticket is not None and not _is_id(ticket):
            raise UpstreamBroken("the ticketID of time entry %d is not a number" % eid)
        try:
            return TimeRecord(source="autotask", entry_id=str(eid), ticket_id=None if ticket is None else str(ticket), technician="resource %d" % resource,
                              worked_on=worked, seconds=seconds)
        except RecordError as exc:
            raise UpstreamBroken(str(exc)) from None

    async def _ticket_rows(self, start: date, end: date) -> list[dict]:
        window = lambda field: {"op": "and", "items": [{"op": "gte", "field": field, "value": _stamp(start)}, {"op": "lt", "field": field, "value": _stamp(end)}]}  # noqa: E731
        search = {"MaxRecords": MAX_RECORDS, "IncludeFields": TICKET_FIELDS,
                  "filter": [{"op": "or", "items": [window("createDate"), window("completedDate"), window(self._resolution_field), {"op": "notExist", "field": "completedDate"}]}]}
        return await self._query("Tickets", search)

    async def _time_rows(self, start: date, end: date) -> list[dict]:
        search = {"MaxRecords": MAX_RECORDS, "IncludeFields": TIME_FIELDS,
                  "filter": [{"op": "and", "items": [{"op": "gte", "field": "dateWorked", "value": _stamp(start)}, {"op": "lt", "field": "dateWorked", "value": _stamp(end)}]}]}
        return await self._query("TimeEntries", search)

    async def tickets(self, *, window_start: date, window_end: date) -> list[TicketRecord]:
        """The tickets created or completed in the window and every ticket not yet completed, as records."""
        labels = await self._labels()
        return [self._ticket(r, labels) for r in await self._ticket_rows(window_start, window_end)]

    async def time_entries(self, *, window_start: date, window_end: date) -> list[TimeRecord]:
        return [self._time(r) for r in await self._time_rows(window_start, window_end)]

    async def _tickets_by_id(self, ids: list[int], labels: dict[str, dict[str, str]]) -> list[TicketRecord]:
        out: list[TicketRecord] = []
        for i in range(0, len(ids), ID_CHUNK):
            chunk = ids[i:i + ID_CHUNK]
            rows = await self._query("Tickets", {"MaxRecords": MAX_RECORDS, "IncludeFields": TICKET_FIELDS, "filter": [{"op": "in", "field": "id", "value": chunk}]})
            out += [self._ticket(r, labels) for r in rows]
        return out

    # ------------------------------------------------------------------ the contract

    async def fetch(self, *, window_start: date, window_end: date, cursor: str | None = None) -> AsyncIterator[FetchResult]:
        """Four canonical rows per day for [window_start, window_end), written once everything is read. There is no resume cursor, so one passed in is refused."""
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        if cursor:
            raise ValueError("the Autotask connector issues no cursor, so none can be resumed")
        self._spent = [0, 0]
        as_of = self._clock()
        if as_of.tzinfo is None:
            raise ValueError("the clock must give a timezone aware time")
        await self._check_headroom()
        labels = await self._labels()
        primary = [self._ticket(r, labels) for r in await self._ticket_rows(window_start, window_end)]
        times = [self._time(r) for r in await self._time_rows(window_start, window_end)]
        known = {int(t.ticket_id) for t in primary}
        missing = sorted({int(e.ticket_id) for e in times if e.ticket_id is not None} - known)
        extra = await self._tickets_by_id(missing, labels) if missing else []
        self.tickets_read, self.time_read = primary + extra, times
        opened: dict[date, int] = {}
        closed: dict[date, int] = {}
        missed: dict[date, int] = {}
        seconds: dict[date, int] = {}
        for t in primary:
            opened[utc_day(t.opened_at)] = opened.get(utc_day(t.opened_at), 0) + 1
            if t.closed_at is not None:
                closed[utc_day(t.closed_at)] = closed.get(utc_day(t.closed_at), 0) + 1
            due = t.resolution_due_at
            if due is not None:
                done = t.finished_at
                if (done is not None and done > due) or (done is None and as_of > due):
                    missed[utc_day(due)] = missed.get(utc_day(due), 0) + 1
        for e in times:
            seconds[utc_day(e.worked_on)] = seconds.get(utc_day(e.worked_on), 0) + e.seconds
        raw = {"source": "Tickets and TimeEntries", "user": self.username}
        points = []
        for day in days_of(window_start, window_end):
            points.append(CanonicalPoint("at_tickets_opened", day, opened.get(day, 0), None, raw))
            points.append(CanonicalPoint("at_tickets_closed", day, closed.get(day, 0), None, raw))
            points.append(CanonicalPoint("at_resolution_goals_missed", day, missed.get(day, 0), None, raw))
            points.append(CanonicalPoint("at_time_seconds", day, seconds.get(day, 0), None, raw))
        yield FetchResult(points=points, cursor=None, api_calls=self._spent[0], throttle_waits=self._spent[1])
