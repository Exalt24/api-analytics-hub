"""A stand-in Business Profile Performance API server on 127.0.0.1, for tests.

It speaks real HTTP to the connector and implements what the saved pages describe (docs/AD_AND_BUSINESS_SOURCES.md, section Google Business
Profile): `GET /v1/locations/{id}:fetchMultiDailyMetricsTimeSeries` with one `dailyMetrics` parameter per metric (the DailyMetric names) and
`dailyRange.start_date|end_date.year|month|day`, both ends inclusive; the `multiDailyMetricTimeSeries[].dailyMetricTimeSeries[]` answer with
`dailyMetric`, an optional `dailySubEntityType` and `timeSeries.datedValues[]` of `{date: {year, month, day}, value}` where `value` is an int64
as a string and is NOT PRESENT when it is zero; Google's error object (`error.code`, `error.message`, `error.status`).

It is a stand-in written from documentation, not a recording of Google. Its own choices, marked UNVERIFIED in the sources page: whether a day with
nothing is listed at all (`list_empty_days` flips it; the default lists it without a `value`), whether a metric with no datapoints is listed
(`list_empty_metrics`), the status it puts on a bad parameter (400 INVALID_ARGUMENT), the order of the series, and the text of its messages.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from .fake_http import Answer, FakeHTTP, Seen
from .gbp import REQUESTED

_PATH = re.compile(r"^/v1/locations/([0-9]+):fetchMultiDailyMetricsTimeSeries$")


def error_body(code: int, status: str, message: str = "stand-in") -> dict:
    return {"error": {"code": code, "message": message, "status": status}}


def _day(q: dict[str, str], side: str) -> date | None:
    try:
        return date(int(q["dailyRange.%s.year" % side]), int(q["dailyRange.%s.month" % side]), int(q["dailyRange.%s.day" % side]))
    except (KeyError, ValueError):
        return None


class FakeGBP(FakeHTTP):
    def __init__(self, *, token: str = "test-token", list_empty_days: bool = True, list_empty_metrics: bool = True):
        super().__init__()
        self.token = token
        self.list_empty_days = list_empty_days
        self.list_empty_metrics = list_empty_metrics
        #: location id -> DailyMetric name -> date -> count
        self.locations: dict[str, dict[str, dict[date, int]]] = {}

    def set_location(self, location_id: str) -> None:
        with self._lock:
            self.locations.setdefault(location_id, {})

    def set_series(self, location_id: str, metric: str, days: dict[date, int]) -> None:
        with self._lock:
            self.locations.setdefault(location_id, {}).setdefault(metric, {}).update(days)

    # ------------------------------------------------------------------ the request

    def answer(self, seen: Seen) -> Answer:
        m = _PATH.match(seen.path)
        if seen.method != "GET" or not m:
            return 404, {}, error_body(404, "NOT_FOUND", "Not found.")
        if seen.headers.get("authorization") != "Bearer " + self.token:
            return 401, {}, error_body(401, "UNAUTHENTICATED", "Request had invalid authentication credentials.")
        if m.group(1) not in self.locations:
            return 404, {}, error_body(404, "NOT_FOUND", "Location not found.")
        metrics = seen.query_all.get("dailyMetrics", [])
        if not metrics or any(name not in REQUESTED for name in metrics):
            return 400, {}, error_body(400, "INVALID_ARGUMENT", "dailyMetrics is required and must be DailyMetric values.")
        first, last = _day(seen.query, "start_date"), _day(seen.query, "end_date")
        if first is None or last is None or last < first:
            return 400, {}, error_body(400, "INVALID_ARGUMENT", "dailyRange is required and both dates must be valid.")
        series: list[dict[str, Any]] = []
        for name in metrics:
            have = self.locations[m.group(1)].get(name, {})
            dated: list[dict[str, Any]] = []
            day = first
            while day <= last:
                value = have.get(day, 0)
                if value or self.list_empty_days:
                    point: dict[str, Any] = {"date": {"year": day.year, "month": day.month, "day": day.day}}
                    if value:
                        point["value"] = str(value)          # an int64 is a string, and is absent when zero
                    dated.append(point)
                day += timedelta(days=1)
            if dated or self.list_empty_metrics:
                series.append({"dailyMetric": name, "timeSeries": {"datedValues": dated}})
        return 200, {}, {"multiDailyMetricTimeSeries": [{"dailyMetricTimeSeries": series}]}
