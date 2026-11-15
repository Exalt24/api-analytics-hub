"""A paged reader for the Salesforce REST query endpoint, standard library only.

Written from Salesforce's documentation as quoted in docs/AD_AND_BUSINESS_SOURCES.md: GET /services/data/vXX.X/query?q=<SOQL> with a bearer token,
`totalSize`, `done`, `records` and `nextRecordsUrl`; "request the next batch of records and repeat until all records have been retrieved. These requests
use nextRecordsUrl, and don't include any parameters". It follows each next URL as given, refuses one that is not a path under /services/data/ (the
token must not go to another host), refuses a loop, checks `totalSize` against what arrived and retries a 5xx or 429 a bounded number of times.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

API_VERSION = "v62.0"
LEAD_FIELDS = ("Id", "CreatedDate", "LastModifiedDate", "LeadSource", "Status", "IsConverted", "ConvertedDate")


class RestError(Exception):
    """The read could not be completed. `records` holds what arrived before it and `rejects` the raw text of any page that could not be read."""

    def __init__(self, message: str, records: list | None = None, rejects: list | None = None):
        super().__init__(message)
        self.records = records or []
        self.rejects = rejects or []


@dataclass
class Fetched:
    records: list[dict] = field(default_factory=list)
    pages: int = 0
    api_calls: int = 0


def soql_datetime(text: str) -> str:
    """A Salesforce datetime text (2026-10-01T12:34:56.000+0000) as a SOQL literal (2026-10-01T12:34:56Z), which is unquoted."""
    moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def lead_query(since_literal: str | None, fields: tuple[str, ...] = LEAD_FIELDS) -> str:
    where = f" WHERE LastModifiedDate >= {since_literal}" if since_literal else ""
    return f"SELECT {', '.join(fields)} FROM Lead{where} ORDER BY LastModifiedDate, Id"


def _get(url: str, token: str, headers: dict, timeout: float, retries: int, backoff: float, counter: list[int]) -> str:
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json", **headers})
    last = ""
    for attempt in range(retries + 1):
        counter[0] += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - the address is the caller's own
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in (429, 500, 502, 503) and attempt < retries:
                last = f"HTTP {exc.code}"
                time.sleep(backoff * (2 ** attempt))
                continue
            raise RestError(f"HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries:
                last = str(exc)
                time.sleep(backoff * (2 ** attempt))
                continue
            raise RestError("could not reach the server: " + str(exc)) from exc
    raise RestError("gave up after retries: " + last)


def fetch_query(base_url: str, token: str, soql: str, *, batch_size: int = 200, timeout: float = 15.0, retries: int = 2, backoff: float = 0.2,
                max_pages: int = 10_000, api_version: str = API_VERSION) -> Fetched:
    out = Fetched()
    counter = [0]
    headers = {"Sforce-Query-Options": f"batchSize={batch_size}"}
    path = f"/services/data/{api_version}/query?" + urllib.parse.urlencode({"q": soql})
    seen: set[str] = set()
    total = None
    while True:
        if not path.startswith("/services/data/") or ".." in path:
            raise RestError(f"refusing next page address {path!r}: it is not a path under /services/data/", out.records)
        if path in seen:
            raise RestError("the next page address repeats; paging does not advance", out.records)
        seen.add(path)
        if len(seen) > max_pages:
            raise RestError("more pages than the cap", out.records)
        text = _get(base_url.rstrip("/") + path, token, headers, timeout, retries, backoff, counter)
        out.api_calls = counter[0]
        out.pages += 1
        try:
            body = json.loads(text)
        except ValueError as exc:
            raise RestError(f"page {out.pages} is not valid JSON ({exc}); {len(out.records)} records were read before it", out.records,
                            [(text, f"page {out.pages} is not valid JSON")]) from exc
        if not isinstance(body, dict) or not isinstance(body.get("records"), list) or "done" not in body or "totalSize" not in body:
            raise RestError(f"page {out.pages} is not a query result (no records, done and totalSize)", out.records, [(text, f"page {out.pages} is not a query result")])
        total = body["totalSize"]
        out.records.extend(body["records"])
        if body["done"]:
            if body.get("nextRecordsUrl"):
                raise RestError("the query says done but names a next page", out.records)
            break
        nxt = body.get("nextRecordsUrl")
        if not nxt:
            raise RestError("the query is not done and names no next page", out.records)
        path = nxt
    if total != len(out.records):
        raise RestError(f"totalSize says {total} records but {len(out.records)} arrived", out.records)
    return out
