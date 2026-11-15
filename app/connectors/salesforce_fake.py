"""A stand-in Salesforce REST API server on 127.0.0.1, for tests.

It speaks real HTTP to the connector and implements what the saved pages describe (docs/AD_AND_BUSINESS_SOURCES.md, section Salesforce):

  * `POST /services/oauth2/token` with `grant_type=refresh_token`, the client id and secret and the refresh token in the BODY, answering `access_token`,
    `instance_url`, `token_type`, `scope`, `id`, `issued_at`, `signature` and no expiry; with `rotate=True` also a new `refresh_token` and the old one is dead;
    `{"error": "invalid_grant", "error_description": "expired access/refresh token"}` for a refresh token it does not know.
  * `GET /services/data/vXX.X/query?q=` with a SOQL subset (below), `totalSize`, `done`, `records[]` (each with `attributes`), at most the batch size named in
    `Sforce-Query-Options: batchSize=N` per page, and `nextRecordsUrl` until `done`.
  * `GET /services/data/vXX.X/sobjects/{Object}/describe` with the object's fields (name, type, referenceTo).
  * `Sforce-Limit-Info: api-usage=used/limit` on every data answer, and a 403 `REQUEST_LIMIT_EXCEEDED` once the limit is passed.
  * The error body is a list of `{message, errorCode}`.

THE SOQL SUBSET it understands: `SELECT <fields> FROM Lead|Opportunity [WHERE <conditions joined by AND>] [ORDER BY <fields>]`, a condition being
`Field >= value`, `Field < value`, `Field <= value`, `Field > value` or `Field = true|false`, with an UNQUOTED date (`2026-10-01`) or datetime
(`2026-10-01T00:00:00Z`) literal, as the SOQL date format page gives them. A quoted date literal, an unknown object (INVALID_TYPE), an unknown or hidden field
(INVALID_FIELD) and anything else it cannot read (MALFORMED_QUERY) answer 400, as Salesforce does.

It is a stand-in written from documentation, not a recording of Salesforce. Its own choices, marked UNVERIFIED in the sources page: the `nextRecordsUrl` form
(`/services/data/vXX.X/query/<locator>-<offset>`), the batch size floor (a test may lower it below the documented 200), the 401 body's message, the
access token's shape, the HTTP status of a bad token request, and the order of the fields.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

from .fake_http import Answer, FakeHTTP, Seen

_QUERY = re.compile(r"^\s*SELECT\s+(?P<fields>.+?)\s+FROM\s+(?P<obj>\w+)(?:\s+WHERE\s+(?P<where>.+?))?(?:\s+ORDER\s+BY\s+(?P<order>.+?))?\s*$", re.IGNORECASE | re.DOTALL)
_COND = re.compile(r"^(?P<field>\w+)\s*(?P<op>>=|<=|>|<|=)\s*(?P<value>\S+)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")

FIELD_TYPES = {
    "Lead": {"Id": "id", "CreatedDate": "datetime", "IsConverted": "boolean", "ConvertedDate": "date", "ConvertedOpportunityId": "reference",
             "Email": "email", "Phone": "phone", "MobilePhone": "phone", "LeadSource": "picklist", "Status": "picklist"},
    "Opportunity": {"Id": "id", "CreatedDate": "datetime", "IsWon": "boolean", "IsClosed": "boolean", "CloseDate": "date", "Amount": "currency",
                    "StageName": "picklist", "AccountId": "reference", "ContactId": "reference", "CampaignId": "reference"},
}


def error_body(code: str, message: str = "stand-in") -> list:
    return [{"message": message, "errorCode": code}]


def sf_datetime(moment: datetime) -> str:
    """The JSON form Salesforce writes a datetime in: `2026-10-01T12:34:56.000+0000`."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def sf_id(prefix: str, n: int) -> str:
    return "%s%015d" % (prefix, n)


class FakeSalesforce(FakeHTTP):
    def __init__(self, *, client_id: str = "client-id", client_secret: str = "client-secret", refresh_token: str = "refresh-token-1",
                 api_version: str = "v62.0", api_limit: int = 100000, api_used: int = 0, rotate: bool = False, min_batch: int = 1,
                 multicurrency: bool = False, hidden_fields: tuple[str, ...] = (), dead_tokens: bool = False):
        super().__init__()
        self.client_id, self.client_secret = client_id, client_secret
        self.refresh_tokens = {refresh_token}
        self.api_version = api_version
        self.api_limit, self.api_used = api_limit, api_used
        self.rotate = rotate
        self.min_batch = min_batch
        self.multicurrency = multicurrency
        self.hidden_fields = set(hidden_fields)
        #: True makes every token it issues refused at once, a token the data API never accepts.
        self.dead_tokens = dead_tokens
        self.access_tokens: set[str] = set()
        self.leads: list[dict[str, Any]] = []
        self.opportunities: list[dict[str, Any]] = []
        self._locators: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------ data

    def add_lead(self, n: int, created: datetime, *, email: str | None = None, phone: str | None = None, mobile: str | None = None,
                 source: str | None = None, converted_on: date | None = None) -> str:
        ident = sf_id("00Q", n)
        with self._lock:
            self.leads.append({"Id": ident, "CreatedDate": created, "Email": email, "Phone": phone, "MobilePhone": mobile, "LeadSource": source,
                               "IsConverted": converted_on is not None, "ConvertedDate": converted_on, "ConvertedOpportunityId": None, "Status": "Open"})
        return ident

    def add_opportunity(self, n: int, created: datetime, *, close: date, won: bool, amount: Decimal | int | None, currency: str = "USD") -> str:
        ident = sf_id("006", n)
        with self._lock:
            self.opportunities.append({"Id": ident, "CreatedDate": created, "IsWon": won, "IsClosed": won, "CloseDate": close, "Amount": amount, "StageName": "Closed Won" if won else "Prospecting",
                                       "CurrencyIsoCode": currency, "AccountId": None, "ContactId": None, "CampaignId": None})
        return ident

    def expire_access_tokens(self) -> None:
        with self._lock:
            self.access_tokens.clear()

    def issue_access_token(self) -> str:
        token = "00D%s!AR%s" % (secrets.token_hex(6).upper(), secrets.token_urlsafe(24))
        if not self.dead_tokens:
            self.access_tokens.add(token)
        return token

    def fields_of(self, obj: str) -> dict[str, str]:
        fields = dict(FIELD_TYPES[obj])
        if obj == "Opportunity" and self.multicurrency:
            fields["CurrencyIsoCode"] = "picklist"
        return {k: v for k, v in fields.items() if k not in self.hidden_fields}

    # ------------------------------------------------------------------ routing

    def answer(self, seen: Seen) -> Answer:
        if seen.path == "/services/oauth2/token":
            return self._token(seen)
        if not seen.path.startswith("/services/data/%s/" % self.api_version) or seen.method != "GET":
            return 404, {}, error_body("NOT_FOUND", "The requested resource does not exist")
        if seen.headers.get("authorization", "")[7:] not in self.access_tokens or not seen.headers.get("authorization", "").startswith("Bearer "):
            return 401, {}, error_body("INVALID_SESSION_ID", "Session expired or invalid")
        if self.api_used >= self.api_limit:
            return 403, {}, error_body("REQUEST_LIMIT_EXCEEDED", "TotalRequests Limit exceeded.")
        self.api_used += 1
        limit = {"Sforce-Limit-Info": "api-usage=%d/%d" % (self.api_used, self.api_limit)}
        rest = seen.path[len("/services/data/%s/" % self.api_version):]
        m = re.match(r"^sobjects/(\w+)/describe/?$", rest)
        if m:
            return self._describe(m.group(1), limit)
        if rest == "query":
            return self._query(seen, limit)
        m = re.match(r"^query/(\w+)-(\d+)$", rest)
        if m:
            return self._more(m.group(1), int(m.group(2)), seen, limit)
        return 404, limit, error_body("NOT_FOUND", "The requested resource does not exist")

    def _token(self, seen: Seen) -> Answer:
        form = {k: v[0] for k, v in parse_qs(seen.body.decode("utf-8"), keep_blank_values=True).items()}
        if seen.method != "POST" or seen.query or form.get("grant_type") != "refresh_token":
            return 400, {}, {"error": "unsupported_grant_type", "error_description": "grant type not supported"}
        if form.get("client_id") != self.client_id:
            return 400, {}, {"error": "invalid_client_id", "error_description": "client identifier invalid"}
        if form.get("client_secret") != self.client_secret:
            return 400, {}, {"error": "invalid_client", "error_description": "invalid client credentials"}
        if form.get("refresh_token") not in self.refresh_tokens:
            return 400, {}, {"error": "invalid_grant", "error_description": "expired access/refresh token"}
        body: dict[str, Any] = {"access_token": self.issue_access_token(), "instance_url": self.url, "id": "https://login.salesforce.com/id/00Dx0000000BV7z/005x00000012Q9P",
                                "token_type": "Bearer", "issued_at": "1278448384422", "signature": "SSSbLO/gBhmmyNUvN18ODBDFYHzakxOMgqYtu+hDPsc=", "scope": "api refresh_token"}
        if self.rotate:
            self.refresh_tokens.discard(form["refresh_token"])
            new = "refresh-" + secrets.token_hex(8)
            self.refresh_tokens.add(new)
            body["refresh_token"] = new
        return 200, {}, body

    def _describe(self, obj: str, limit: dict) -> Answer:
        if obj not in FIELD_TYPES:
            return 404, limit, error_body("NOT_FOUND", "The requested resource does not exist")
        fields = []
        for name, kind in self.fields_of(obj).items():
            refers = ["Opportunity"] if name == "ConvertedOpportunityId" else ["Account"] if name == "AccountId" else []
            fields.append({"name": name, "type": kind, "referenceTo": refers, "nillable": name not in ("Id", "CreatedDate")})
        return 200, limit, {"name": obj, "fields": fields}

    # ------------------------------------------------------------------ SOQL

    def _literal(self, kind: str, text: str) -> Any:
        if kind == "boolean":
            if text not in ("true", "false"):
                raise ValueError("boolean")
            return text == "true"
        if kind == "date":
            if not _DATE.match(text):
                raise ValueError("date")
            return date.fromisoformat(text)
        if kind == "datetime":
            if not _DATETIME.match(text):
                raise ValueError("datetime")
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        raise ValueError("kind")

    def _run(self, soql: str) -> tuple[str, list[str], list[dict]] | tuple[int, list]:
        m = _QUERY.match(soql)
        if not m:
            return 400, error_body("MALFORMED_QUERY", "unexpected token")
        obj = m.group("obj")
        table = {"Lead": self.leads, "Opportunity": self.opportunities}.get(obj)
        if table is None:
            return 400, error_body("INVALID_TYPE", "sObject type '%s' is not supported." % obj)
        known = self.fields_of(obj)
        selected = [f.strip() for f in m.group("fields").split(",")]
        for name in selected + ([f.strip() for f in m.group("order").split(",")] if m.group("order") else []):
            if name not in known:
                return 400, error_body("INVALID_FIELD", "No such column '%s' on entity '%s'." % (name, obj))
        conds = []
        for text in re.split(r"\s+AND\s+", m.group("where") or "", flags=re.IGNORECASE) if m.group("where") else []:
            c = _COND.match(text.strip())
            if not c or c.group("field") not in known:
                return 400, error_body("INVALID_FIELD" if c else "MALFORMED_QUERY", "cannot read the condition")
            try:
                conds.append((c.group("field"), c.group("op"), self._literal(known[c.group("field")], c.group("value"))))
            except ValueError:
                return 400, error_body("MALFORMED_QUERY", "value of filter criterion for field '%s' must be of type %s and should not be enclosed in quotes" % (c.group("field"), known[c.group("field")]))
        ops = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b, "=": lambda a, b: a == b}
        rows = [r for r in table if all(r[f] is not None and ops[op](r[f], v) for f, op, v in conds)]
        for name in reversed([f.strip() for f in m.group("order").split(",")] if m.group("order") else []):
            rows.sort(key=lambda r, n=name: (r[n] is None, r[n]))
        return obj, selected, rows

    def _shape(self, obj: str, selected: list[str], row: dict) -> dict:
        out: dict[str, Any] = {"attributes": {"type": obj, "url": "/services/data/%s/sobjects/%s/%s" % (self.api_version, obj, row["Id"])}}
        for name in selected:
            value = row[name]
            if isinstance(value, datetime):
                value = sf_datetime(value)
            elif isinstance(value, date):
                value = value.isoformat()
            out[name] = value
        return out

    def _page(self, locator: str, offset: int, batch: int, limit: dict) -> Answer:
        obj, selected, rows = self._locators[locator]       # type: ignore[misc]
        chunk = rows[offset:offset + batch]
        body: dict[str, Any] = {"totalSize": len(rows), "done": offset + batch >= len(rows),
                                "records": [self._shape(obj, selected, r) for r in chunk]}
        if not body["done"]:
            body["nextRecordsUrl"] = "/services/data/%s/query/%s-%d" % (self.api_version, locator, offset + batch)
        # Amount is a JSON number in the real API: a Decimal is written as one (the shortest float repr, exact for the amounts the tests use)
        return 200, limit, json.dumps(body, default=lambda d: float(d) if isinstance(d, Decimal) else str(d)).encode("utf-8")

    def _batch(self, seen: Seen) -> int:
        m = re.search(r"batchSize=(\d+)", seen.headers.get("sforce-query-options", ""))
        return max(self.min_batch, min(int(m.group(1)), 2000)) if m else 2000

    def _query(self, seen: Seen, limit: dict) -> Answer:
        if "q" not in seen.query:
            return 400, limit, error_body("MALFORMED_QUERY", "q is required")
        result = self._run(seen.query["q"])
        if len(result) == 2:
            return result[0], limit, result[1]
        locator = "01g%012d" % len(self._locators)
        self._locators[locator] = result                       # type: ignore[assignment]
        return self._page(locator, 0, self._batch(seen), limit)

    def _more(self, locator: str, offset: int, seen: Seen, limit: dict) -> Answer:
        if locator not in self._locators or seen.query:
            return 404, limit, error_body("NOT_FOUND", "The requested resource does not exist")
        return self._page(locator, offset, self._batch(seen), limit)
