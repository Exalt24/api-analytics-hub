"""A stand-in TikTok Business API server on 127.0.0.1, for tests and the combined ad spend view.

It speaks real HTTP to the connector and implements what the saved pages describe (docs/AD_AND_BUSINESS_SOURCES.md):
`GET /open_api/v1.3/report/integrated/get/` with `advertiser_id`, `service_type`, `report_type`, `data_level`, JSON list
`dimensions` and `metrics`, `start_date`, `end_date`, `page` and `page_size`; the envelope `{"code", "message", "request_id", "data":
{"page_info": {...}, "list": [{"dimensions": {...}, "metrics": {...}}]}}` with every metric a string; the rule "a daily breakdown reaches
at most 30 days"; the rule "`currency` as a metric needs `advertiser_id` among the dimensions"; a day with no data left out of the
list; the `Access-Token` header; and `GET /open_api/v1.3/advertiser/info/`.

It is a stand-in written from documentation, not a recording of TikTok. Its own choices, marked UNVERIFIED in the sources page: that an
error is HTTP 200 with a non-zero `code` (the connector reads the code first, so 401, 403, 429 and 5xx work too), the default page
size of 10 it takes from the page, that `end_date` is inclusive, the form `YYYY-MM-DD 00:00:00` of `stat_time_day`, and the text of its messages.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any, Mapping

from .fake_http import Answer, FakeHTTP, Seen

KNOWN_METRICS = frozenset({"spend", "billed_cost", "impressions", "clicks", "ctr", "reach", "conversion", "cost_per_conversion", "currency"})
KNOWN_DIMENSIONS = frozenset({"advertiser_id", "campaign_id", "adgroup_id", "ad_id", "stat_time_day", "stat_time_hour", "country_code"})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REPORT_PATH = "/open_api/v1.3/report/integrated/get/"
INFO_PATH = "/open_api/v1.3/advertiser/info/"


def envelope(code: int, message: str, data: Any = None) -> dict:
    return {"code": code, "message": message, "request_id": "FAKEREQUEST", "data": data if data is not None else {}}


class FakeTikTok(FakeHTTP):
    def __init__(self, *, token: str = "test-token"):
        super().__init__()
        self.token = token
        #: advertiser id -> {"currency", "timezone", "status", "name", "days": {day: {...}}}
        self.advertisers: dict[str, dict[str, Any]] = {}
        #: Send this value in x-tt-ads-throttle on every report answer (the 20,000 id truncation marker).
        self.throttle_header: str | None = None
        #: Format of `stat_time_day` in a row.
        self.day_format = "%s 00:00:00"
        #: Answer rows for a different advertiser than the one asked about (a hostile or confused response).
        self.rows_for_advertiser: str | None = None

    def set_advertiser(self, advertiser_id: str, days: Mapping[date, Mapping[str, Any]], *, currency: str = "USD",
                       timezone: str = "America/New_York", status: str = "STATUS_ENABLE", name: str = "Stand-in advertiser") -> None:
        """`days` maps a day to {"spend": "12.34", "impressions": int, "clicks": int, "conversion": int|str}."""
        with self._lock:
            self.advertisers[advertiser_id] = {"currency": currency, "timezone": timezone, "status": status, "name": name,
                                               "days": {d: dict(v) for d, v in days.items()}}

    # ------------------------------------------------------------------ the request

    def answer(self, seen: Seen) -> Answer:
        if seen.method != "GET" or seen.path not in (REPORT_PATH, INFO_PATH):
            return 404, {}, {"code": 40007, "message": "The operation/object does not exist.", "request_id": "FAKEREQUEST", "data": {}}
        if seen.headers.get("access-token") != self.token:
            return 200, {}, envelope(40105, "Access token is incorrect or has been revoked.")
        if seen.path == INFO_PATH:
            return self._info(seen.query)
        return self._report(seen.query)

    def _list_param(self, query: dict[str, str], name: str) -> list | None:
        try:
            value = json.loads(query[name])
        except (KeyError, ValueError):
            return None
        return value if isinstance(value, list) else None

    def _info(self, query: dict[str, str]) -> Answer:
        ids = self._list_param(query, "advertiser_ids")
        if not ids or not all(isinstance(i, str) for i in ids):
            return 200, {}, envelope(40002, "advertiser_ids must be a list of strings")
        rows = []
        for ident in ids:
            adv = self.advertisers.get(ident)
            if adv is None:
                return 200, {}, envelope(40001, "No permission to operate advertiser %s." % ident)
            rows.append({"advertiser_id": ident, "name": adv["name"], "currency": adv["currency"], "timezone": adv["timezone"],
                         "status": adv["status"]})
        return 200, {}, envelope(0, "OK", {"list": rows})

    def _report(self, q: dict[str, str]) -> Answer:
        advertiser = self.advertisers.get(q.get("advertiser_id", ""))
        if advertiser is None:
            return 200, {}, envelope(40001, "No permission to operate advertiser %s." % q.get("advertiser_id"))
        for name, allowed in (("report_type", ("BASIC", "AUDIENCE", "PLAYABLE_MATERIAL", "DSA", "BC", "TT_SHOP")),
                              ("service_type", ("AUCTION", "RESERVATION")), ("data_level", ("AUCTION_ADVERTISER", "AUCTION_CAMPAIGN", "AUCTION_ADGROUP", "AUCTION_AD"))):
            if q.get(name) not in allowed:
                return 200, {}, envelope(40002, "%s is required and must be one of %s" % (name, ", ".join(allowed)))
        dims, metrics = self._list_param(q, "dimensions"), self._list_param(q, "metrics")
        if not dims or any(d not in KNOWN_DIMENSIONS for d in dims):
            return 200, {}, envelope(40002, "dimensions is required and holds an unsupported value")
        if not metrics or any(m not in KNOWN_METRICS for m in metrics):
            return 200, {}, envelope(40002, "metrics holds an unsupported value")
        if "currency" in metrics and "advertiser_id" not in dims:
            return 200, {}, envelope(40002, "currency as a metric needs advertiser_id in dimensions")
        if q.get("data_level") == "AUCTION_ADVERTISER" and "advertiser_id" not in dims:
            return 200, {}, envelope(40002, "the data level must match the dimensions")
        if not (_DATE_RE.match(q.get("start_date", "")) and _DATE_RE.match(q.get("end_date", ""))):
            return 200, {}, envelope(40002, "start_date and end_date must be YYYY-MM-DD")
        try:
            start, end = date.fromisoformat(q["start_date"]), date.fromisoformat(q["end_date"])
        except ValueError:
            return 200, {}, envelope(40002, "start_date and end_date must be calendar days")
        if end < start:
            return 200, {}, envelope(40002, "end_date is before start_date")
        if "stat_time_day" in dims and (end - start).days + 1 > 30:
            return 200, {}, envelope(40002, "a daily breakdown can retrieve data for a maximum of 30 days")
        if not (q.get("page", "1").isdigit() and int(q.get("page", "1")) >= 1 and q.get("page_size", "10").isdigit()
                and int(q.get("page_size", "10")) >= 1):
            return 200, {}, envelope(40002, "page and page_size must be positive integers")
        page, size = int(q.get("page", "1")), int(q.get("page_size", "10"))
        rows = []
        day = start
        while day <= end:
            entry = advertiser["days"].get(day)
            if entry is not None:
                dim_out: dict[str, str] = {}
                if "advertiser_id" in dims:
                    dim_out["advertiser_id"] = self.rows_for_advertiser or q["advertiser_id"]
                if "stat_time_day" in dims:
                    dim_out["stat_time_day"] = self.day_format % day.isoformat()
                met_out = {}
                for m in metrics:
                    met_out[m] = advertiser["currency"] if m == "currency" else str(entry.get(m, "0"))
                rows.append({"dimensions": dim_out, "metrics": met_out})
            day += timedelta(days=1)
        total = len(rows)
        pages = max(1, -(-total // size)) if total else 0
        headers = {"x-tt-ads-throttle": self.throttle_header} if self.throttle_header else {}
        data = {"page_info": {"page": page, "page_size": size, "total_number": total, "total_page": pages},
                "list": rows[(page - 1) * size: page * size]}
        return 200, headers, envelope(0, "OK", data)
