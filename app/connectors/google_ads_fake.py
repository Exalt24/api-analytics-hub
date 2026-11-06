"""A stand-in Google Ads API server on 127.0.0.1, for tests and the combined ad spend view.

It speaks real HTTP to the connector and implements what the saved pages describe (docs/AD_AND_BUSINESS_SOURCES.md):
`POST /v26/customers/{id}/googleAds:search` with a JSON body `{"query", "pageToken"}`; a GAQL query it parses for the
two shapes the connector sends (`FROM customer` segmented by `segments.date`, and `FROM local_services_lead`); results
in lowerCamelCase with int64 values as strings and doubles as JSON numbers; `nextPageToken` and `fieldMask`; the rules
"a finite date range is required when `segments.date` is selected" and "a daily query reaches back 37 months"; the
Status error object (`{"error": {"code", "message", "status", "details"}}`) with the gRPC to HTTP codes of the errors page.

It is a stand-in written from documentation, not a recording of Google. Its own choices, marked UNVERIFIED in the sources page:
the page size (the pages say 10,000; `page_size` lowers it so a test can page), the base64 form of its page tokens, the text of
its messages, that a day with no row in `days` has no result row, and that it requires no developer token.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from .fake_http import Answer, FakeHTTP, Seen

_PATH_RE = re.compile(r"^/v26/customers/(\d{10})/googleAds:search$")
_QUERY_RE = re.compile(r"^SELECT (?P<select>.+?) FROM (?P<resource>\w+)(?: WHERE (?P<where>.+))?$", re.S)
_COND_RE = re.compile(r"^(?P<field>[a-z_.]+) (?P<op>>=|<=|<|>) '(?P<value>[^']*)'$")
CUSTOMER_FIELDS = frozenset({
    "segments.date", "customer.currency_code", "customer.id", "customer.descriptive_name", "customer.time_zone", "customer.status",
    "metrics.cost_micros", "metrics.impressions", "metrics.clicks", "metrics.conversions", "metrics.conversions_value",
})
LEAD_FIELDS = frozenset({"local_services_lead.creation_date_time", "local_services_lead.lead_type", "local_services_lead.id"})
MAX_MONTHS_BACK = 37


def status_error(code: int, status: str, message: str, details: list | None = None) -> dict:
    return {"error": {"code": code, "message": message, "status": status, "details": details or []}}


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p.title() for p in rest)


def _token(offset: int) -> str:
    return base64.b64encode(("page:%d" % offset).encode()).decode()


def _offset(token: str) -> int | None:
    try:
        text = base64.b64decode(token.encode(), validate=True).decode()
    except ValueError:
        return None
    m = re.fullmatch(r"page:(\d+)", text)
    return int(m.group(1)) if m else None


class FakeGoogleAds(FakeHTTP):
    def __init__(self, *, token: str = "test-token", today: date | None = None, page_size: int = 10_000):
        super().__init__()
        self.token = token
        self.today = today or date(2026, 11, 2)
        self.page_size = page_size
        #: Require this login-customer-id header on every request when set (a manager account's id).
        self.require_login: str | None = None
        #: customer id -> {"currency", "time_zone", "status", "days": {day: {...}}, "leads": [timestamps], "name"}
        self.customers: dict[str, dict[str, Any]] = {}
        #: Send `conversions` and `conversionsValue` as absent when zero (the pages do not say, UNVERIFIED).
        self.omit_zero_doubles = False

    def set_customer(self, customer_id: str, days: Mapping[date, Mapping[str, Any]], *, currency: str = "USD",
                     time_zone: str = "America/New_York", status: str = "ENABLED", leads: list[str] | None = None,
                     name: str = "Stand-in account") -> None:
        """`days` maps a day to {"cost_micros", "impressions", "clicks", "conversions", "conversions_value"} (Decimal or int)."""
        with self._lock:
            self.customers[customer_id] = {"currency": currency, "time_zone": time_zone, "status": status, "name": name,
                                           "days": {d: dict(v) for d, v in days.items()}, "leads": list(leads or [])}

    # ------------------------------------------------------------------ the request

    def answer(self, seen: Seen) -> Answer:
        match = _PATH_RE.match(seen.path)
        if seen.method != "POST" or not match:
            return 404, {}, status_error(404, "NOT_FOUND", "Not found.")
        if seen.headers.get("authorization") != "Bearer " + self.token:
            return 401, {}, status_error(401, "UNAUTHENTICATED", "Request had invalid authentication credentials.")
        customer = self.customers.get(match.group(1))
        if customer is None or (self.require_login and seen.headers.get("login-customer-id") != self.require_login):
            return 403, {}, status_error(403, "PERMISSION_DENIED", "The caller does not have permission")
        try:
            body = json.loads(seen.body or b"{}")
        except ValueError:
            return 400, {}, status_error(400, "INVALID_ARGUMENT", "Invalid JSON payload received.")
        query = body.get("query") if isinstance(body, dict) else None
        if not isinstance(query, str):
            return 400, {}, status_error(400, "INVALID_ARGUMENT", "query is required")
        problem = self._parse(query)
        if isinstance(problem, tuple) and problem[0] == "error":
            return 400, {}, problem[1]
        select, resource, conds = problem
        rows = self._rows(customer, resource, select, conds)
        offset = 0
        if body.get("pageToken"):
            offset = _offset(body["pageToken"])
            if offset is None:
                return 400, {}, status_error(400, "INVALID_ARGUMENT", "Invalid page token.")
        page = rows[offset: offset + self.page_size]
        out: dict[str, Any] = {"fieldMask": ",".join(_camel(f.split(".")[0]) + "." + _camel(f.split(".", 1)[1]) for f in select),
                               "requestId": "FAKEREQUEST"}
        if page:
            out["results"] = page
        if offset + self.page_size < len(rows):
            out["nextPageToken"] = _token(offset + self.page_size)
        return 200, {"request-id": "FAKEREQUEST"}, json.dumps(out, default=float).encode()

    def _parse(self, query: str):
        m = _QUERY_RE.match(" ".join(query.split()))
        if not m:
            return "error", status_error(400, "INVALID_ARGUMENT", "The query is not one this stand-in parses.", [
                {"errorCode": {"queryError": "UNEXPECTED_END_OF_QUERY"}}])
        select = [f.strip() for f in m.group("select").split(",")]
        resource = m.group("resource")
        allowed = {"customer": CUSTOMER_FIELDS, "local_services_lead": LEAD_FIELDS}.get(resource)
        if allowed is None:
            return "error", status_error(400, "INVALID_ARGUMENT", "Unrecognized resource.", [{"errorCode": {"queryError": "INVALID_FROM_CLAUSE"}}])
        bad = [f for f in select if f not in allowed]
        if bad:
            return "error", status_error(400, "INVALID_ARGUMENT", "Unrecognized field in the query: '%s'." % bad[0],
                                         [{"errorCode": {"queryError": "UNRECOGNIZED_FIELD"}}])
        conds = []
        for part in re.split(r" AND ", m.group("where")) if m.group("where") else []:
            c = _COND_RE.match(part.strip())
            if not c:
                return "error", status_error(400, "INVALID_ARGUMENT", "Unsupported condition.", [{"errorCode": {"queryError": "BAD_VALUE"}}])
            conds.append((c.group("field"), c.group("op"), c.group("value")))
        if resource == "customer":
            lows = [v for f, op, v in conds if f == "segments.date" and op in (">=", ">")]
            highs = [v for f, op, v in conds if f == "segments.date" and op in ("<=", "<")]
            if "segments.date" in select and not (lows and highs):
                # "If any of these segments are in the SELECT clause, a finite date range ... must be specified in the WHERE clause."
                return "error", status_error(400, "INVALID_ARGUMENT", "Query requires a finite date range.", [
                    {"errorCode": {"queryError": "CANNOT_SELECT_WITHOUT_DATE_RANGE"}}])
            if lows:
                try:
                    low = date.fromisoformat(min(lows))
                except ValueError:
                    return "error", status_error(400, "INVALID_ARGUMENT", "Invalid date.", [{"errorCode": {"queryError": "BAD_VALUE"}}])
                if (self.today.year - low.year) * 12 + self.today.month - low.month > MAX_MONTHS_BACK:
                    return "error", status_error(400, "INVALID_ARGUMENT", "Date range is too far back.", [
                        {"errorCode": {"dateRangeError": "REQUESTED_DATE_GRANULARITY_NOT_SUPPORTED"}}])
        return select, resource, conds

    def _rows(self, customer: Mapping[str, Any], resource: str, select: list[str], conds: list[tuple[str, str, str]]) -> list[dict]:
        if resource == "local_services_lead":
            out = []
            for stamp in sorted(customer["leads"]):
                if all(self._compare(stamp, op, value) for f, op, value in conds if f == "local_services_lead.creation_date_time"):
                    row: dict[str, Any] = {}
                    if "local_services_lead.creation_date_time" in select:
                        row.setdefault("localServicesLead", {})["creationDateTime"] = stamp
                    if "local_services_lead.lead_type" in select:
                        row.setdefault("localServicesLead", {})["leadType"] = "PHONE_CALL"
                    out.append(row)
            return out
        out = []
        for day in sorted(customer["days"]):
            iso = day.isoformat()
            if not all(self._compare(iso, op, value) for f, op, value in conds if f == "segments.date"):
                continue
            entry = customer["days"][day]
            row = {"customer": {}, "metrics": {}, "segments": {}}
            if "segments.date" in select:
                row["segments"]["date"] = iso
            if "customer.currency_code" in select:
                row["customer"]["currencyCode"] = customer["currency"]
            if "customer.id" in select:
                row["customer"]["id"] = "1"
            if "metrics.cost_micros" in select:
                row["metrics"]["costMicros"] = str(entry.get("cost_micros", 0))
            if "metrics.impressions" in select:
                row["metrics"]["impressions"] = str(entry.get("impressions", 0))
            if "metrics.clicks" in select:
                row["metrics"]["clicks"] = str(entry.get("clicks", 0))
            for src, dst in (("conversions", "conversions"), ("conversions_value", "conversionsValue")):
                value = Decimal(str(entry.get(src, 0)))
                if "metrics." + src in select and not (self.omit_zero_doubles and value == 0):
                    row["metrics"][dst] = value
            out.append({k: v for k, v in row.items() if v})
        if select == [f for f in select if f.startswith("customer.")]:
            # The account query has no date segment: one row about the account itself.
            out = [{"customer": {_camel(f.split(".", 1)[1]): self._customer_field(customer, f) for f in select}}]
        return out

    @staticmethod
    def _customer_field(customer: Mapping[str, Any], field: str) -> Any:
        return {"customer.id": "1234567890", "customer.descriptive_name": customer["name"], "customer.currency_code": customer["currency"],
                "customer.time_zone": customer["time_zone"], "customer.status": customer["status"]}[field]

    @staticmethod
    def _compare(left: str, op: str, right: str) -> bool:
        return {">=": left >= right, "<=": left <= right, "<": left < right, ">": left > right}[op]
