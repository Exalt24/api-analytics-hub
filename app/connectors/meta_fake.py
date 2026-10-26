"""A stand-in Meta Marketing API server on 127.0.0.1, for tests and the sample deck.

It speaks real HTTP to the connector and implements what the saved pages describe (docs/META_SOURCES.md):
`GET /v26.0/act_{id}/insights` with `time_range`, `time_increment`, `level`, `fields`,
`action_attribution_windows`, `limit` and `after`; rows whose values are numeric strings, the date as
`date_start` and `date_stop`, `actions` and `action_values` lists whose entries carry `action_type`, `value`
and a number for every attribution window asked for; cursor paging with `paging.cursors.after` and a `next`
link that carries the access token, and its absence on the last page; the error object
(`{"error": {"message", "type", "code", "error_subcode", "fbtrace_id"}}`); and the three usage headers.

It is a stand-in written from documentation, not a recording of Meta. What it cannot tell you is listed in
docs/META_SOURCES.md. Where it refuses something (an unknown field, a window it does not know, a cursor it
did not issue, a start date more than 37 months back) it does so because a page says the real API would, so a
connector that passes here has not been let off a documented rule. Two choices are its own and are marked
UNVERIFIED there: the HTTP status it puts on an error object (400), and that a day with no impressions and no
spend has no row.

Faults are scripted: `server.faults.extend([Fault(400, error_body(4, "OAuthException", "..."))])` makes the
next request answer that way, once per entry, before the normal answer resumes.
"""
from __future__ import annotations

import base64
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

KNOWN_FIELDS = (
    "account_currency", "account_id", "account_name", "spend", "impressions", "clicks", "ctr", "cpc",
    "actions", "action_values", "reach", "frequency", "date_start", "date_stop",
)
KNOWN_WINDOWS = ("1d_view", "7d_view", "28d_view", "1d_click", "7d_click", "28d_click", "1d_ev", "dda", "default")
KNOWN_LEVELS = ("ad", "adset", "campaign", "account")
_PATH_RE = re.compile(r"^/v26\.0/act_(\d+)(/insights)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_MONTHS_BACK = 37


@dataclass
class Fault:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    #: True makes the server drop the connection without answering, a transport failure.
    drop: bool = False


def error_body(code: int, kind: str, message: str, subcode: int | None = None) -> dict:
    err: dict[str, Any] = {"message": message, "type": kind, "code": code, "fbtrace_id": "FAKETRACE"}
    if subcode is not None:
        err["error_subcode"] = subcode
    return {"error": err}


def _cursor(offset: int, empties: int = 0) -> str:
    return base64.b64encode(("o:%d:e:%d" % (offset, empties)).encode()).decode()


def _decode(cursor: str) -> tuple[int, int] | None:
    """(offset, empty pages still to serve) of a cursor this stand-in issued, or None."""
    try:
        text = base64.b64decode(cursor.encode(), validate=True).decode()
    except ValueError:
        return None
    m = re.fullmatch(r"o:(\d+):e:(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


class FakeMeta:
    """Start with `with FakeMeta(token="t") as server:`, then point the connector at `server.url`."""

    def __init__(self, *, token: str = "test-token", today: date | None = None):
        self.token = token
        self.today = today or date(2026, 11, 2)
        self.faults: list[Fault] = []
        #: account id -> {"currency", "timezone", "status", "days": {day: {...}}}
        self.accounts: dict[str, dict[str, Any]] = {}
        #: Every request seen: (path, parsed query, raw query string).
        self.requests: list[tuple[str, dict[str, str], str]] = []
        #: Utilisation percent the stand-in reports in its headers.
        self.utilisation = 5
        #: Minutes to regain access reported in the business use case header.
        self.regain_minutes = 0
        #: Make the `next` link point at this base address instead of the stand-in's own (a hostile response).
        self.next_base: str | None = None
        #: Answer this many empty pages that still carry a `next` link before the real ones (the paging page allows it).
        self.empty_pages_with_next = 0
        #: Report these action types as purchases besides omni_purchase, as the page lists them, so a connector that adds them counts more than once.
        self.part_purchase_types = ("offsite_conversion.fb_pixel_purchase", "onsite_conversion.purchase")
        #: Added to the `value` of every action entry (the "default window" figure) but not to the per-window
        #: numbers, so a connector that reads `value` instead of the window it asked for reads the wrong figure.
        self.default_value_skew = 0
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> "FakeMeta":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802 - the name http.server looks for
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
                status, headers, body, drop = fake.handle(parsed.path, query, parsed.query)
                if drop:
                    self.close_connection = True
                    self.connection.close()
                    return
                data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=UTF-8")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        assert self._server is not None
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def set_account(self, account_id: str, days: Mapping[date, Mapping[str, Any]], *, currency: str = "USD",
                    timezone: str = "America/Los_Angeles", status: int = 1) -> None:
        """`days` maps a day to {"spend": "12.34", "impressions": int, "clicks": int, "purchases": int|None, "purchase_value": "1.5"|None}."""
        with self._lock:
            self.accounts[str(account_id)] = {
                "currency": currency, "timezone": timezone, "status": status,
                "days": {d: dict(v) for d, v in days.items()},
            }

    # ------------------------------------------------------------------ the request

    def _headers(self) -> dict[str, str]:
        out = {
            "x-fb-ads-insights-throttle": json.dumps(
                {"app_id_util_pct": self.utilisation, "acc_id_util_pct": self.utilisation, "ads_api_access_tier": "standard_access"}),
            "x-business-use-case-usage": json.dumps({"123": [{
                "type": "ads_insights", "call_count": self.utilisation, "total_cputime": 1, "total_time": 1,
                "estimated_time_to_regain_access": self.regain_minutes, "ads_api_access_tier": "standard_access"}]}),
        }
        return out

    def handle(self, path: str, query: dict[str, str], raw_query: str) -> tuple[int, dict[str, str], Any, bool]:
        with self._lock:
            self.requests.append((path, dict(query), raw_query))
            if self.faults:
                fault = self.faults.pop(0)
                if fault.drop:
                    return 0, {}, b"", True
                body = fault.body if fault.body is not None else error_body(2, "OAuthException", "scripted fault")
                return fault.status, dict(fault.headers), body, False
            return self._answer(path, query)

    def _answer(self, path: str, query: dict[str, str]) -> tuple[int, dict[str, str], Any, bool]:
        match = _PATH_RE.match(path)
        if not match:
            return 404, {}, error_body(100, "GraphMethodException", "Unsupported get request.", 33), False
        if query.get("access_token") != self.token:
            return 400, {}, error_body(190, "OAuthException", "Invalid OAuth access token."), False
        account_id = match.group(1)
        account = self.accounts.get(account_id)
        if account is None:
            return 400, {}, error_body(200, "OAuthException", "(#200) Permissions error"), False
        if match.group(2) is None:
            return self._account(account, account_id, query)
        problem = self._validate(query)
        if problem:
            code, message, subcode = problem
            return 400, {}, error_body(code, "OAuthException", message, subcode), False

        since, until = self._range(query)
        fields = [f for f in query.get("fields", "impressions,spend").split(",") if f]
        windows = json.loads(query["action_attribution_windows"]) if "action_attribution_windows" in query else ["default"]
        rows_all = []
        day = since
        while day <= until:
            entry = account["days"].get(day)
            if entry is not None and (entry.get("impressions", 0) or Decimal(str(entry.get("spend", "0")))):
                rows_all.append(self._row(account, day, entry, fields, windows))
            day += timedelta(days=1)

        limit = int(query.get("limit", 25))
        offset, empties = _decode(query["after"]) if "after" in query else (0, self.empty_pages_with_next)
        if empties:
            # A scripted run of empty pages that still point at a next page, which the paging page allows.
            return 200, self._headers(), self._page([], offset, offset, empties - 1, query, more=True), False
        page = rows_all[offset: offset + limit]
        end = offset + len(page)
        return 200, self._headers(), self._page(page, offset, end, 0, query, more=end < len(rows_all)), False

    def _page(self, rows: list, offset: int, end: int, empties: int, query: dict[str, str], *, more: bool) -> dict:
        out: dict[str, Any] = {"data": rows}
        paging: dict[str, Any] = {"cursors": {"before": _cursor(offset, empties), "after": _cursor(end, empties)}}
        if more:
            nxt = "after=%s&access_token=%s" % (paging["cursors"]["after"], self.token)
            base = self.next_base or self.url
            paging["next"] = "%s/v26.0/act_x/insights?%s" % (base, nxt)
        out["paging"] = paging
        return out

    def _account(self, account: Mapping[str, Any], account_id: str, query: dict[str, str]) -> tuple[int, dict[str, str], Any, bool]:
        fields = [f for f in query.get("fields", "id").split(",") if f]
        known = {"id": "act_" + account_id, "account_status": account["status"], "currency": account["currency"],
                 "timezone_name": account["timezone"]}
        bad = [f for f in fields if f not in known]
        if bad:
            return 400, {}, error_body(100, "GraphMethodException", "(#100) Tried accessing nonexisting field (%s)" % bad[0]), False
        return 200, self._headers(), {k: known[k] for k in fields} | {"id": known["id"]}, False

    def _range(self, query: dict[str, str]) -> tuple[date, date]:
        rng = json.loads(query["time_range"])
        return date.fromisoformat(rng["since"]), date.fromisoformat(rng["until"])

    def _row(self, account: Mapping[str, Any], day: date, entry: Mapping[str, Any], fields: list[str],
             windows: list[str]) -> dict[str, Any]:
        row: dict[str, Any] = {}
        if "account_currency" in fields:
            row["account_currency"] = account["currency"]
        if "spend" in fields:
            row["spend"] = str(entry.get("spend", "0"))
        if "impressions" in fields:
            row["impressions"] = str(entry.get("impressions", 0))
        if "clicks" in fields:
            row["clicks"] = str(entry.get("clicks", 0))
        if "actions" in fields and entry.get("purchases") is not None:
            row["actions"] = [self._action("link_click", str(entry.get("clicks", 0)), windows)] + [
                self._action(t, str(entry["purchases"]), windows) for t in ("omni_purchase",) + tuple(self.part_purchase_types)]
        if "action_values" in fields and entry.get("purchase_value") is not None:
            row["action_values"] = [
                self._action(t, str(entry["purchase_value"]), windows) for t in ("omni_purchase",) + tuple(self.part_purchase_types)]
        row["date_start"] = row["date_stop"] = day.isoformat()
        return row

    def _action(self, kind: str, value: str, windows: list[str]) -> dict[str, str]:
        out = {"action_type": kind, "value": str(Decimal(value) + self.default_value_skew)}
        for w in windows:
            if w != "default":
                out[w] = value
        return out

    def _validate(self, query: dict[str, str]) -> tuple[int, str, int | None] | None:
        """The refusals the saved pages describe; None when the request would be accepted."""
        if "time_range" not in query:
            return 100, "(#100) this stand-in needs time_range", None
        try:
            rng = json.loads(query["time_range"])
            since, until = date.fromisoformat(rng["since"]), date.fromisoformat(rng["until"])
        except (ValueError, KeyError, TypeError):
            return 100, "(#100) time_range must be {'since':YYYY-MM-DD,'until':YYYY-MM-DD}", None
        if not (_DATE_RE.match(rng["since"]) and _DATE_RE.match(rng["until"])):
            return 100, "(#100) time_range dates must be YYYY-MM-DD", None
        if since > until:
            return 100, "(#100) since cannot be after until", None
        if (self.today.year - since.year) * 12 + self.today.month - since.month > MAX_MONTHS_BACK:
            return 3018, "The start date of the time range cannot be beyond 37 months from the current date", None
        if query.get("level", "account") not in KNOWN_LEVELS:
            return 100, "(#100) level must be one of ad, adset, campaign, account", None
        inc = query.get("time_increment", "all_days")
        if inc not in ("all_days", "monthly") and not (inc.isdigit() and 1 <= int(inc) <= 90):
            return 100, "(#100) time_increment must be all_days, monthly or 1 to 90", None
        for name in [f for f in query.get("fields", "").split(",") if f]:
            if name not in KNOWN_FIELDS:
                return 100, "(#100) %s is not valid for fields param" % name, None
        if "action_attribution_windows" in query:
            try:
                windows = json.loads(query["action_attribution_windows"])
            except ValueError:
                return 100, "(#100) action_attribution_windows must be a list", None
            if not isinstance(windows, list) or any(w not in KNOWN_WINDOWS for w in windows):
                return 100, "(#100) action_attribution_windows has a window this stand-in does not know", None
        if "limit" in query and not (query["limit"].isdigit() and int(query["limit"]) >= 1):
            return 100, "(#100) limit must be a positive integer", None
        if "after" in query and _decode(query["after"]) is None:
            return 2642, "Invalid cursors values", None
        return None
