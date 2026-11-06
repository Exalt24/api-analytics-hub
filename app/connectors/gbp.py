"""Google Business Profile connector: how a local business's listing on Google Search and Maps is used, day by day.

The tenth connector behind the contract in `base.py`, and the first for a LOCAL listing rather than an ad account or a store. It reads
the Business Profile Performance API (`locations.fetchMultiDailyMetricsTimeSeries`) and writes six daily counts per location:

    gbp_impressions         the four per surface impression counts added (desktop and mobile, Search and Maps)
    gbp_calls               taps on the profile's call button (`CALL_CLICKS`)
    gbp_website_clicks      taps on the profile's website link (`WEBSITE_CLICKS`)
    gbp_direction_requests  direction requests to the location (`BUSINESS_DIRECTION_REQUESTS`)
    gbp_messages            taps on the profile's messaging button (`MESSAGING_CLICKS`)
    gbp_bookings            bookings made from the profile through Reserve with Google (`BUSINESS_BOOKINGS`)

It has been run only against the stand-in server in `gbp_fake.py`, never against a live Business Profile: no listing is held, and Google
asks an application for basic API access before a project's quota is above 0. Every fact about the call, with the page it came from and
what is not confirmed, is in docs/AD_AND_BUSINESS_SOURCES.md (section Google Business Profile).

WHAT IT CALLS. `GET {base}/v1/locations/{id}:fetchMultiDailyMetricsTimeSeries` with one `dailyMetrics` parameter per metric and the
range as `dailyRange.start_date.year|month|day` and `dailyRange.end_date.year|month|day`, BOTH ENDS INCLUSIVE (the DailyRange page says
so), so the connector's half open window [start, end) is asked as start to end minus one day. `Authorization: Bearer` carries an
already issued token for the `business.manage` scope.

IMPRESSIONS ARE FOUR COUNTS ADDED. Each surface counts unique users per day within itself ("multiple impressions by a unique user within a
single day are counted as a single impression"), so the sum counts a person who saw the listing on Search and on Maps twice. It is a
measure of how often the listing was shown, not of how many people saw it, and it is stored under that name.

A ZERO IS ABSENT. The TimeSeries page says a datapoint's `value` "will not be present when the value is zero", so a missing `value` is
a zero. Whether a day with nothing is listed at all, or left out of `datedValues`, is not on the pages; both are read as zero, which is
the one reading that cannot invent an event. A metric the answer does not list is zeros for the same reason. That is a stated limit
of the stand-in's reading, and a response with nothing in it cannot be told from a dead listing.

WHAT IS REFUSED. A metric the request did not ask for, a series carrying a `dailySubEntityType` (a breakdown nobody asked for would be
added into the total twice), the same metric twice, a month value (`day` 0), a date outside the requested range, the same date twice
for one metric, a count that is not a whole number of at least 0, a body that is not an object, and an `error` object inside a 200.

THE TOKEN. An already issued access token, held in a `Secret`, sent only in the `Authorization` header. No URL, query or response body is
put in an error message.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Mapping

import httpx

from app.crypto import Secret

from .base import AuthExpired, AuthRevoked, CanonicalPoint, Connector, FetchResult, UpstreamBroken
from .common import BIGINT_MAX, Retrier, Retry, retry_after
from .ga4 import settled_window_end  # noqa: F401 - the same window rule, re-exported for the sync script

BASE_URL = "https://businessprofileperformance.googleapis.com"

#: canonical key -> the DailyMetric values added into it (the names on the DailyMetric page).
METRICS: dict[str, tuple[str, ...]] = {
    "gbp_impressions": ("BUSINESS_IMPRESSIONS_DESKTOP_MAPS", "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
                        "BUSINESS_IMPRESSIONS_MOBILE_MAPS", "BUSINESS_IMPRESSIONS_MOBILE_SEARCH"),
    "gbp_calls": ("CALL_CLICKS",),
    "gbp_website_clicks": ("WEBSITE_CLICKS",),
    "gbp_direction_requests": ("BUSINESS_DIRECTION_REQUESTS",),
    "gbp_messages": ("MESSAGING_CLICKS",),
    "gbp_bookings": ("BUSINESS_BOOKINGS",),
}
SUPPORTED = frozenset(METRICS)
#: The API metric each name belongs to, and every name the request asks for (a fixed order, so a request is the same every time).
_KEY_OF = {metric: key for key, names in METRICS.items() for metric in names}
REQUESTED = tuple(metric for names in METRICS.values() for metric in names)
#: A year is the longest range one request asks for. The pages give no limit; a caller with more cuts the window.
MAX_DAYS = 366

_LOCATION_RE = re.compile(r"^[0-9]{1,24}$")
_INT64_RE = re.compile(r"^[0-9]{1,19}$")


def _error(body: Any) -> dict:
    err = body.get("error") if isinstance(body, dict) else None
    return err if isinstance(err, dict) else {}


def _calendar_date(value: Any, what: str) -> date:
    if not isinstance(value, dict):
        raise UpstreamBroken("%s is not a date object" % what)
    year, month, day = value.get("year"), value.get("month"), value.get("day")
    for part in (year, month, day):
        if isinstance(part, bool) or not isinstance(part, int):
            raise UpstreamBroken("%s has a part that is not a whole number" % what)
    if not day:
        raise UpstreamBroken("%s is a month value, not a day" % what)
    try:
        return date(year, month, day)
    except ValueError:
        raise UpstreamBroken("%s is not a calendar date" % what) from None


def _count(value: Any, what: str) -> int:
    """A datapoint's `value`: an int64 sent as a string. Absent means zero (the TimeSeries page)."""
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise UpstreamBroken("%s is %s, not a count" % (what, type(value).__name__))
    text = str(value).strip()
    if not _INT64_RE.match(text):
        raise UpstreamBroken("%s is not a whole number of at least 0" % what)
    number = int(text)
    if number > BIGINT_MAX:
        raise UpstreamBroken("%s is larger than any count this hub stores" % what)
    return number


class GBPConnector(Connector):
    platform = "gbp"
    supported_metrics = SUPPORTED

    def __init__(
        self,
        *,
        location_id: str,
        access_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        retrier: Retrier | None = None,
        **retry_options: Any,
    ):
        if not _LOCATION_RE.match(str(location_id).strip()):
            raise ValueError("location_id must be the numeric Business Profile location id, got %r" % (location_id,))
        if (access_token is None) == (token_provider is None):
            raise ValueError("give exactly one of access_token and token_provider")
        self.location_id = str(location_id).strip()
        self._token = Secret(access_token) if access_token is not None else None
        self._token_provider = token_provider
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._retrier = retrier or Retrier(**retry_options)
        self.degraded_metrics: set[str] = set()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "GBPConnector":
        """Build from GBP_LOCATION_ID and GBP_ACCESS_TOKEN (and GBP_API_BASE_URL for a stand-in)."""
        env = os.environ if env is None else env
        missing = [n for n in ("GBP_LOCATION_ID", "GBP_ACCESS_TOKEN") if not env.get(n)]
        if missing:
            raise ValueError("set %s in the environment" % " and ".join(missing))
        if env.get("GBP_API_BASE_URL"):
            kwargs.setdefault("base_url", env["GBP_API_BASE_URL"])
        return cls(location_id=env["GBP_LOCATION_ID"], access_token=env["GBP_ACCESS_TOKEN"], **kwargs)

    def __repr__(self) -> str:
        return "GBPConnector(location_id=%r, base_url=%r)" % (self.location_id, self._base)

    @property
    def external_account(self) -> str:
        return self.location_id

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ transport

    def _bearer(self) -> str:
        token = self._token.reveal() if self._token is not None else self._token_provider()
        if not token or not isinstance(token, str):
            raise AuthExpired("no access token is available")
        return token

    def _classify(self, response: httpx.Response) -> Retry | None:
        if response.status_code == 200:
            return None
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        status = str(_error(body).get("status") or "")
        http = response.status_code
        text = ("HTTP %d %s" % (http, status)).strip()[:60]
        if http == 401 or status == "UNAUTHENTICATED":
            raise AuthExpired("the access token was rejected (%s)" % text)
        if http in (403, 404) or status in ("PERMISSION_DENIED", "NOT_FOUND"):
            raise AuthRevoked("no access to location %s (%s)" % (self.location_id, text))
        if http == 429 or status == "RESOURCE_EXHAUSTED" or http >= 500 or status in ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"):
            return Retry(retry_after(response), text)
        raise UpstreamBroken("Business Profile refused the request (%s)" % text)

    async def _get(self, metrics: tuple[str, ...], first: date, last: date) -> tuple[dict, int, int]:
        url = "%s/v1/locations/%s:fetchMultiDailyMetricsTimeSeries" % (self._base, self.location_id)
        params: list[tuple[str, str]] = [("dailyMetrics", m) for m in metrics]
        for side, day in (("start_date", first), ("end_date", last)):
            params += [("dailyRange.%s.year" % side, str(day.year)), ("dailyRange.%s.month" % side, str(day.month)),
                       ("dailyRange.%s.day" % side, str(day.day))]

        async def send() -> httpx.Response:
            return await self._client.get(url, params=params, headers={"Authorization": "Bearer " + self._bearer()})

        response, calls, waits = await self._retrier.run(send, self._classify)
        try:
            body = response.json()
        except ValueError:
            raise UpstreamBroken("Business Profile answered 200 with a body that is not JSON") from None
        if not isinstance(body, dict):
            raise UpstreamBroken("Business Profile answered 200 with %s, not an object" % type(body).__name__)
        if body.get("error"):
            raise UpstreamBroken("Business Profile answered 200 with an error in the body")
        return body, calls, waits

    # ------------------------------------------------------------------ verify and fetch

    async def verify(self) -> str:
        yesterday = settled_window_end(datetime.now(timezone.utc)) - timedelta(days=1)
        await self._get(("CALL_CLICKS",), yesterday, yesterday)
        return "Business Profile location %s" % self.location_id

    async def fetch(self, *, window_start: date, window_end: date, cursor: str | None = None) -> AsyncIterator[FetchResult]:
        """Six canonical rows per day for [window_start, window_end), from one request.

        There is no resume cursor (one request holds the window), so a cursor passed in is refused rather than ignored.
        """
        if window_end <= window_start:
            raise ValueError("the window must hold at least one day")
        if (window_end - window_start).days > MAX_DAYS:
            raise ValueError("a window of more than %d days must be cut by the caller" % MAX_DAYS)
        if cursor:
            raise ValueError("the Business Profile connector issues no cursor, so none can be resumed")
        last = window_end - timedelta(days=1)
        body, calls, waits = await self._get(REQUESTED, window_start, last)
        totals = self._parse(body, window_start, last)
        raw = {"source": "fetchMultiDailyMetricsTimeSeries", "location_id": self.location_id}
        points: list[CanonicalPoint] = []
        day = window_start
        while day < window_end:
            for key in METRICS:
                points.append(CanonicalPoint(key, day, totals.get((key, day), 0), None, raw))
            day += timedelta(days=1)
        yield FetchResult(points=points, cursor=None, api_calls=calls, throttle_waits=waits)

    @staticmethod
    def _parse(body: Mapping[str, Any], first: date, last: date) -> dict[tuple[str, date], int]:
        groups = body.get("multiDailyMetricTimeSeries", [])
        if not isinstance(groups, list):
            raise UpstreamBroken("multiDailyMetricTimeSeries is not a list")
        totals: dict[tuple[str, date], int] = {}
        seen_metrics: set[str] = set()
        for group in groups:
            series_list = group.get("dailyMetricTimeSeries", []) if isinstance(group, dict) else None
            if not isinstance(series_list, list):
                raise UpstreamBroken("a metric group is not an object with a dailyMetricTimeSeries list")
            for series in series_list:
                if not isinstance(series, dict):
                    raise UpstreamBroken("a metric series is not an object")
                metric = series.get("dailyMetric")
                if metric not in _KEY_OF:
                    raise UpstreamBroken("the answer carries a metric that was not asked for")
                if series.get("dailySubEntityType"):
                    raise UpstreamBroken("the answer carries a breakdown that was not asked for")
                if metric in seen_metrics:
                    raise UpstreamBroken("the same metric came back twice")
                seen_metrics.add(metric)
                values = (series.get("timeSeries") or {}).get("datedValues", []) if isinstance(series.get("timeSeries") or {}, dict) else None
                if not isinstance(values, list):
                    raise UpstreamBroken("datedValues is not a list")
                days: set[date] = set()
                for point in values:
                    if not isinstance(point, dict):
                        raise UpstreamBroken("a datapoint is not an object")
                    day = _calendar_date(point.get("date"), "the date of a datapoint")
                    if not first <= day <= last:
                        raise UpstreamBroken("a datapoint is outside the requested range")
                    if day in days:
                        raise UpstreamBroken("the same date came back twice for one metric")
                    days.add(day)
                    key = (_KEY_OF[metric], day)
                    totals[key] = totals.get(key, 0) + _count(point.get("value"), "the value of a datapoint")
        return totals
