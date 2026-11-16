"""Harvest connector: time tracked per day, billable value, and project budget checks.

The twelfth connector behind the contract in `base.py`, and the first for a time tracker. It reads the v2 REST API's time entries and writes four daily keys:

    harvest_hours              hours tracked that day, in whole seconds (`hours` x 3600)
    harvest_billable_hours     the same for entries with `billable` true
    harvest_time_entries       how many entries were logged that day
    harvest_billable_amount    `rounded_hours` x `billable_rate` of the billable entries (money, in the connection's currency)

`projects()` returns the projects with their budget in hours, and `budget_alerts()` turns those and the hours spent so far into a status per project
(under, warn, over). That is the "budget monitoring" half of a project dashboard: the figures a registry of projects needs without a person opening Harvest.

It has been run only against the stand-in server in `harvest_fake.py`, never against a live Harvest account: none is held. Every fact about the calls, with the page it
came from and what the pages do not say, is in docs/HARVEST_GRAPH_SOURCES.md (section Harvest).

WHAT IT CALLS. `GET {base}/v2/time_entries?from=..&to=..&per_page=2000` with `Authorization: Bearer`, `Harvest-Account-Id` and a `User-Agent` of the form
`name (contact)` (the pages say a missing User-Agent is a 400). `to` is inclusive on the page, so the half open window end is moved back one day. Paging follows
`links.next` exactly as given and never builds a page URL itself (the pagination page says so); a `links.next` that names another host or a path outside `/v2/` is
never followed, so the token cannot be sent elsewhere. `GET /v2/users/me` is the cheapest authenticated call and is what `verify()` uses.

LIMITS. The pages give 100 requests per 15 seconds, answer 429 with a Retry-After header, and say the throttle lifts by itself. The connector counts its own requests
and raises `RateLimited` BEFORE the 101st inside 15 seconds, leaving the wait to the runner, and treats a 429 the same way (a Retry-After longer than the retrier's cap
is handed back, never slept). 500, 502, 503 and 504 are retried with a bound.

MONEY. Amounts are computed here, not read: Harvest gives a rate and hours, not a line total. `rounded_hours` is used because the page says it is the figure "used in
summary reports and invoices"; hours and rates are JSON numbers read as exact decimals. A project's currency is read from `/v2/projects` first, and an entry on a project
in another currency than the connection's is refused, never added in. If the key may not read projects (403) the amount is left out and `degraded_metrics` says so,
while the three hour counts are still written.

A running timer's hours so far are counted; the next sync replaces them, because a day's figures are overwritten, not added to.

THE TOKEN AND THE ACCOUNT'S DATA never appear in an error message: only the HTTP status is quoted.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, AsyncIterator, Callable, Mapping
from urllib.parse import urlparse

import httpx

from app.crypto import Secret

from .base import AuthExpired, CanonicalPoint, Connector, FetchResult, ProtectedDataDenied, RateLimited, UpstreamBroken
from .common import Retrier, Retry, minor_units, retry_after, to_decimal
from .ga4 import settled_window_end  # noqa: F401 - the same window rule, re-exported for the sync script

#: The public surface: the connector, its project and alert records, and `budget_alerts`, which a dashboard route or a scheduled job calls with `projects()` and `hours_by_project()`.
__all__ = ["HarvestConnector", "HarvestProject", "BudgetAlert", "budget_alerts", "METRICS", "SUPPORTED", "settled_window_end"]

API_BASE = "https://api.harvestapp.com"
METRICS =("harvest_hours", "harvest_billable_hours", "harvest_time_entries", "harvest_billable_amount")
SUPPORTED = frozenset(METRICS)
#: "The default and maximum per_page value for API requests is 2000."
PER_PAGE = 2000
MAX_PAGES = 500
#: "The rate limit for general API requests is 100 requests per 15 seconds."
RATE_LIMIT_REQUESTS = 100
RATE_LIMIT_SECONDS = 15.0
#: No single entry can hold more than a day. The pages give no maximum, so this is the connector's own bound: a larger figure is a broken answer, not a long day.
MAX_ENTRY_HOURS = Decimal(24)
SECONDS_PER_HOUR = Decimal(3600)

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_ACCOUNT_RE = re.compile(r"^[0-9]{1,12}$")
_CONTACT_RE = re.compile(r"^(?:[^@\s()]+@[^@\s()]+\.[^@\s()]+|https?://[^\s()]+)$")


def _decimal_json(text: str) -> Any:
    return json.loads(text, parse_float=Decimal)


def _checked_base(value: str) -> str:
    parsed = urlparse(value.strip())
    loopback = parsed.hostname in ("127.0.0.1", "localhost")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("base_url must be a bare https address such as https://api.harvestapp.com, got %r" % (value,))
    if parsed.scheme != "https" and not (loopback and parsed.scheme == "http"):
        raise ValueError("base_url must use https")
    return "%s://%s" % (parsed.scheme, parsed.netloc)


def _seconds(hours: Decimal) -> int:
    return int((hours * SECONDS_PER_HOUR).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _entry_date(value: Any, what: str) -> date:
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        raise UpstreamBroken("%s is not a YYYY-MM-DD date" % what)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise UpstreamBroken("%s is not a calendar date" % what) from None


@dataclass(frozen=True)
class HarvestProject:
    """One project with the budget figures a dashboard needs. `budget_hours` is None when the project has no budget in hours."""

    id: int
    name: str
    currency: str
    is_active: bool
    budget_hours: Decimal | None
    budget_is_monthly: bool


@dataclass(frozen=True)
class BudgetAlert:
    project_id: int
    name: str
    status: str            # under | warn | over | no_hours_budget | monthly_not_checked
    hours_spent: Decimal
    budget_hours: Decimal | None
    percent_used: int | None


def budget_alerts(projects: list[HarvestProject], hours_by_project: Mapping[int, Decimal], *, warn_percent: int = 80) -> list[BudgetAlert]:
    """A status per project. `over` means the hours spent are above the budget (exactly equal is `warn`, since nothing is left but nothing is over).

    A budget that resets every month is not compared with hours counted over another span, so it is `monthly_not_checked`. A project with no hours budget is
    `no_hours_budget`, never "under". Percentages are whole numbers rounded down, in integer arithmetic.
    """
    if isinstance(warn_percent, bool) or not isinstance(warn_percent, int) or not 1 <= warn_percent <= 100:
        raise ValueError("warn_percent must be a whole number from 1 to 100")
    out: list[BudgetAlert] = []
    for p in projects:
        spent = hours_by_project.get(p.id, Decimal(0))
        if p.budget_is_monthly:
            out.append(BudgetAlert(p.id, p.name, "monthly_not_checked", spent, p.budget_hours, None))
        elif p.budget_hours is None or p.budget_hours <= 0:
            out.append(BudgetAlert(p.id, p.name, "no_hours_budget", spent, p.budget_hours, None))
        else:
            percent = int((spent * 100) // p.budget_hours)
            status = "over" if spent > p.budget_hours else "warn" if percent >= warn_percent else "under"
            out.append(BudgetAlert(p.id, p.name, status, spent, p.budget_hours, percent))
    return out


class HarvestConnector(Connector):
    platform = "harvest"
    supported_metrics = SUPPORTED

    def __init__(
        self,
        *,
        account_id: str,
        access_token: str,
        currency: str,
        contact: str,
        base_url: str = API_BASE,
        client: httpx.AsyncClient | None = None,
        retrier: Retrier | None = None,
        clock: Callable[[], float] = time.monotonic,
        **retry_options: Any,
    ):
        if not _ACCOUNT_RE.match(str(account_id)):
            raise ValueError("account_id must be the numeric Harvest account id")
        if not _CURRENCY_RE.match(str(currency)):
            raise ValueError("currency must be an ISO 4217 code such as USD")
        if not isinstance(access_token, str) or not access_token.strip():
            raise ValueError("access_token is required")
        if not isinstance(contact, str) or not _CONTACT_RE.match(contact):
            raise ValueError("contact must be an email address or a web address: Harvest asks every request to say who is calling")
        self.currency = str(currency)
        self._account = str(account_id)
        self._token = Secret(access_token)
        self._user_agent = "api-analytics-hub (%s)" % contact
        self._base = _checked_base(base_url)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._retrier = retrier or Retrier(**retry_options)
        self._clock = clock
        self._sent: list[float] = []
        self.degraded_metrics: set[str] = set()
        self._project_currency: dict[int, str] | None = None
        self._spent = [0, 0]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "HarvestConnector":
        """Build from HARVEST_ACCOUNT_ID, HARVEST_ACCESS_TOKEN, HARVEST_CURRENCY and HARVEST_CONTACT (HARVEST_BASE_URL for a stand-in)."""
        import os

        env = os.environ if env is None else env
        names = ("HARVEST_ACCOUNT_ID", "HARVEST_ACCESS_TOKEN", "HARVEST_CURRENCY", "HARVEST_CONTACT")
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise ValueError("set %s in the environment" % ", ".join(missing))
        if env.get("HARVEST_BASE_URL"):
            kwargs.setdefault("base_url", env["HARVEST_BASE_URL"])
        return cls(account_id=env["HARVEST_ACCOUNT_ID"], access_token=env["HARVEST_ACCESS_TOKEN"], currency=env["HARVEST_CURRENCY"],
                   contact=env["HARVEST_CONTACT"], **kwargs)

    def __repr__(self) -> str:
        return "HarvestConnector(account=%r)" % self._account

    @property
    def external_account(self) -> str:
        return self._account

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ transport

    def _pace(self) -> None:
        now = self._clock()
        self._sent = [t for t in self._sent if now - t < RATE_LIMIT_SECONDS]
        if len(self._sent) >= RATE_LIMIT_REQUESTS:
            raise RateLimited(RATE_LIMIT_SECONDS - (now - self._sent[0]),
                              "%d requests were sent in the last %d seconds, which is Harvest's limit" % (RATE_LIMIT_REQUESTS, int(RATE_LIMIT_SECONDS)))
        self._sent.append(now)

    @staticmethod
    def _classify(response: httpx.Response) -> Retry | None:
        http = response.status_code
        if http == 200:
            return None
        if http == 401:
            raise AuthExpired("the access token was refused (HTTP 401)")
        if http == 403:
            raise ProtectedDataDenied("this user may not read that (HTTP 403)")
        if http == 429:
            return Retry(retry_after(response), "HTTP 429")
        if http in (500, 502, 503, 504):
            return Retry(retry_after(response), "HTTP %d" % http)
        raise UpstreamBroken("Harvest refused the request (HTTP %d)" % http)

    def _checked_next(self, url: Any) -> str:
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or "%s://%s" % (parsed.scheme, parsed.netloc) != self._base or not parsed.path.startswith("/v2/") or ".." in parsed.path:
            raise UpstreamBroken("the answer names a next page outside the Harvest API, refusing to send the token there")
        return url

    async def _get(self, url_or_path: str, params: Mapping[str, str] | None = None) -> tuple[Any, int, int]:
        url = url_or_path if url_or_path.startswith("http") else self._base + url_or_path

        async def send() -> httpx.Response:
            self._pace()                               # every attempt is a request, so a retry counts against the limit too
            return await self._client.get(url, params=params, headers={
                "Authorization": "Bearer " + self._token.reveal(), "Harvest-Account-Id": self._account,
                "User-Agent": self._user_agent, "Accept": "application/json"})

        response, calls, waits = await self._retrier.run(send, self._classify)
        try:
            return _decimal_json(response.text), calls, waits
        except ValueError:
            raise UpstreamBroken("Harvest answered 200 with a body that is not JSON") from None

    async def _pages(self, path: str, params: Mapping[str, str], key: str) -> tuple[list[dict], int, int]:
        """Every record of a paged list, following `links.next` as given. The count the answer reports must equal what arrived."""
        records: list[dict] = []
        calls = waits = 0
        body, c, w = await self._get(path, params)
        calls, waits = calls + c, waits + w
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            if not isinstance(body, dict) or not isinstance(body.get(key), list):
                raise UpstreamBroken("the answer has no %s list" % key)
            records += body[key]
            links = body.get("links")
            nxt = links.get("next") if isinstance(links, dict) else None
            if nxt is None:
                break
            nxt = self._checked_next(nxt)
            if nxt in seen:
                raise UpstreamBroken("a next page names itself")
            seen.add(nxt)
            body, c, w = await self._get(nxt)
            calls, waits = calls + c, waits + w
        else:
            raise UpstreamBroken("more than %d pages in one list" % MAX_PAGES)
        total = body.get("total_entries")
        if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total != len(records)):
            raise UpstreamBroken("total_entries says %s records and %d arrived" % (total, len(records)))
        for r in records:
            if not isinstance(r, dict):
                raise UpstreamBroken("a record is %s, not an object" % type(r).__name__)
        return records, calls, waits

    async def verify(self) -> str:
        body, _, _ = await self._get("/v2/users/me")
        if not isinstance(body, dict) or isinstance(body.get("id"), bool) or not isinstance(body.get("id"), int):
            raise UpstreamBroken("the users/me answer is not a user")
        return "Harvest account %s" % self._account

    # ------------------------------------------------------------------ projects

    async def projects(self) -> list[HarvestProject]:
        """Every project (active and archived) with its currency and its budget in hours. A 403 is `ProtectedDataDenied`: only an administrator or a manager may read them."""
        rows, c, w = await self._pages("/v2/projects", {"per_page": str(PER_PAGE)}, "projects")
        self._spent[0] += c
        self._spent[1] += w
        out: list[HarvestProject] = []
        ids: set[int] = set()
        for r in rows:
            ident = r.get("id")
            if isinstance(ident, bool) or not isinstance(ident, int) or ident in ids:
                raise UpstreamBroken("a project has no usable id, or appears twice")
            ids.add(ident)
            currency = r.get("currency")
            if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
                raise UpstreamBroken("project %d has no ISO currency" % ident)
            name = r.get("name")
            if not isinstance(name, str) or not name:
                raise UpstreamBroken("project %d has no name" % ident)
            budget = r.get("budget")
            budget_hours = None if budget is None else to_decimal(budget, "the budget of project %d" % ident)
            if budget_hours is not None and budget_hours < 0:
                raise UpstreamBroken("the budget of project %d is below 0" % ident)
            for flag in ("is_active", "budget_is_monthly"):
                if not isinstance(r.get(flag), bool):
                    raise UpstreamBroken("%s of project %d is not true or false" % (flag, ident))
            out.append(HarvestProject(ident, name, currency, r["is_active"], budget_hours, r["budget_is_monthly"]))
        self._project_currency = {p.id: p.currency for p in out}
        return out

    # ------------------------------------------------------------------ time entries

    async def _entries(self, start: date, end: date) -> list[dict]:
        rows, c, w = await self._pages("/v2/time_entries", {"from": start.isoformat(), "to": (end - timedelta(days=1)).isoformat(), "per_page": str(PER_PAGE)}, "time_entries")
        self._spent[0] += c
        self._spent[1] += w
        ids: set[int] = set()
        for r in rows:
            ident = r.get("id")
            if isinstance(ident, bool) or not isinstance(ident, int):
                raise UpstreamBroken("a time entry has no usable id")
            if ident in ids:
                raise UpstreamBroken("time entry %d appears twice" % ident)
            ids.add(ident)
            day = _entry_date(r.get("spent_date"), "the spent_date of time entry %d" % ident)
            if not start <= day < end:
                raise UpstreamBroken("time entry %d is outside the window" % ident)
            hours = to_decimal(r.get("hours"), "the hours of time entry %d" % ident)
            if hours < 0 or hours > MAX_ENTRY_HOURS:
                raise UpstreamBroken("the hours of time entry %d are outside 0 to 24" % ident)
            if not isinstance(r.get("billable"), bool):
                raise UpstreamBroken("billable of time entry %d is not true or false" % ident)
            project = r.get("project")
            if not isinstance(project, dict) or isinstance(project.get("id"), bool) or not isinstance(project.get("id"), int):
                raise UpstreamBroken("time entry %d names no project" % ident)
        return rows

    async def hours_by_project(self, *, window_start: date, window_end: date) -> dict[int, Decimal]:
        """Hours tracked per project over [window_start, window_end), for `budget_alerts`."""
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        totals: dict[int, Decimal] = {}
        for r in await self._entries(window_start, window_end):
            pid = r["project"]["id"]
            totals[pid] = totals.get(pid, Decimal(0)) + to_decimal(r["hours"], "hours")
        return totals

    async def fetch(self, *, window_start: date, window_end: date, cursor: str | None = None) -> AsyncIterator[FetchResult]:
        """Four canonical rows per day for [window_start, window_end), written once every page is read.

        There is no resume cursor, so a cursor passed in is refused rather than ignored. A day with nothing is written as zeros because every page was read.
        """
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        if cursor:
            raise ValueError("the Harvest connector issues no cursor, so none can be resumed")
        self._spent = [0, 0]
        self.degraded_metrics = set()
        currencies: dict[int, str] | None
        try:
            await self.projects()
            currencies = self._project_currency
        except ProtectedDataDenied:
            currencies = None
            self.degraded_metrics.add("harvest_billable_amount")
        entries = await self._entries(window_start, window_end)
        seconds: dict[date, int] = {}
        billable_seconds: dict[date, int] = {}
        counts: dict[date, int] = {}
        amounts: dict[date, int] = {}
        for r in entries:
            day = date.fromisoformat(r["spent_date"])
            hours = to_decimal(r["hours"], "hours")
            counts[day] = counts.get(day, 0) + 1
            seconds[day] = seconds.get(day, 0) + _seconds(hours)
            if r["billable"]:
                billable_seconds[day] = billable_seconds.get(day, 0) + _seconds(hours)
                rate = r.get("billable_rate")
                if currencies is not None and rate is not None:
                    pid = r["project"]["id"]
                    if pid not in currencies:
                        raise UpstreamBroken("time entry %d names project %d, which the project list does not hold" % (r["id"], pid))
                    if currencies[pid] != self.currency:
                        raise UpstreamBroken("time entry %d is on a project in %s and this connection sums %s: refusing to add across currencies"
                                             % (r["id"], currencies[pid], self.currency))
                    value = to_decimal(r.get("rounded_hours"), "the rounded_hours of time entry %d" % r["id"]) * to_decimal(rate, "the billable_rate of time entry %d" % r["id"])
                    amounts[day] = amounts.get(day, 0) + minor_units(value, self.currency, "the amount of time entry %d" % r["id"])
        raw = {"source": "time_entries", "account": self._account}
        points: list[CanonicalPoint] = []
        day = window_start
        while day < window_end:
            points.append(CanonicalPoint("harvest_hours", day, seconds.get(day, 0), None, raw))
            points.append(CanonicalPoint("harvest_billable_hours", day, billable_seconds.get(day, 0), None, raw))
            points.append(CanonicalPoint("harvest_time_entries", day, counts.get(day, 0), None, raw))
            if currencies is not None:
                points.append(CanonicalPoint("harvest_billable_amount", day, amounts.get(day, 0), self.currency, raw))
            day += timedelta(days=1)
        yield FetchResult(points=points, cursor=None, api_calls=self._spent[0], throttle_waits=self._spent[1])
