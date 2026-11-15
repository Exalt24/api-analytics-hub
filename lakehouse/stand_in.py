"""A stand-in for the Salesforce REST query endpoint, standard library only. It is not Salesforce.

It speaks real HTTP on 127.0.0.1 and follows the pages quoted in docs/AD_AND_BUSINESS_SOURCES.md (the Salesforce section): a bearer token is required
(401 with a list of {message, errorCode} otherwise), `GET /services/data/vXX.X/query?q=` answers with `totalSize`, `done`, `records` (each with an
`attributes` object) and a `nextRecordsUrl` until done, a batch size comes from `Sforce-Query-Options: batchSize=N`. It understands one SOQL shape only,
`SELECT <fields> FROM Lead [WHERE LastModifiedDate >= <datetime literal>] [ORDER BY ...]`, and refuses a quoted datetime or an unknown field with
MALFORMED_QUERY. Where the saved pages are silent (the form of the locator in a next URL, the status of an error) the choices are its own.

Faults a test can switch on, to see what the pipeline does: `corrupt_page` (that page number answers text that is not JSON), `http_errors` (the next N
requests answer 503), `duplicate_on_page` (the last record of a page is repeated at the top of the next, as an overlapping page would), `next_url_override`
(the next page address is whatever a test says, to see a refusal) and `total_override` (a totalSize that does not match).
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

API_VERSION = "v62.0"
KNOWN_FIELDS = {"Id", "CreatedDate", "LastModifiedDate", "LeadSource", "Status", "IsConverted", "ConvertedDate"}
_QUERY = re.compile(r"^\s*SELECT\s+(?P<fields>.+?)\s+FROM\s+Lead(?:\s+WHERE\s+LastModifiedDate\s*>=\s*(?P<since>\S+))?(?:\s+ORDER\s+BY\s+.+?)?\s*$", re.I | re.S)
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$")


def parse_sf_datetime(text: str) -> datetime:
    t = text.replace("Z", "+0000")
    if re.search(r"[+-]\d{2}:\d{2}$", t):
        t = t[:-3] + t[-2:]
    fmt = "%Y-%m-%dT%H:%M:%S.%f%z" if "." in t else "%Y-%m-%dT%H:%M:%S%z"
    return datetime.strptime(t, fmt).astimezone(timezone.utc)


class StandInSalesforce:
    def __init__(self, leads: list[dict], token: str = "stand-in-token", default_batch: int = 200):
        self.leads = list(leads)
        self.token = token
        self.default_batch = default_batch
        self.corrupt_page: int | None = None
        self.http_errors = 0
        self.duplicate_on_page = False
        self.next_url_override: str | None = None
        self.total_override: int | None = None
        self.calls = 0
        self.api_usage = (0, 100000)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---- control
    @property
    def url(self) -> str:
        assert self._server is not None, "start() first"
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "StandInSalesforce":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401 - silence the access log
                return

            def do_GET(self):  # noqa: N802
                outer._handle(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def revise(self, lead_id: str, modified: str, **changes) -> None:
        """Change a lead and move its LastModifiedDate, as a user edit in the org would."""
        for lead in self.leads:
            if lead["Id"] == lead_id:
                lead.update(changes)
                lead["LastModifiedDate"] = modified
                return
        raise KeyError(lead_id)

    # ---- the protocol
    def _send(self, h: BaseHTTPRequestHandler, status: int, body, raw: bool = False) -> None:
        data = body.encode() if raw else json.dumps(body).encode()
        h.send_response(status)
        h.send_header("Content-Type", "text/plain" if raw else "application/json;charset=UTF-8")
        h.send_header("Sforce-Limit-Info", "api-usage=%d/%d" % self.api_usage)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    @staticmethod
    def _error(code: str, message: str) -> list:
        return [{"message": message, "errorCode": code}]

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        with self._lock:
            self.calls += 1
            if self.http_errors > 0:
                self.http_errors -= 1
                return self._send(h, 503, self._error("SERVER_UNAVAILABLE", "stand-in 503"))
        if h.headers.get("Authorization") != "Bearer " + self.token:
            return self._send(h, 401, self._error("INVALID_SESSION_ID", "Session expired or invalid"))
        parsed = urlparse(h.path)
        prefix = f"/services/data/{API_VERSION}/query"
        if parsed.path == prefix:
            query = parse_qs(parsed.query).get("q", [""])[0]
            m = _QUERY.match(query)
            if not m:
                return self._send(h, 400, self._error("MALFORMED_QUERY", "unsupported query"))
            fields = [f.strip() for f in m["fields"].split(",")]
            unknown = [f for f in fields if f not in KNOWN_FIELDS]
            if unknown:
                return self._send(h, 400, self._error("INVALID_FIELD", "No such column '%s' on entity 'Lead'" % unknown[0]))
            since = m["since"]
            if since is not None and not _DATETIME.match(since):
                return self._send(h, 400, self._error("MALFORMED_QUERY", "a datetime literal is not quoted"))
            rows = [r for r in self.leads if since is None or parse_sf_datetime(r["LastModifiedDate"]) >= parse_sf_datetime(since)]
            rows.sort(key=lambda r: (parse_sf_datetime(r["LastModifiedDate"]), r["Id"]))
            batch = self._batch(h)
            locator = "01gST%010d" % (abs(hash((query, batch))) % 10**10)
            self._cursors = getattr(self, "_cursors", {})
            self._cursors[locator] = (rows, fields, batch)
            return self._page(h, locator, 0)
        if parsed.path.startswith(prefix + "/"):
            token = parsed.path[len(prefix) + 1:]
            locator, _, off = token.rpartition("-")
            cursor = getattr(self, "_cursors", {}).get(locator)
            if cursor is None or not off.isdigit():
                return self._send(h, 404, self._error("NOT_FOUND", "The requested resource does not exist"))
            return self._page(h, locator, int(off))
        return self._send(h, 404, self._error("NOT_FOUND", "The requested resource does not exist"))

    def _batch(self, h: BaseHTTPRequestHandler) -> int:
        m = re.search(r"batchSize=(\d+)", h.headers.get("Sforce-Query-Options", ""))
        return max(1, int(m.group(1))) if m else self.default_batch

    def _page(self, h: BaseHTTPRequestHandler, locator: str, offset: int) -> None:
        rows, fields, batch = self._cursors[locator]
        number = offset // batch + 1
        if self.corrupt_page == number:
            return self._send(h, 200, '{"totalSize": 3, "done": fal', raw=True)
        chunk = rows[offset:offset + batch]
        if self.duplicate_on_page and offset > 0 and rows[offset - 1:offset]:
            chunk = rows[offset - 1:offset] + chunk
        records = [{"attributes": {"type": "Lead", "url": f"/services/data/{API_VERSION}/sobjects/Lead/{r['Id']}"}, **{f: r.get(f) for f in fields}} for r in chunk]
        done = offset + batch >= len(rows)
        body: dict = {"totalSize": len(rows) + (number - 1 if self.duplicate_on_page else 0), "done": done, "records": records}
        if self.total_override is not None:
            body["totalSize"] = self.total_override
        if not done:
            body["nextRecordsUrl"] = self.next_url_override or f"/services/data/{API_VERSION}/query/{locator}-{offset + batch}"
        return self._send(h, 200, body)


def main() -> int:
    """Serve the sample org's leads until interrupted: python -m lakehouse.stand_in --port 8765"""
    import argparse
    from datetime import date

    from .sample_data import SF_ORG_ID, build_world

    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 1))
    ap.add_argument("--batch", type=int, default=200)
    a = ap.parse_args()
    world = build_world(a.seed, a.days, a.end)
    with StandInSalesforce(world.leads[SF_ORG_ID], default_batch=a.batch) as si:
        print("stand-in Salesforce on", si.url, "token stand-in-token, org", SF_ORG_ID, flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
