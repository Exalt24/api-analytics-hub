"""ConnectWise Manage (PSA) connector: tickets and technician time, as daily figures and as ticket records for the MSP backlog analysis.

The fourteenth connector behind the contract in `base.py`, and the first for a professional services automation (PSA) system. It reads the Manage REST API's service tickets
and time entries and writes four daily keys:

    cw_tickets_opened             tickets created that day (the UTC day of `_info.dateEntered`)
    cw_tickets_closed             tickets closed that day (the UTC day of `closedDate`)
    cw_resolution_goals_missed    tickets whose resolution goal (`resolutionGoalUTC`) fell on that day and were resolved after it, or are still unresolved now
    cw_time_seconds               technician time worked that day, in whole seconds (`actualHours` x 3600, on the UTC day of `timeStart`)

`tickets()` and `time_entries()` return the same data as `TicketRecord` and `TimeRecord` (`app/msp/records.py`) for `app/msp/analysis.py`, which ranks the ticket drivers, the SLA
risk and the touch time per category.

WRITTEN FROM THIRD PARTY DESCRIPTIONS, AND TESTED ONLY AGAINST A STAND-IN. ConnectWise's developer documentation (developer.connectwise.com) needs a login, and its
registration refuses a free mailbox address, so no ConnectWise page could be saved (docs/vendor-contract/connectwise/connectwise-developer-portal-not-retrieved.txt). What the
code rests on instead is the Manage OpenAPI file as kept in an open source client library (field names and parameters) and one blog post that shows real headers (the `clientId`
header, the `Link` header, forward-only paging, `_info`). Both are in docs/vendor-contract/connectwise/ and are not ConnectWise's own pages. Nothing here ran against a
live ConnectWise tenant: none is held. Every fact, its source and what no source states is in docs/MSP_SOURCES.md (section ConnectWise).

WHAT IT CALLS. `GET {api_url}/service/tickets`, `GET {api_url}/time/entries`, `GET {api_url}/system/info` (the cheapest call, used by `verify()`) and, only when
`refresh_open_sla` is on, `GET {api_url}/service/tickets/{id}`. `api_url` is the address up to and including `/apis/3.0` (the OpenAPI file's server is
`.../v4_6_release/apis/3.0`; the cloud host depends on the tenant's region and is the operator's to give). Every request carries `Authorization: Basic base64(company+publicKey:privateKey)`
and `clientId` (the OpenAPI file marks `clientId` as a required header; the blog shows the Basic string; the exact `company+publicKey:privateKey` shape is the blog's and the
other third party sources agree). Paging is forward-only: the header `pagination-type: forward-only`, `pageSize=1000` ("The max is 1,000"), and the next page is the URL in the
`Link: <...>; rel="next"` header exactly as given; it is never followed unless it is on the same host under the same API path, so the keys cannot be sent elsewhere.

WHAT IT DOES NOT DO, because no saved page says how. It sends no `conditions`: the condition grammar (how a date is written) is on no saved page, so tickets and time are read in
full and cut to the window here, and an operator who knows the grammar can pass `ticket_conditions` and `time_conditions` through unchanged. It sends no `fields` list for the same
reason. It makes no assumption about a rate limit: none of the saved pages states one (the third party figures disagree), so it paces nothing, retries a 429 (waiting for the
`Retry-After` header when there is one, else backing off, and handing a wait longer than the retrier's cap to the runner) and a 500, 502, 503 or 504 with a bound.

SLA FIGURES. The OpenAPI file says of `resolutionGoalUTC`, `respondByGoalUTC` and the minutes fields: "To obtain the current SLA times for an active ticket, please use the
/service/tickets/calculateSLA endpoint for multiple tickets or /service/tickets/{id} for individual tickets." The list is therefore used as it is for tickets that are finished, and
for open tickets `refresh_open_sla=True` re-reads each one by id (one more request each). `calculateSLA` is not in the OpenAPI file's paths, so it is not called.

A ticket that is closed must carry a `closedDate`, or the answer is refused. The token never appears in an error message: only the HTTP status is quoted.
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Callable, Mapping
from urllib.parse import urlparse

import httpx

from app.crypto import Secret
from app.msp.records import RecordError, TicketRecord, TimeRecord

from .base import AuthRevoked, CanonicalPoint, Connector, FetchResult, UpstreamBroken
from .common import Retrier, Retry, retry_after
from .msp_common import checked_base, days_of, decimal_json, parse_instant, seconds_from_hours, utc_day

__all__ = ["ConnectWiseConnector", "METRICS", "SUPPORTED", "PAGE_SIZE"]

METRICS = ("cw_tickets_opened", "cw_tickets_closed", "cw_resolution_goals_missed", "cw_time_seconds")
SUPPORTED = frozenset(METRICS)
#: "PageSize: How many items are returned per page. The max is 1,000" (the blog post saved beside the OpenAPI file).
PAGE_SIZE = 1000
MAX_PAGES = 5000
NO_SUMMARY = "(no summary)"


def _is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _name(ref: Any, what: str, *, required: bool) -> str | None:
    """The `name` of a reference object (`{"id": 1, "name": "Service Board"}`)."""
    if ref is None:
        if required:
            raise UpstreamBroken("%s is missing" % what)
        return None
    if not isinstance(ref, dict) or not isinstance(ref.get("name"), str) or not ref["name"].strip():
        if required:
            raise UpstreamBroken("%s has no name" % what)
        return None
    return ref["name"].strip()


class ConnectWiseConnector(Connector):
    platform = "connectwise"
    supported_metrics = SUPPORTED

    def __init__(
        self,
        *,
        api_url: str,
        company_id: str,
        public_key: str,
        private_key: str,
        client_id: str,
        refresh_open_sla: bool = False,
        ticket_conditions: str | None = None,
        time_conditions: str | None = None,
        client: httpx.AsyncClient | None = None,
        retrier: Retrier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        **retry_options: Any,
    ):
        for name, value in (("company_id", company_id), ("public_key", public_key), ("private_key", private_key), ("client_id", client_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("%s is required" % name)
        if "+" in company_id or ":" in company_id:
            raise ValueError("company_id must not hold a + or a : (it would change what the Basic string means)")
        if ":" in public_key:
            raise ValueError("public_key must not hold a : (it would change what the Basic string means)")
        self._base = checked_base(api_url, what="api_url", allow_path=True)
        if not urlparse(self._base).path:
            raise ValueError("api_url must include the API path, such as https://na.myconnectwise.net/v4_6_release/apis/3.0")
        self._base_path = urlparse(self._base).path
        self.company_id = company_id.strip()
        self._basic = Secret(base64.b64encode(("%s+%s:%s" % (company_id.strip(), public_key, private_key)).encode("utf-8")).decode("ascii"))
        self._client_id = client_id.strip()
        self._refresh = bool(refresh_open_sla)
        self._ticket_conditions, self._time_conditions = ticket_conditions, time_conditions
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._retrier = retrier or Retrier(**retry_options)
        self._clock = clock
        self._spent = [0, 0]
        self.degraded_metrics: set[str] = set()
        #: What the last `fetch` read, for the ledger: the records themselves.
        self.tickets_read: list[TicketRecord] = []
        self.time_read: list[TimeRecord] = []

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "ConnectWiseConnector":
        """Build from CONNECTWISE_API_URL, CONNECTWISE_COMPANY_ID, CONNECTWISE_PUBLIC_KEY, CONNECTWISE_PRIVATE_KEY and CONNECTWISE_CLIENT_ID."""
        import os

        env = os.environ if env is None else env
        names = ("CONNECTWISE_API_URL", "CONNECTWISE_COMPANY_ID", "CONNECTWISE_PUBLIC_KEY", "CONNECTWISE_PRIVATE_KEY", "CONNECTWISE_CLIENT_ID")
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise ValueError("set %s in the environment" % ", ".join(missing))
        return cls(api_url=env["CONNECTWISE_API_URL"], company_id=env["CONNECTWISE_COMPANY_ID"], public_key=env["CONNECTWISE_PUBLIC_KEY"],
                   private_key=env["CONNECTWISE_PRIVATE_KEY"], client_id=env["CONNECTWISE_CLIENT_ID"], **kwargs)

    def __repr__(self) -> str:
        return "ConnectWiseConnector(company=%r)" % self.company_id

    @property
    def external_account(self) -> str:
        return self.company_id

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
            raise AuthRevoked("ConnectWise refused the company, keys or clientId (HTTP 401)")
        if http == 429:
            return Retry(retry_after(response), "HTTP 429")
        if http in (500, 502, 503, 504):
            return Retry(retry_after(response), "HTTP %d" % http)
        raise UpstreamBroken("ConnectWise refused the request (HTTP %d)" % http)

    def _checked_next(self, url: Any) -> str:
        parsed = urlparse(url) if isinstance(url, str) else None
        origin = urlparse(self._base)
        if (parsed is None or (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc) or ".." in parsed.path
                or not parsed.path.startswith(self._base_path + "/")):
            raise UpstreamBroken("the answer names a next page outside the ConnectWise API, refusing to send the keys there")
        return url

    async def _get(self, url_or_path: str, params: Mapping[str, str] | None = None, *, forward_only: bool = False) -> tuple[Any, httpx.Response]:
        url = url_or_path if url_or_path.startswith("http") else self._base + url_or_path

        async def send() -> httpx.Response:
            headers = {"Authorization": "Basic " + self._basic.reveal(), "clientId": self._client_id, "Accept": "application/json"}
            if forward_only:
                headers["pagination-type"] = "forward-only"
            return await self._client.get(url, params=params, headers=headers)

        response, calls, waits = await self._retrier.run(send, self._classify)
        self._spent[0] += calls
        self._spent[1] += waits
        try:
            return decimal_json(response.text), response
        except ValueError:
            raise UpstreamBroken("ConnectWise answered 200 with a body that is not JSON") from None

    async def _list(self, path: str, conditions: str | None) -> list[dict]:
        """Every record of a forward-only paged list. The next page is the `Link` header's `rel="next"` URL as given; a repeated URL or a repeated record is a broken answer."""
        params: dict[str, str] = {"pageSize": str(PAGE_SIZE)}
        if conditions:
            params["conditions"] = conditions
        records: list[dict] = []
        ids: set[int] = set()
        seen: set[str] = set()
        body, response = await self._get(path, params, forward_only=True)
        for _ in range(MAX_PAGES):
            if not isinstance(body, list):
                raise UpstreamBroken("the answer to %s is not a list" % path)
            for r in body:
                if not isinstance(r, dict) or not _is_id(r.get("id")):
                    raise UpstreamBroken("a record of %s has no usable id" % path)
                if r["id"] in ids:
                    raise UpstreamBroken("record %d of %s appears twice" % (r["id"], path))
                ids.add(r["id"])
                records.append(r)
            link = response.links.get("next")
            if link is None:
                return records
            nxt = self._checked_next(link.get("url"))
            if nxt in seen:
                raise UpstreamBroken("a next page names itself")
            seen.add(nxt)
            body, response = await self._get(nxt, None, forward_only=True)
        raise UpstreamBroken("more than %d pages in one list" % MAX_PAGES)

    async def verify(self) -> str:
        body, _ = await self._get("/system/info")
        if not isinstance(body, dict):
            raise UpstreamBroken("the system/info answer is not an object")
        return "ConnectWise Manage company %s" % self.company_id

    # ------------------------------------------------------------------ records

    def _ticket(self, r: dict) -> TicketRecord:
        tid = r["id"]
        info = r.get("_info")
        if not isinstance(info, dict) or "dateEntered" not in info:
            raise UpstreamBroken("ticket %d has no _info.dateEntered" % tid)
        opened = parse_instant(info["dateEntered"], "the _info.dateEntered of ticket %d" % tid, naive_is_utc=False)
        if not isinstance(r.get("closedFlag"), bool):
            raise UpstreamBroken("closedFlag of ticket %d is not true or false" % tid)
        closed = r["closedFlag"]
        closed_at = None
        if closed:
            if r.get("closedDate") is None:
                raise UpstreamBroken("ticket %d is closed and has no closedDate" % tid)
            closed_at = parse_instant(r["closedDate"], "the closedDate of ticket %d" % tid, naive_is_utc=False)

        def optional(key: str) -> datetime | None:
            return None if r.get(key) is None else parse_instant(r[key], "the %s of ticket %d" % (key, tid), naive_is_utc=False)

        summary = r.get("summary")
        if not isinstance(summary, str):
            raise UpstreamBroken("the summary of ticket %d is not text" % tid)
        try:
            return TicketRecord(
                source="connectwise", ticket_id=str(tid), title=summary.strip() or NO_SUMMARY,
                category=_name(r.get("board"), "the board of ticket %d" % tid, required=True),  # type: ignore[arg-type]
                subcategory=_name(r.get("type"), "the type of ticket %d" % tid, required=False),
                priority=_name(r.get("priority"), "the priority of ticket %d" % tid, required=False),
                status=_name(r.get("status"), "the status of ticket %d" % tid, required=True),  # type: ignore[arg-type]
                company=_name(r.get("company"), "the company of ticket %d" % tid, required=False),
                opened_at=opened, closed=closed, closed_at=closed_at, resolved_at=optional("dateResolved"),
                first_response_at=optional("dateResponded"), first_response_due_at=optional("respondByGoalUTC"),
                resolution_due_at=optional("resolutionGoalUTC"))
        except RecordError as exc:
            raise UpstreamBroken(str(exc)) from None

    def _time(self, r: dict) -> TimeRecord:
        eid = r["id"]
        start = parse_instant(r.get("timeStart"), "the timeStart of time entry %d" % eid, naive_is_utc=False)
        seconds = seconds_from_hours(r.get("actualHours"), "the actualHours of time entry %d" % eid)
        member = r.get("member")
        who = _name(member, "the member of time entry %d" % eid, required=False)
        if who is None and isinstance(member, dict) and isinstance(member.get("identifier"), str) and member["identifier"].strip():
            who = member["identifier"].strip()
        if who is None:
            raise UpstreamBroken("time entry %d names no member" % eid)
        ticket = r.get("ticket")
        ticket_id = str(ticket["id"]) if isinstance(ticket, dict) and _is_id(ticket.get("id")) else None
        try:
            return TimeRecord(source="connectwise", entry_id=str(eid), ticket_id=ticket_id, technician=who, worked_on=start, seconds=seconds)
        except RecordError as exc:
            raise UpstreamBroken(str(exc)) from None

    async def tickets(self) -> list[TicketRecord]:
        """Every ticket the key may read (or every ticket `ticket_conditions` matches), as records. With `refresh_open_sla`, each open ticket is re-read by id for its current SLA times."""
        out = [self._ticket(r) for r in await self._list("/service/tickets", self._ticket_conditions)]
        if self._refresh:
            for i, t in enumerate(out):
                if t.is_open:
                    body, _ = await self._get("/service/tickets/%s" % t.ticket_id)
                    if not isinstance(body, dict) or body.get("id") != int(t.ticket_id):
                        raise UpstreamBroken("the answer for ticket %s is not that ticket" % t.ticket_id)
                    out[i] = self._ticket(body)
        return out

    async def time_entries(self) -> list[TimeRecord]:
        return [self._time(r) for r in await self._list("/time/entries", self._time_conditions)]

    # ------------------------------------------------------------------ the contract

    async def fetch(self, *, window_start: date, window_end: date, cursor: str | None = None) -> AsyncIterator[FetchResult]:
        """Four canonical rows per day for [window_start, window_end), written once everything is read. There is no resume cursor, so one passed in is refused."""
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        if cursor:
            raise ValueError("the ConnectWise connector issues no cursor, so none can be resumed")
        self._spent = [0, 0]
        as_of = self._clock()
        if as_of.tzinfo is None:
            raise ValueError("the clock must give a timezone aware time")
        tickets = await self.tickets()
        times = await self.time_entries()
        self.tickets_read, self.time_read = tickets, times
        opened: dict[date, int] = {}
        closed: dict[date, int] = {}
        missed: dict[date, int] = {}
        seconds: dict[date, int] = {}
        for t in tickets:
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
        raw = {"source": "service/tickets and time/entries", "company": self.company_id}
        points = []
        for day in days_of(window_start, window_end):
            points.append(CanonicalPoint("cw_tickets_opened", day, opened.get(day, 0), None, raw))
            points.append(CanonicalPoint("cw_tickets_closed", day, closed.get(day, 0), None, raw))
            points.append(CanonicalPoint("cw_resolution_goals_missed", day, missed.get(day, 0), None, raw))
            points.append(CanonicalPoint("cw_time_seconds", day, seconds.get(day, 0), None, raw))
        yield FetchResult(points=points, cursor=None, api_calls=self._spent[0], throttle_waits=self._spent[1])
