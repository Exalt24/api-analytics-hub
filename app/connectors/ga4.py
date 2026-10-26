"""Google Analytics 4 Data API connector: daily website figures per property.

The second connector behind the contract in `base.py`, and the one that checks the claim
that adding a platform touches nothing else: it is a class, a registry entry and one
migration that adds four metric keys. The sync runner, the snapshot table and its row level
security are the ones Shopify already uses.

WHAT IT CALLS. `POST {base}/v1beta/properties/{id}:runReport` with one date range, the
`date` dimension and four metrics, paged with `limit` and `offset`. Every fact about that
call, with the page it came from and what is not confirmed, is in docs/GA4_SOURCES.md. It has
been run only against the stand-in server in `ga4_fake.py`, never against a live property.

WHAT A DAY IS. GA4 leaves out a row whose metrics are all zero unless `keepEmptyRows` is
true, and this connector does not set it. So, exactly as with Shopify orders, no row for a
day means either "a sync looked and the property reported nothing" or "nobody looked", and
the report tells the two apart from the sync runs, never from the absence of a row.

WHY THE RETRIES ARE BOUNDED. The pages say 500 and 503 count against a server error quota of
10 per hour for the project and property pair, and that backoff must have a retry limit. A
loop that retries a broken property forever spends that quota and then blocks every request.
So a call is tried `max_attempts` times with an exponential, jittered wait, a wait the server
asks for that is longer than `backoff_cap` is not slept but handed back as `RateLimited` for
the runner to own, and after the last attempt the failure is raised as `RateLimited`.

THE TOKEN. Taken as an already issued OAuth 2.0 access token (from the environment by
`from_env`, or from a callable that returns the current one), held in a `Secret` and sent
only in the Authorization header. Exchanging a service account key for a token is not built.
"""
from __future__ import annotations

import asyncio
import os
import random
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

import httpx

from app.crypto import Secret

from .base import (
    AuthExpired,
    AuthRevoked,
    CanonicalPoint,
    Connector,
    FetchResult,
    RateLimited,
    UpstreamBroken,
)

BASE_URL = "https://analyticsdata.googleapis.com"
#: Pinned in the URL. `v1beta` is the version every runReport page names.
API_VERSION = "v1beta"
SCOPE = "https://www.googleapis.com/auth/analytics.readonly"

#: (GA4 metric name, canonical key). `keyEvents` is the current name; the schema page lists no
#: `conversions` metric, and the stand-in refuses it the way a 400 would.
METRICS = (
    ("sessions", "sessions"),
    ("totalUsers", "users"),
    ("screenPageViews", "page_views"),
    ("keyEvents", "key_events"),
)

#: The documented ceiling on rows per request is 250,000 and the default 10,000. One row per
#: day means a window never reaches either, so paging is exercised in tests with a small page.
DEFAULT_PAGE_SIZE = 10_000
MAX_PAGE_SIZE = 250_000

#: Stop before a further page when the property reports this few tokens left in the hour,
#: because the pages say any exhausted bucket answers every request with 429.
QUOTA_FLOOR = 100

_DATE_RE = re.compile(r"^\d{8}$")
_PROPERTY_RE = re.compile(r"^(?:properties/)?(\d{1,20})$")


def settled_window_end(now: datetime) -> date:
    """The half-open end of a window whose every day is over in every time zone, for a timezone-aware `now`.

    GA4 cuts a day in the property's own time zone, and the zone furthest behind UTC is UTC-12. A day is
    finished everywhere only once it is finished there, so the end is the date twelve hours before `now` in UTC:
    a sync at 00:30 UTC on Nov 1 stops before Oct 31, which is still an evening in New York.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return (now - timedelta(hours=12)).astimezone(timezone.utc).date()


def _error_text(response: httpx.Response) -> str:
    """`STATUS: message` from Google's error object, cut short, or just the HTTP status."""
    try:
        body = response.json()
    except ValueError:
        body = None
    # Google sometimes wraps the error object in a list, and an OAuth refusal sends `error` as a plain string.
    if isinstance(body, list) and body:
        body = body[0]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, str):
        err = {"status": err[:60], "message": body.get("error_description")}
    if not isinstance(err, dict):
        err = {}
    status, message = err.get("status"), err.get("message")
    if status or message:
        return ("%s: %s" % (status or response.status_code, str(message or ""))).strip()[:200]
    return "HTTP %d" % response.status_code


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds from a numeric Retry-After. Nothing on the pages says GA4 sends one (UNVERIFIED), so absent is normal."""
    raw = response.headers.get("Retry-After")
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and value >= 0 else None


def _whole_number(value: Any, what: str) -> int:
    """A metric value arrives as a string. A count must be a whole non-negative number, never a float."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise UpstreamBroken("%s is %s, not a number string" % (what, type(value).__name__))
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise UpstreamBroken("%s is not a number" % what) from None
    if not number.is_finite() or number != number.to_integral_value() or number < 0:
        raise UpstreamBroken("%s must be a whole number of at least 0, got %s" % (what, value))
    return int(number)


class GA4Connector(Connector):
    platform = "ga4"
    supported_metrics = frozenset(canonical for _, canonical in METRICS)

    def __init__(
        self,
        *,
        property_id: str,
        access_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        quota_floor: int = QUOTA_FLOOR,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ):
        match = _PROPERTY_RE.match(str(property_id).strip())
        if not match:
            # Before any network call: the pages say the id is numeric, and a wrong one
            # would otherwise come back as a 403 that reads like a permissions problem.
            raise ValueError("property_id must be the numeric GA4 property id, got %r" % (property_id,))
        if (access_token is None) == (token_provider is None):
            raise ValueError("give exactly one of access_token and token_provider")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError("page_size must be 1 to %d" % MAX_PAGE_SIZE)
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.property_id = match.group(1)
        self._token = Secret(access_token) if access_token is not None else None
        self._token_provider = token_provider
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._page_size = page_size
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._quota_floor = quota_floor
        self._sleep = sleep
        self._rng = rng
        #: The property's time zone from the last response metadata, for the report's footnote.
        self.time_zone: str | None = None
        #: Tokens left in the hour as of the last response that carried propertyQuota.
        self.tokens_per_hour_remaining: int | None = None
        self.degraded_metrics: set[str] = set()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "GA4Connector":
        """Build from GA4_PROPERTY_ID and GA4_ACCESS_TOKEN (and GA4_API_BASE_URL for a stand-in)."""
        env = os.environ if env is None else env
        missing = [name for name in ("GA4_PROPERTY_ID", "GA4_ACCESS_TOKEN") if not env.get(name)]
        if missing:
            raise ValueError("set %s in the environment" % " and ".join(missing))
        base = env.get("GA4_API_BASE_URL")
        if base:
            kwargs.setdefault("base_url", base)
        return cls(property_id=env["GA4_PROPERTY_ID"], access_token=env["GA4_ACCESS_TOKEN"], **kwargs)

    def __repr__(self) -> str:
        return "GA4Connector(property_id=%r, base_url=%r)" % (self.property_id, self._base)

    @property
    def external_account(self) -> str:
        """The property as a connection stores it, so a sync can check it is the one the connection holds."""
        return self.property_id

    async def aclose(self) -> None:
        """Close the HTTP client this connector made. A client handed in is the caller's to close."""
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ transport

    @property
    def _endpoint(self) -> str:
        return "%s/%s/properties/%s:runReport" % (self._base, API_VERSION, self.property_id)

    def _bearer(self) -> str:
        token = self._token.reveal() if self._token is not None else self._token_provider()
        if not token or not isinstance(token, str):
            raise AuthExpired("no access token is available")
        return token

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        """Seconds to wait before attempt `attempt + 1`: the server's ask, else exponential with jitter."""
        if retry_after is not None:
            return retry_after
        return min(self._backoff_cap, self._backoff_base * (2 ** attempt)) * (0.5 + self._rng() / 2)

    async def _post(self, body: Mapping[str, Any]) -> tuple[dict, int, int]:
        """One runReport with bounded retries. Returns (payload, http attempts, waits)."""
        waits = 0
        for attempt in range(self._max_attempts):
            last = attempt == self._max_attempts - 1
            retry_after: float | None = None
            try:
                response = await self._client.post(
                    self._endpoint,
                    headers={"Authorization": "Bearer " + self._bearer(), "Content-Type": "application/json"},
                    json=body,
                )
            except httpx.HTTPError as exc:
                # The class only: a transport error can carry the URL.
                problem = "transport failure (%s)" % type(exc).__name__
            else:
                code = response.status_code
                if code == 200:
                    try:
                        payload = response.json()
                    except ValueError:
                        raise UpstreamBroken("GA4 answered 200 with a body that is not JSON") from None
                    if not isinstance(payload, dict):
                        raise UpstreamBroken("GA4 answered 200 with %s, not an object" % type(payload).__name__)
                    return payload, attempt + 1, waits
                if code == 401:
                    raise AuthExpired("the access token was rejected (%s)" % _error_text(response))
                if code == 403:
                    raise AuthRevoked("no access to property %s (%s)" % (self.property_id, _error_text(response)))
                if code == 400:
                    raise UpstreamBroken("GA4 refused the request (%s)" % _error_text(response))
                if code != 429 and code < 500:
                    raise UpstreamBroken("unexpected status %d (%s)" % (code, _error_text(response)))
                retry_after = _retry_after(response)
                problem = "%s (%s)" % ("quota or rate limit" if code == 429 else "server error %d" % code,
                                       _error_text(response))
            if last:
                raise RateLimited(retry_after, "gave up after %d attempts: %s" % (self._max_attempts, problem))
            delay = self._delay(attempt, retry_after)
            if delay > self._backoff_cap:
                # A long wait is the runner's to schedule, not a coroutine's to sleep through.
                raise RateLimited(retry_after, "asked to wait %.0f seconds: %s" % (delay, problem))
            waits += 1
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ parsing

    def _body(self, start: date, end: date, offset: int) -> dict[str, Any]:
        return {
            "dateRanges": [{"startDate": start.isoformat(), "endDate": (end - timedelta(days=1)).isoformat()}],
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": name} for name, _ in METRICS],
            "orderBys": [{"dimension": {"dimensionName": "date", "orderType": "ALPHANUMERIC"}}],
            "limit": str(self._page_size),
            "offset": str(offset),
            "returnPropertyQuota": True,
        }

    def _note_metadata(self, payload: Mapping[str, Any]) -> bool:
        """Remember the time zone and quota, and say whether the report was sampled."""
        meta = payload.get("metadata") or {}
        if isinstance(meta, dict) and isinstance(meta.get("timeZone"), str):
            self.time_zone = meta["timeZone"]
        quota = payload.get("propertyQuota") or {}
        try:
            self.tokens_per_hour_remaining = int(quota["tokensPerHour"]["remaining"])
        except (KeyError, TypeError, ValueError):
            pass
        return bool(isinstance(meta, dict) and meta.get("samplingMetadatas"))

    def _points(self, payload: Mapping[str, Any], start: date, end: date, seen: set[date],
                sampled: bool) -> list[CanonicalPoint]:
        dims = [h.get("name") for h in payload.get("dimensionHeaders") or [] if isinstance(h, dict)]
        if dims != ["date"]:
            raise UpstreamBroken("expected one dimension column named date, got %s" % dims)
        names = [h.get("name") for h in payload.get("metricHeaders") or [] if isinstance(h, dict)]
        wanted = [name for name, _ in METRICS]
        missing = [name for name in wanted if name not in names]
        if missing:
            raise UpstreamBroken("the response has no column for %s" % ", ".join(missing))
        column = {name: names.index(name) for name in wanted}
        rows = payload.get("rows") or []
        if not isinstance(rows, list):
            raise UpstreamBroken("rows is not a list")
        raw = {"source": "runReport", **({"sampled": True} if sampled else {})}
        points: list[CanonicalPoint] = []
        for row in rows:
            try:
                text = row["dimensionValues"][0]["value"]
                values = [mv["value"] for mv in row["metricValues"]]
            except (KeyError, IndexError, TypeError):
                raise UpstreamBroken("a row is missing its dimensionValues or metricValues") from None
            if not isinstance(text, str) or not _DATE_RE.match(text):
                raise UpstreamBroken("a row's date is %r, not YYYYMMDD" % (text,))
            try:
                day = datetime.strptime(text, "%Y%m%d").date()
            except ValueError:
                raise UpstreamBroken("a row's date %s is not a calendar day" % text) from None
            if not start <= day < end:
                raise UpstreamBroken("a row for %s is outside the window %s to %s" % (day, start, end))
            if day in seen:
                # Two rows for one day would be written as one, silently keeping either.
                raise UpstreamBroken("two rows for %s" % day)
            if len(values) != len(names):
                raise UpstreamBroken("a row has %d metric values for %d columns" % (len(values), len(names)))
            seen.add(day)
            for name, canonical in METRICS:
                points.append(CanonicalPoint(
                    canonical, day, _whole_number(values[column[name]], "%s on %s" % (name, day)), None, raw))
        return points

    # ------------------------------------------------------------------ public

    async def verify(self) -> str:
        """One row of one metric for yesterday: the cheapest authenticated call the pages describe."""
        body = {
            "dateRanges": [{"startDate": "yesterday", "endDate": "yesterday"}],
            "metrics": [{"name": "sessions"}],
            "limit": "1",
            "returnPropertyQuota": True,
        }
        payload, _, _ = await self._post(body)
        self._note_metadata(payload)
        return "GA4 property %s%s" % (self.property_id, " (time zone %s)" % self.time_zone if self.time_zone else "")

    async def fetch(
        self,
        *,
        window_start: date,
        window_end: date,
        cursor: str | None = None,
    ) -> AsyncIterator[FetchResult]:
        """One canonical row per day and metric for [window_start, window_end), page by page.

        The window is half open everywhere in this system and GA4's `endDate` is inclusive, so
        the end is moved back one day. The cursor is the offset of the next page.
        """
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        try:
            offset = int(cursor) if cursor else 0
            if offset < 0:
                raise ValueError
        except ValueError:
            raise UpstreamBroken("the resume cursor %r is not a row offset" % (cursor,)) from None
        seen: set[date] = set()
        while True:
            if offset and self.tokens_per_hour_remaining is not None and self.tokens_per_hour_remaining <= self._quota_floor:
                raise RateLimited(None, "%d tokens are left this hour, stopping before page at offset %d"
                                  % (self.tokens_per_hour_remaining, offset))
            payload, calls, waits = await self._post(self._body(window_start, window_end, offset))
            sampled = self._note_metadata(payload)
            rows = payload.get("rows") or []
            total = payload.get("rowCount")
            if total is None and not rows:
                total = 0  # proto3 JSON drops zero-valued fields, so a report with no rows may carry no rowCount at all
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise UpstreamBroken("rowCount is %r, not a number of rows" % (total,))
            points = self._points(payload, window_start, window_end, seen, sampled)
            offset += len(rows)
            if offset < total and not rows:
                raise UpstreamBroken("rowCount is %d but the page at offset %d came back empty" % (total, offset))
            done = offset >= total
            yield FetchResult(points=points, cursor=None if done else str(offset),
                              api_calls=calls, throttle_waits=waits)
            if done:
                return
