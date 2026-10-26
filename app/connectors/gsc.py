"""Google Search Console connector: daily search figures per property, and the top queries and pages of each day.

The fourth connector behind the contract in `base.py`: a class, a registry entry and one migration that adds
three metric keys and the table the per-query and per-page rows go into. Every fact about the call, with the
page it came from and what is not confirmed, is in docs/GSC_SOURCES.md. It has been run only against the
stand-in server in `gsc_fake.py`, never against a live property: no Search Console property is held.

WHAT IT CALLS. `POST {base}/webmasters/v3/sites/{siteUrl}/searchAnalytics/query`, the property URL
percent-encoded into the path in full (`sc-domain:example.com` and `https://www.example.com/` both hold
characters that are not path-safe). One query groups by `date` over the whole window, one row a day, paged
with `startRow` until a page comes back empty, which is how the how-to page says to read it. Then, for each day
that has a row, one query grouped by `query` and one by `page` for that single day, with `rowLimit` set to the
number of top rows wanted. A day is the unit because the page says so ("run a query each day for one day's
worth of data") and because `rowLimit` cuts the whole answer, not each day of it.

WHAT IS AND IS NOT A DAY. A day with no data has no row ("any days without data are omitted"), so, as with
Shopify orders and GA4, no row means "a sync looked and found nothing" or "nobody looked", and the report
tells the two apart from the sync runs. And the newest days are not final: the request asks for
`dataState: all`, which "will include fresh data", and the response's `metadata.first_incomplete_date` says
where the data stops being final. Rows from that day on are stored marked `provisional`; the next sync
rewrites them with the settled figures. Dates are Pacific time, which the page states.

WHAT IS NOT PROMISED. The page says the API "does not guarantee to return all data rows but rather top ones",
and that grouping by page or query "may drop some data". The daily totals here group by date only, which the
how-to calls the accurate form; the top rows are what they say they are, the top few, never a total.

CLICKS ARE NOT ALWAYS NUMBERS YOU CAN TRUST BLINDLY. The page types `clicks`, `impressions` and `position` as
doubles. A value that is an integer, an integral float or a numeric string is read; a fractional count, a
negative, NaN, or more clicks than impressions is a broken response, not a figure.

THE TOKEN. An already issued OAuth 2.0 access token (the pages name the `webmasters.readonly` scope), held in
a `Secret` and sent only in the Authorization header (UNVERIFIED on the pages read, as for GA4). Getting or
refreshing one is not built.
"""
from __future__ import annotations

import asyncio
import os
import random
import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping
from urllib.parse import quote

import httpx

from app.crypto import Secret

from .base import (
    AuthExpired,
    AuthRevoked,
    CanonicalPoint,
    Connector,
    DimensionRow,
    FetchResult,
    RateLimited,
    UpstreamBroken,
)
from .ga4 import settled_window_end  # noqa: F401 - the same window rule, re-exported for the sync script

BASE_URL = "https://www.googleapis.com"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"

#: The page's own maximum for `rowLimit`; the default there is 1,000.
MAX_ROW_LIMIT = 25_000
#: Top rows kept for each day and dimension.
DEFAULT_TOP_N = 10
DIMENSIONS = ("query", "page")

SUPPORTED = frozenset({"search_clicks", "search_impressions", "search_position"})

#: The largest value the snapshot table's bigint column holds. A response past it is broken, not a figure.
BIGINT_MAX = 9_223_372_036_854_775_807

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SITE_RE = re.compile(r"^(?:sc-domain:[A-Za-z0-9][A-Za-z0-9.-]*|https?://[^\s/?#]+(?:/[^\s?#]*)?)$")

#: 403 reasons that mean "slow down", not "you may not read this". From the errors page.
_THROTTLE_REASONS = frozenset({
    "rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded", "limitExceeded",
    "servingLimitExceeded", "concurrentLimitExceeded", "variableTermLimitExceeded",
    "rateLimitExceededUnreg", "userRateLimitExceededUnreg", "dailyLimitExceededUnreg",
})


class _GoogleError:
    """A Google error object read into the fields the connector acts on."""

    def __init__(self, code: int | None, reason: str | None, status: str | None, message: str, http: int):
        self.code, self.reason, self.status, self.message, self.http = code, reason, status, message, http

    @property
    def effective(self) -> int:
        return self.code if self.code is not None else self.http

    def throttled(self) -> bool:
        return self.effective == 429 or self.reason in _THROTTLE_REASONS or self.status == "RESOURCE_EXHAUSTED"

    def text(self) -> str:
        head = self.reason or self.status or ("code %s" % self.code if self.code is not None else "HTTP %d" % self.http)
        return ("%s: %s" % (head, self.message)).strip().rstrip(":")[:200]


def _read_error(body: Any, http: int) -> _GoogleError | None:
    """The error object of a body, or None when it has none. Tolerates a list and an OAuth style string error."""
    if isinstance(body, list) and body:
        body = body[0]
    if not isinstance(body, dict) or "error" not in body:
        return None
    err = body["error"]
    if isinstance(err, str):
        return _GoogleError(None, err[:60], None, str(body.get("error_description") or "")[:150], http)
    if not isinstance(err, dict):
        return _GoogleError(None, None, None, str(err)[:120], http)
    code = err.get("code")
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    reason = None
    items = err.get("errors")
    if isinstance(items, list) and items and isinstance(items[0], dict) and isinstance(items[0].get("reason"), str):
        reason = items[0]["reason"][:60]
    status = err.get("status") if isinstance(err.get("status"), str) else None
    return _GoogleError(code, reason, status, str(err.get("message") or "")[:150], http)


def _count(value: Any, what: str) -> int:
    """A count the page types as a double. Whole and at least 0, whether it arrives as 12, 12.0 or "12"."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise UpstreamBroken("%s is %s, not a number" % (what, type(value).__name__))
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise UpstreamBroken("%s is not a number" % what) from None
    if not number.is_finite() or number != number.to_integral_value() or number < 0:
        raise UpstreamBroken("%s must be a whole number of at least 0, got %s" % (what, value))
    if number > BIGINT_MAX:
        raise UpstreamBroken("%s is larger than any count this hub stores" % what)
    return int(number)


def _position_milli(value: Any, what: str) -> int:
    """An average position as thousandths, rounded half up. Never negative, never NaN."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise UpstreamBroken("%s is %s, not a number" % (what, type(value).__name__))
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise UpstreamBroken("%s is not a number" % what) from None
    if not number.is_finite() or number < 0:
        raise UpstreamBroken("%s must be at least 0, got %s" % (what, value))
    if number > 1_000_000:
        raise UpstreamBroken("%s is not a plausible position, got %s" % (what, value))
    return int((number * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def normalise_site(site_url: str) -> str:
    """A Domain property or a URL-prefix property, with a bare origin given its trailing slash.

    The page's examples are `sc-domain:example.com` and `http://www.example.com/`; a URL-prefix property is
    stored with its slash, so `https://www.example.com` would be read as a different (unknown) property.
    """
    text = str(site_url).strip()
    if not _SITE_RE.match(text):
        raise ValueError("site_url must be sc-domain:<domain> or a http(s) URL, got %r" % (site_url,))
    if text.startswith("sc-domain:"):
        return text
    return text + "/" if re.fullmatch(r"https?://[^/]+", text) else text


class GSCConnector(Connector):
    platform = "gsc"
    supported_metrics = SUPPORTED

    def __init__(
        self,
        *,
        site_url: str,
        access_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        page_size: int = MAX_ROW_LIMIT,
        top_n: int = DEFAULT_TOP_N,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.site_url = normalise_site(site_url)
        if (access_token is None) == (token_provider is None):
            raise ValueError("give exactly one of access_token and token_provider")
        if not 1 <= page_size <= MAX_ROW_LIMIT:
            raise ValueError("page_size must be 1 to %d" % MAX_ROW_LIMIT)
        if not 0 <= top_n <= MAX_ROW_LIMIT:
            raise ValueError("top_n must be 0 to %d" % MAX_ROW_LIMIT)
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._token = Secret(access_token) if access_token is not None else None
        self._token_provider = token_provider
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._page_size = page_size
        self._top_n = top_n
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._rng = rng
        self._clock = clock
        #: The first date Search Console says is still incomplete, from the last date query (None when none is).
        self.first_incomplete_date: date | None = None
        #: The newest day `verify()` saw data for.
        self.latest_data: date | None = None
        self.degraded_metrics: set[str] = set()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "GSCConnector":
        """Build from GSC_SITE_URL and GSC_ACCESS_TOKEN (and GSC_API_BASE_URL for a stand-in)."""
        env = os.environ if env is None else env
        missing = [name for name in ("GSC_SITE_URL", "GSC_ACCESS_TOKEN") if not env.get(name)]
        if missing:
            raise ValueError("set %s in the environment" % " and ".join(missing))
        base = env.get("GSC_API_BASE_URL")
        if base:
            kwargs.setdefault("base_url", base)
        return cls(site_url=env["GSC_SITE_URL"], access_token=env["GSC_ACCESS_TOKEN"], **kwargs)

    def __repr__(self) -> str:
        return "GSCConnector(site_url=%r, base_url=%r)" % (self.site_url, self._base)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ transport

    @property
    def _endpoint(self) -> str:
        return "%s/webmasters/v3/sites/%s/searchAnalytics/query" % (self._base, quote(self.site_url, safe=""))

    def _bearer(self) -> str:
        token = self._token.reveal() if self._token is not None else self._token_provider()
        if not token or not isinstance(token, str):
            raise AuthExpired("no access token is available")
        return token

    def _delay(self, attempt: int, wait: float | None) -> float:
        if wait is not None:
            return wait
        return min(self._backoff_cap, self._backoff_base * (2 ** attempt)) * (0.5 + self._rng() / 2)

    async def _post(self, body: Mapping[str, Any]) -> tuple[dict, int, int]:
        """One query with bounded retries. Returns (payload, http attempts, waits).

        An error object is read whatever the HTTP status: Google can put one in a 200, and a 403 may mean
        "no access" or "slow down", which only its `reason` tells apart.
        """
        waits = 0
        for attempt in range(self._max_attempts):
            last = attempt == self._max_attempts - 1
            wait: float | None = None
            try:
                response = await self._client.post(
                    self._endpoint,
                    headers={"Authorization": "Bearer " + self._bearer(), "Content-Type": "application/json"},
                    json=body,
                )
            except httpx.HTTPError as exc:
                problem = "transport failure (%s)" % type(exc).__name__
            else:
                try:
                    payload: Any = response.json()
                except ValueError:
                    payload = None
                err = _read_error(payload, response.status_code)
                if err is None and response.status_code == 200:
                    if not isinstance(payload, dict):
                        raise UpstreamBroken("Search Console answered 200 with %s, not an object" % (
                            "a body that is not JSON" if payload is None else type(payload).__name__))
                    return payload, attempt + 1, waits
                if err is None:
                    err = _GoogleError(None, None, None, "", response.status_code)
                if err.throttled() or err.effective >= 500:
                    wait = self._retry_after(response)
                    problem = "%s (%s)" % ("quota or rate limit" if err.throttled() else "server error %d" % err.effective,
                                           err.text())
                else:
                    self._raise(err)
                    raise AssertionError("unreachable")  # pragma: no cover
            if last:
                raise RateLimited(wait, "gave up after %d attempts: %s" % (self._max_attempts, problem))
            delay = self._delay(attempt, wait)
            if delay > self._backoff_cap:
                raise RateLimited(wait, "asked to wait %.0f seconds: %s" % (delay, problem))
            waits += 1
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        try:
            value = float(raw) if raw is not None else None
        except ValueError:
            return None
        return value if value is not None and value >= 0 else None

    def _raise(self, err: _GoogleError) -> None:
        code = err.effective
        if code == 401:
            raise AuthExpired("the access token was rejected (%s)" % err.text())
        if code == 403:
            raise AuthRevoked("no access to %s (%s)" % (self.site_url, err.text()))
        raise UpstreamBroken("Search Console refused the request (%s)" % err.text())

    # ------------------------------------------------------------------ parsing

    def _body(self, start: date, end: date, dimensions: list[str], start_row: int, limit: int) -> dict[str, Any]:
        return {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": dimensions,
            "type": "web",
            "dataState": "all",
            "rowLimit": limit,
            "startRow": start_row,
        }

    def _note_metadata(self, payload: Mapping[str, Any]) -> None:
        meta = payload.get("metadata")
        found = meta.get("first_incomplete_date") if isinstance(meta, dict) else None
        if isinstance(found, str) and _DATE_RE.match(found):
            try:
                self.first_incomplete_date = date.fromisoformat(found)
                return
            except ValueError:
                pass
        self.first_incomplete_date = None

    @staticmethod
    def _rows(payload: Mapping[str, Any]) -> list:
        rows = payload.get("rows")
        if rows is None:
            return []  # the API leaves `rows` out when there are none
        if not isinstance(rows, list):
            raise UpstreamBroken("rows is not a list")
        return rows

    @staticmethod
    def _figures(row: Mapping[str, Any], what: str) -> tuple[int, int, int]:
        for name in ("clicks", "impressions", "position"):
            if name not in row:
                raise UpstreamBroken("%s has no %s" % (what, name))
        clicks, impressions = _count(row["clicks"], "clicks " + what), _count(row["impressions"], "impressions " + what)
        if clicks > impressions:
            raise UpstreamBroken("%s has %d clicks and %d impressions" % (what, clicks, impressions))
        return clicks, impressions, _position_milli(row["position"], "position " + what)

    @staticmethod
    def _key(row: Any, what: str) -> str:
        keys = row.get("keys") if isinstance(row, dict) else None
        if not isinstance(keys, list) or len(keys) != 1 or not isinstance(keys[0], str) or not keys[0]:
            raise UpstreamBroken("a row's keys are not one text value (%s)" % what)
        return keys[0]

    def _daily_points(self, rows: list, start: date, end: date, seen: set[date]) -> list[CanonicalPoint]:
        points: list[CanonicalPoint] = []
        for row in rows:
            text = self._key(row, "date")
            if not _DATE_RE.match(text):
                raise UpstreamBroken("a row's date is %r, not YYYY-MM-DD" % (text,))
            try:
                day = date.fromisoformat(text)
            except ValueError:
                raise UpstreamBroken("a row's date %s is not a calendar day" % text) from None
            if not start <= day < end:
                raise UpstreamBroken("a row for %s is outside the window %s to %s" % (day, start, end))
            if day in seen:
                # A page that repeats a day would be written as one, silently keeping either.
                raise UpstreamBroken("two rows for %s" % day)
            clicks, impressions, position = self._figures(row, "the row for %s" % day)
            seen.add(day)
            provisional = self.first_incomplete_date is not None and day >= self.first_incomplete_date
            raw = {"source": "searchAnalytics", **({"provisional": True} if provisional else {})}
            points += [
                CanonicalPoint("search_clicks", day, clicks, None, raw),
                CanonicalPoint("search_impressions", day, impressions, None, raw),
                CanonicalPoint("search_position", day, position, None, raw),
            ]
        return points

    def _top_rows(self, rows: list, day: date, dimension: str, seen: set[str]) -> list[DimensionRow]:
        if len(rows) > self._top_n:
            raise UpstreamBroken("asked for %d %s rows and got %d" % (self._top_n, dimension, len(rows)))
        out: list[DimensionRow] = []
        for row in rows:
            key = self._key(row, dimension)
            if key in seen:
                raise UpstreamBroken("two %s rows for %r on %s" % (dimension, key[:40], day))
            seen.add(key)
            clicks, impressions, position = self._figures(row, "the %s row %r" % (dimension, key[:40]))
            out.append(DimensionRow(day, dimension, key, clicks, impressions, position))
        return out

    # ------------------------------------------------------------------ public

    async def verify(self) -> str:
        """The newest day with data, from a date query over the last ten days, as the how-to page suggests."""
        today = self._clock().astimezone(timezone.utc).date()
        payload, _, _ = await self._post(self._body(today - timedelta(days=10), today - timedelta(days=1), ["date"], 0, 100))
        days: list[date] = []
        for row in self._rows(payload):
            text = self._key(row, "date")
            if _DATE_RE.match(text):
                days.append(date.fromisoformat(text))
        self.latest_data = max(days) if days else None
        return "Search Console property %s%s" % (
            self.site_url, " (newest data %s)" % self.latest_data.isoformat() if self.latest_data else "")

    async def fetch(
        self,
        *,
        window_start: date,
        window_end: date,
        cursor: str | None = None,
    ) -> AsyncIterator[FetchResult]:
        """Daily figures for [window_start, window_end), page by page, then the top rows of each day with data.

        The window is half open here and the page's `endDate` is inclusive, so the end is moved back a day. The
        cursor is the `startRow` of the next page of daily rows; the top-row pages carry the cursor `top`, which
        on a resume restarts the whole fetch (the writes are idempotent).
        """
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        try:
            start_row = int(cursor) if cursor and cursor != "top" else 0
            if start_row < 0:
                raise ValueError
        except ValueError:
            raise UpstreamBroken("the resume cursor %r is not a row offset" % (cursor,)) from None
        last_day = window_end - timedelta(days=1)
        seen: set[date] = set()
        while True:
            payload, calls, waits = await self._post(
                self._body(window_start, last_day, ["date"], start_row, self._page_size))
            self._note_metadata(payload)
            rows = self._rows(payload)
            points = self._daily_points(rows, window_start, window_end, seen)
            start_row += len(rows)
            done = not rows
            more_to_come = bool(seen) and self._top_n > 0
            yield FetchResult(points=points, cursor=None if done and not more_to_come else
                              ("top" if done else str(start_row)), api_calls=calls, throttle_waits=waits)
            if done:
                break
        if self._top_n == 0:
            return
        days = sorted(seen)
        for index, day in enumerate(days):
            found: list[DimensionRow] = []
            calls = waits = 0
            for dimension in DIMENSIONS:
                payload, made, slept = await self._post(self._body(day, day, [dimension], 0, self._top_n))
                calls, waits = calls + made, waits + slept
                found += self._top_rows(self._rows(payload), day, dimension, set())
            yield FetchResult(rows=found, cursor=None if index == len(days) - 1 else "top",
                              api_calls=calls, throttle_waits=waits)
