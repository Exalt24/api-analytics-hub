"""A stand-in QuickBooks Online Accounting API server on 127.0.0.1, for tests.

It speaks real HTTP to the connector and implements what the saved pages describe (docs/AD_AND_BUSINESS_SOURCES.md):
`GET /v3/company/{realmId}/query?query=<select>` for the one query shape the connector sends (`SELECT * FROM Invoice WHERE
TxnDate >= '..' AND TxnDate < '..' ORDERBY TxnDate, Id STARTPOSITION n MAXRESULTS m`), the `QueryResponse` with `startPosition`,
`maxResults` and an `Invoice` list that is absent when nothing matches, the 1,000 row maximum, invoices whose `TotalAmt` and
`Balance` are JSON numbers, `CurrencyRef`, a voided invoice whose amounts are zero and `PrivateNote` is "Voided", `GET
.../companyinfo/{realmId}`, the `Fault` object (`type`, `Error[]` with `Message`, `Detail` and `code`) for validation, system,
authentication and authorization faults, and a 429 on throttling.

It is a stand-in written from documentation, not a recording of Intuit. Its own choices, marked UNVERIFIED in the sources page: the JSON
spelling of a fault (the pages show the XML form), the HTTP status it puts on a validation fault (400), the text of its messages, and that it
requires `Accept: application/json`.
"""
from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from .fake_http import Answer, FakeHTTP, Seen

_QUERY_RE = re.compile(
    r"^SELECT \* FROM (?P<entity>\w+) WHERE TxnDate >= '(?P<start>[^']*)' AND TxnDate < '(?P<end>[^']*)' "
    r"ORDERBY TxnDate, Id STARTPOSITION (?P<pos>\d+) MAXRESULTS (?P<max>\d+)$", re.I)
_PATH_RE = re.compile(r"^/v3/company/(\d+)/(query|companyinfo/(\d+))$")
MAX_RESULTS = 1000


def fault_body(kind: str, code: str, message: str, detail: str = "stand-in") -> dict:
    return {"Fault": {"Error": [{"Message": message, "Detail": detail, "code": code}], "type": kind}, "time": "2026-11-06T12:00:00.000-08:00"}


class FakeQuickBooks(FakeHTTP):
    def __init__(self, *, token: str = "test-token", max_results: int = MAX_RESULTS):
        super().__init__()
        self.token = token
        self.max_results = max_results
        #: realm id -> {"name": str, "invoices": [invoice dicts]}
        self.companies: dict[str, dict[str, Any]] = {}

    def set_company(self, realm_id: str, *, name: str = "Stand-in Co") -> None:
        with self._lock:
            self.companies[realm_id] = {"name": name, "invoices": []}

    def add_invoice(self, realm_id: str, invoice_id: str, txn_date: str, total: Decimal | str, *, balance: Decimal | str | None = None,
                    currency: str | None = "USD", voided: bool = False) -> dict:
        total = Decimal(str(total))
        invoice: dict[str, Any] = {
            "Id": invoice_id, "SyncToken": "0", "domain": "QBO", "TxnDate": txn_date, "DocNumber": "D" + invoice_id,
            "TotalAmt": Decimal(0) if voided else total,
            "Balance": Decimal(0) if voided else Decimal(str(balance if balance is not None else total)),
            "CustomerRef": {"value": "1", "name": "Stand-in customer"}, "PrivateNote": "Voided" if voided else "",
        }
        if currency is not None:
            invoice["CurrencyRef"] = {"value": currency, "name": currency}
        with self._lock:
            self.companies[realm_id]["invoices"].append(invoice)
        return invoice

    # ------------------------------------------------------------------ the request

    @staticmethod
    def _json(body: Any, status: int = 200) -> Answer:
        return status, {"intuit_tid": "FAKETID"}, json.dumps(body, default=float).encode()

    def answer(self, seen: Seen) -> Answer:
        match = _PATH_RE.match(seen.path)
        if seen.method != "GET" or not match:
            return self._json(fault_body("ValidationFault", "610", "Object Not Found"), 404)
        if seen.headers.get("authorization") != "Bearer " + self.token:
            return self._json(fault_body("AuthenticationFault", "3200", "message=AuthenticationFailed; errorCode=003200; statusCode=401"), 401)
        company = self.companies.get(match.group(1))
        if company is None:
            return self._json(fault_body("AuthorizationFault", "120", "Authorization failure: realm not authorized"), 403)
        if "application/json" not in seen.headers.get("accept", ""):
            return self._json(fault_body("ValidationFault", "400", "This stand-in answers JSON only"), 400)
        if match.group(2) != "query":
            if match.group(3) != match.group(1):
                return self._json(fault_body("ValidationFault", "610", "Object Not Found"), 400)
            return self._json({"CompanyInfo": {"CompanyName": company["name"], "Country": "US", "Id": match.group(1)}, "time": "2026-11-06T12:00:00.000-08:00"})
        return self._query(company, seen.query.get("query", ""))

    def _query(self, company: Mapping[str, Any], text: str) -> Answer:
        m = _QUERY_RE.match(" ".join(text.split()))
        if not m:
            return self._json(fault_body("ValidationFault", "4000", "Syntax error in the query"), 400)
        if m.group("entity").lower() != "invoice":
            return self._json(fault_body("ValidationFault", "100", "OBJECT name not available"), 400)
        try:
            start, end = date.fromisoformat(m.group("start")), date.fromisoformat(m.group("end"))
        except ValueError:
            return self._json(fault_body("ValidationFault", "300", "Time value is incorrect"), 400)
        position, size = int(m.group("pos")), int(m.group("max"))
        if position < 1 or size < 1 or size > self.max_results:
            return self._json(fault_body("ValidationFault", "4001", "STARTPOSITION and MAXRESULTS must be positive and MAXRESULTS at most %d" % self.max_results), 400)
        rows = [i for i in company["invoices"] if start <= date.fromisoformat(i["TxnDate"]) < end]
        rows.sort(key=lambda i: (i["TxnDate"], i["Id"]))
        page = rows[position - 1: position - 1 + size]
        inner: dict[str, Any] = {"startPosition": position, "maxResults": len(page)}
        if page:
            inner["Invoice"] = page
        return self._json({"QueryResponse": inner, "time": "2026-11-06T12:00:00.000-08:00"})
