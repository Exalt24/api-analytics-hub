"""The QuickBooks Online connector against its stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the request the connector builds (the realm path, the Bearer and Accept headers, the query text with its half open
date range, the stable order and the position and size), how it reads the documented response (`QueryResponse.Invoice`, absent when empty, JSON numbers read as exact
decimals, `CurrencyRef`, a voided invoice), the money, currency and voided rules, the error taxonomy by `Fault` type and status, that retries are bounded, and that the
stand-in refuses what the saved pages say Intuit refuses. They do not prove Intuit accepts the request: the server is written from the saved pages
(docs/AD_AND_BUSINESS_SOURCES.md) and no company or sandbox was used.

Every refusal runs a positive control first (the same call succeeds).
"""
from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors.base import AuthExpired, AuthRevoked, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.fake_http import Fault as _Fault  # noqa: E402
from app.connectors.quickbooks import QuickBooksConnector  # noqa: E402
from app.connectors.quickbooks_fake import FakeQuickBooks, fault_body  # noqa: E402
from app.connectors.registry import registry  # noqa: E402
from vendor_helpers import Sleeps, client, collect, points_of, retrier, run  # noqa: E402

def Fault(status, body=None, headers=None, drop=False):  # noqa: N802 - reads as the class it wraps
    """A scripted fault; a text body is sent as the raw bytes it is, not as a JSON string."""
    return _Fault(status, body.encode() if isinstance(body, str) else body, headers or {}, drop)


REALM = "9130350000000001"
TOKEN = "eyJlbmMi-SECRET-qb-token"
START, END = date(2026, 10, 1), date(2026, 10, 4)


@pytest.fixture()
def server():
    with FakeQuickBooks(token=TOKEN) as fake:
        fake.set_company(REALM, name="Fort Myers RV Repair")
        yield fake


def make(server, sleeps=None, **kw):
    kw.setdefault("access_token", TOKEN)
    kw.setdefault("retrier", retrier(sleeps))
    return QuickBooksConnector(realm_id=kw.pop("realm_id", REALM), base_url=server.url, client=client(), **kw)


def by_key(pages):
    return {(p.metric_key, p.observed_on.day): p.value_numeric for p in points_of(pages)}


# ---------------------------------------------------------------- the happy path

def test_invoices_are_counted_per_invoice_date_with_total_and_open_balance_as_exact_minor_units(server):
    server.add_invoice(REALM, "1", "2026-10-01", "362.07", balance="100.00")
    server.add_invoice(REALM, "2", "2026-10-01", "0.10", balance="0.10")
    server.add_invoice(REALM, "3", "2026-10-03", "1000", balance="0")
    pages = run(collect(make(server), START, END))
    got = by_key(pages)
    assert got[("invoices", 1)] == 2 and got[("invoiced_amount", 1)] == 36217 and got[("invoice_open_balance", 1)] == 10010
    assert got[("invoices", 2)] == 0 and got[("invoiced_amount", 2)] == 0
    assert got[("invoices", 3)] == 1 and got[("invoiced_amount", 3)] == 100000 and got[("invoice_open_balance", 3)] == 0
    assert all(type(p.value_numeric) is int for p in points_of(pages)) and len(points_of(pages)) == 9
    assert {(p.metric_key, p.currency) for p in points_of(pages)} == {("invoices", None), ("invoiced_amount", "USD"), ("invoice_open_balance", "USD")}


def test_a_sum_of_tenths_is_exact_because_the_json_numbers_are_read_as_decimals_not_floats(server):
    for i in range(30):
        server.add_invoice(REALM, str(i + 1), "2026-10-01", "0.10")
    assert by_key(run(collect(make(server), START, END)))[("invoiced_amount", 1)] == 300


def test_the_request_is_the_documented_one(server):
    run(collect(make(server, minor_version=75), START, END))
    seen = server.requests[-1]
    assert seen.method == "GET" and seen.path == "/v3/company/%s/query" % REALM
    assert seen.headers["authorization"] == "Bearer " + TOKEN and seen.headers["accept"] == "application/json"
    assert seen.query["query"] == ("SELECT * FROM Invoice WHERE TxnDate >= '2026-10-01' AND TxnDate < '2026-10-04' "
                                   "ORDERBY TxnDate, Id STARTPOSITION 1 MAXRESULTS 1000")
    assert seen.query["minorversion"] == "75"
    run(collect(make(server), START, END))
    assert "minorversion" not in server.requests[-1].query


def test_a_voided_invoice_is_not_an_invoice_the_shop_issued(server):
    server.add_invoice(REALM, "1", "2026-10-01", "50.00")
    server.add_invoice(REALM, "2", "2026-10-01", "999.00", voided=True)
    got = by_key(run(collect(make(server), START, END)))
    assert got[("invoices", 1)] == 1 and got[("invoiced_amount", 1)] == 5000


def test_a_zero_total_invoice_that_is_not_voided_still_counts(server):
    server.add_invoice(REALM, "1", "2026-10-01", "0")
    assert by_key(run(collect(make(server), START, END)))[("invoices", 1)] == 1


def test_a_window_with_no_invoice_writes_counts_of_zero_and_money_only_with_a_known_currency(server):
    pages = run(collect(make(server), START, END))
    assert {k[0] for k in by_key(pages)} == {"invoices"} and set(by_key(pages).values()) == {0}
    configured = run(collect(make(server, currency="USD"), START, END))
    assert len(points_of(configured)) == 9 and {p.currency for p in points_of(configured) if p.currency} == {"USD"}


def test_a_configured_currency_refuses_an_invoice_in_another_one(server):
    server.add_invoice(REALM, "1", "2026-10-01", "5", currency="CAD")
    assert run(collect(make(server), START, END))                                    # learned, not configured: accepted
    with pytest.raises(UpstreamBroken, match="two currencies"):
        run(collect(make(server, currency="USD"), START, END))
    with pytest.raises(ValueError):
        QuickBooksConnector(realm_id="1", access_token="t", currency="dollars")


def test_a_zero_decimal_company_currency_is_not_scaled(server):
    server.add_invoice(REALM, "1", "2026-10-01", "1500", currency="JPY")
    assert by_key(run(collect(make(server), START, END)))[("invoiced_amount", 1)] == 1500


# ---------------------------------------------------------------- paging

def test_a_result_longer_than_the_page_is_read_by_start_position_in_a_stable_order(server, monkeypatch):
    server.max_results = 2
    monkeypatch.setattr(__import__("app.connectors.quickbooks", fromlist=["x"]), "MAX_RESULTS", 2)
    for i in range(5):
        server.add_invoice(REALM, "%02d" % i, "2026-10-0%d" % (1 + i % 3), "10.00")
    pages = run(collect(make(server), START, END))
    got = by_key(pages)
    assert got[("invoices", 1)] + got[("invoices", 2)] + got[("invoices", 3)] == 5 and got[("invoiced_amount", 1)] == 2000
    queries = [r.query["query"] for r in server.requests]
    assert [q.split("STARTPOSITION ")[1] for q in queries] == ["1 MAXRESULTS 2", "3 MAXRESULTS 2", "5 MAXRESULTS 2"]
    assert [len(p.points) for p in pages] == [0, 0, 9]


def test_an_invoice_that_appears_on_two_pages_is_refused_not_counted_twice(server, monkeypatch):
    one = {"Id": "1", "TxnDate": "2026-10-01", "TotalAmt": Decimal("1.00"), "Balance": Decimal("1.00"), "CurrencyRef": {"value": "USD"}}
    monkeypatch.setattr(__import__("app.connectors.quickbooks", fromlist=["x"]), "MAX_RESULTS", 1)
    server.faults[:] = [Fault(200, json.dumps({"QueryResponse": {"Invoice": [one]}}, default=float)),
                        Fault(200, json.dumps({"QueryResponse": {"Invoice": [one]}}, default=float))]
    with pytest.raises(UpstreamBroken, match="appears twice"):
        run(collect(make(server), START, END))


def test_a_runaway_response_is_stopped_at_the_page_bound(server, monkeypatch):
    import app.connectors.quickbooks as qb
    monkeypatch.setattr(qb, "MAX_RESULTS", 1)
    monkeypatch.setattr(qb, "MAX_PAGES", 3)
    for i in range(5):
        one = {"Id": str(i), "TxnDate": "2026-10-01", "TotalAmt": 1, "Balance": 1, "CurrencyRef": {"value": "USD"}}
        server.faults.append(Fault(200, json.dumps({"QueryResponse": {"Invoice": [one]}})))
    with pytest.raises(UpstreamBroken, match="more than 3 pages"):
        run(collect(make(server), START, END))


def test_there_is_no_resume_cursor_so_one_passed_in_is_refused_not_ignored(server):
    with pytest.raises(ValueError, match="no cursor"):
        run(collect(make(server), START, END, cursor="anything"))


# ---------------------------------------------------------------- errors

@pytest.mark.parametrize("http,kind,exc", [
    (401, "AuthenticationFault", AuthExpired), (200, "AuthenticationFault", AuthExpired), (403, "AuthorizationFault", AuthRevoked),
    (400, "ValidationFault", UpstreamBroken), (200, "ValidationFault", UpstreamBroken)])
def test_a_fault_that_retrying_cannot_fix_is_a_typed_error_at_once_even_inside_a_200(server, http, kind, exc):
    assert run(collect(make(server), START, END))                                    # positive control
    server.faults.append(Fault(http, fault_body(kind, "100", "scripted")))
    sleeps = Sleeps()
    with pytest.raises(exc):
        run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == []


def test_a_fault_inside_query_response_is_read_the_same_way(server):
    server.faults.append(Fault(200, {"QueryResponse": {"Fault": fault_body("AuthenticationFault", "3200", "x")["Fault"]}}))
    with pytest.raises(AuthExpired):
        run(collect(make(server), START, END))


def test_an_http_status_with_no_fault_is_read_by_its_status(server):
    for http, exc in ((401, AuthExpired), (403, AuthRevoked), (400, UpstreamBroken), (404, UpstreamBroken)):
        server.faults.append(Fault(http, {"message": "no fault here"}))
        with pytest.raises(exc):
            run(collect(make(server), START, END))


@pytest.mark.parametrize("http,body", [(500, {"message": "x"}), (503, {"message": "x"}), (200, fault_body("SystemFault", "10000", "x"))])
def test_a_system_fault_or_server_error_is_retried_with_a_bound_and_then_succeeds(server, http, body):
    server.faults.append(Fault(http, body))
    sleeps = Sleeps()
    assert run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == [1.0]


def test_a_429_means_wait_sixty_seconds_which_is_longer_than_the_cap_so_the_runner_owns_it(server):
    server.faults.append(Fault(429, {"Fault": {"Error": [{"Message": "ThrottleExceeded", "code": "003001"}], "type": "ThrottleFault"}}))
    sleeps = Sleeps()
    with pytest.raises(RateLimited) as caught:
        run(collect(make(server, sleeps), START, END))
    assert caught.value.retry_after_seconds == 60.0 and sleeps.calls == []


def test_a_429_is_slept_when_the_cap_allows_and_a_retry_after_header_wins(server):
    server.faults.append(Fault(429, {"message": "x"}))
    sleeps = Sleeps()
    from app.connectors.common import Retrier
    r = Retrier(sleep=sleeps, rng=lambda: 1.0, backoff_cap=120.0)
    assert run(collect(make(server, retrier=r), START, END))
    assert sleeps.calls == [60.0]
    server.faults.append(Fault(429, {"message": "x"}, {"Retry-After": "11"}))
    sleeps.calls.clear()
    assert run(collect(make(server, retrier=r), START, END))
    assert sleeps.calls == [11.0]


def test_retries_stop_at_the_budget_with_a_rate_limit_for_the_runner(server):
    for _ in range(3):
        server.faults.append(Fault(503, {"message": "x"}))
    sleeps = Sleeps()
    with pytest.raises(RateLimited, match="gave up after 3 attempts"):
        run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == [1.0, 2.0]


@pytest.mark.parametrize("body", [b"<html>502</html>", b"[]", b'"text"', b"null", b""])
def test_a_200_that_is_not_a_json_object_is_broken_not_empty(server, body):
    server.faults.append(Fault(200, body))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server), START, END))


def test_a_dropped_connection_is_a_transport_failure_that_is_retried(server):
    server.faults.append(Fault(0, drop=True))
    sleeps = Sleeps()
    assert run(collect(make(server, sleeps), START, END))
    assert sleeps.calls == [1.0]


def test_a_bad_token_is_an_expired_credential_and_never_appears_in_the_message(server):
    with pytest.raises(AuthExpired) as caught:
        run(collect(make(server, access_token="wrong-SECRET"), START, END))
    assert "wrong-SECRET" not in str(caught.value) and TOKEN not in str(caught.value)


def test_a_company_the_token_cannot_read_is_a_revoked_credential(server):
    with pytest.raises(AuthRevoked):
        run(collect(make(server, realm_id="123"), START, END))


# ---------------------------------------------------------------- shapes that must not become figures

def invoice(**over):
    out = {"Id": "1", "TxnDate": "2026-10-01", "TotalAmt": Decimal("10.00"), "Balance": Decimal("5.00"), "CurrencyRef": {"value": "USD"}}
    out.update(over)
    return out


def serve(server, *invoices, key="Invoice"):
    server.faults.append(Fault(200, json.dumps({"QueryResponse": {key: list(invoices)}}, default=float)))


@pytest.mark.parametrize("mutate", [
    {"Id": None}, {"Id": ""}, {"Id": 7}, {"TxnDate": None}, {"TxnDate": "10/01/2026"}, {"TxnDate": "2026-02-30"}, {"TxnDate": "2026-09-30"}, {"TxnDate": "2026-10-04"},
    {"CurrencyRef": None}, {"CurrencyRef": {}}, {"CurrencyRef": {"value": "usd"}}, {"CurrencyRef": {"value": 5}},
    {"TotalAmt": None}, {"TotalAmt": "abc"}, {"TotalAmt": -1}, {"TotalAmt": True}, {"TotalAmt": [1]}, {"Balance": "abc"}, {"Balance": -3}, {"Balance": True},
])
def test_an_invoice_that_is_not_what_the_pages_describe_is_refused_after_a_positive_control(server, mutate):
    serve(server, invoice())
    assert run(collect(make(server), START, END))                                    # positive control
    serve(server, invoice(**mutate))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server), START, END))


def test_a_missing_balance_reads_as_no_open_balance_and_a_non_list_invoice_key_is_refused(server):
    out = invoice()
    del out["Balance"]
    serve(server, out)
    assert by_key(run(collect(make(server), START, END)))[("invoice_open_balance", 1)] == 0
    server.faults.append(Fault(200, json.dumps({"QueryResponse": {"Invoice": {"Id": "1"}}})))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server), START, END))
    server.faults.append(Fault(200, json.dumps({"QueryResponse": [1]})))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server), START, END))
    server.faults.append(Fault(200, json.dumps({"QueryResponse": {"Invoice": [5]}})))
    with pytest.raises(UpstreamBroken):
        run(collect(make(server), START, END))


def test_two_currencies_in_one_window_are_refused(server):
    serve(server, invoice(), invoice(Id="2", CurrencyRef={"value": "CAD"}))
    with pytest.raises(UpstreamBroken, match="two currencies"):
        run(collect(make(server), START, END))


# ---------------------------------------------------------------- the company, the registry, construction

def test_verify_reads_the_company_info(server):
    conn = make(server)
    assert run(_verify(conn)) == "QuickBooks company %s (Fort Myers RV Repair)" % REALM and conn.company == "Fort Myers RV Repair"
    server.faults.append(Fault(200, {"CompanyInfo": {}}))
    with pytest.raises(UpstreamBroken):
        run(_verify(make(server)))


async def _verify(conn):
    try:
        return await conn.verify()
    finally:
        await conn._client.aclose()


def test_the_realm_id_is_validated_before_any_request():
    for bad in ("", "abc", "12 3", "1" * 40):
        with pytest.raises(ValueError):
            QuickBooksConnector(realm_id=bad, access_token="t")
    for bad in (0, -1, True, "75"):
        with pytest.raises(ValueError):
            QuickBooksConnector(realm_id="1", access_token="t", minor_version=bad)
    with pytest.raises(ValueError):
        QuickBooksConnector(realm_id="1")
    assert QuickBooksConnector(realm_id=" 77 ", access_token="t").external_account == "77"


def test_a_window_with_no_days_is_refused_and_the_token_never_shows_in_a_repr():
    conn = QuickBooksConnector(realm_id="1", access_token=TOKEN)
    assert TOKEN not in repr(conn)
    with pytest.raises(ValueError):
        run(collect(conn, START, START))


def test_from_env_reads_the_documented_variables_and_names_what_is_missing():
    conn = QuickBooksConnector.from_env({"QUICKBOOKS_REALM_ID": "5", "QUICKBOOKS_ACCESS_TOKEN": "t", "QUICKBOOKS_API_BASE_URL": "http://x",
                                         "QUICKBOOKS_CURRENCY": "USD"})
    assert conn.realm_id == "5" and conn._base == "http://x" and conn.currency == "USD"
    with pytest.raises(ValueError, match="QUICKBOOKS_REALM_ID and QUICKBOOKS_ACCESS_TOKEN"):
        QuickBooksConnector.from_env({})


def test_the_registry_describes_the_platform_and_redacts_the_token():
    spec = registry.get("quickbooks")
    assert spec.supported_metrics == frozenset({"invoices", "invoiced_amount", "invoice_open_balance"})
    assert registry.redact("quickbooks", {"realm_id": "1", "access_token": "x"}) == {"realm_id": "1", "access_token": "***"}
    with pytest.raises(ValueError, match="missing credentials"):
        registry.build("quickbooks", realm_id="1")


# ---------------------------------------------------------------- the stand-in refuses what the pages say Intuit refuses

def query(server, text):
    return httpx.get("%s/v3/company/%s/query" % (server.url, REALM), params={"query": text}, headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json"},
                     timeout=5, trust_env=False)


GOOD = "SELECT * FROM Invoice WHERE TxnDate >= '2026-10-01' AND TxnDate < '2026-10-04' ORDERBY TxnDate, Id STARTPOSITION 1 MAXRESULTS 10"


@pytest.mark.parametrize("text,code", [
    ("SELECT * FROM Customer WHERE TxnDate >= '2026-10-01' AND TxnDate < '2026-10-04' ORDERBY TxnDate, Id STARTPOSITION 1 MAXRESULTS 10", "100"),
    (GOOD.replace("2026-10-04", "not-a-date"), "300"), (GOOD.replace("MAXRESULTS 10", "MAXRESULTS 1001"), "4001"), (GOOD.replace("STARTPOSITION 1", "STARTPOSITION 0"), "4001"),
    ("SELECT Id FROM Invoice", "4000"), (GOOD.replace("AND", "OR"), "4000")])
def test_what_the_pages_say_is_refused_is_refused_with_a_validation_fault(server, text, code):
    assert query(server, GOOD).status_code == 200                                    # positive control
    response = query(server, text)
    assert response.status_code == 400 and response.json()["Fault"]["type"] == "ValidationFault" and response.json()["Fault"]["Error"][0]["code"] == code


def test_the_stand_in_answers_json_only_and_rejects_a_bad_token_a_wrong_realm_and_an_unknown_path(server):
    assert httpx.get("%s/v3/company/%s/query" % (server.url, REALM), params={"query": GOOD}, headers={"Authorization": "Bearer " + TOKEN},
                     timeout=5, trust_env=False).status_code == 400
    assert httpx.get("%s/v3/company/%s/query" % (server.url, REALM), params={"query": GOOD}, headers={"Accept": "application/json"},
                     timeout=5, trust_env=False).status_code == 401
    assert httpx.get(server.url + "/v2/company/1/query", timeout=5, trust_env=False).status_code == 404


def test_the_stand_in_leaves_invoice_out_when_nothing_matches_and_pages_by_position(server):
    assert "Invoice" not in query(server, GOOD).json()["QueryResponse"]
    for i in range(3):
        server.add_invoice(REALM, str(i), "2026-10-01", "1")
    first = query(server, GOOD.replace("MAXRESULTS 10", "MAXRESULTS 2")).json()["QueryResponse"]
    second = query(server, GOOD.replace("MAXRESULTS 10", "MAXRESULTS 2").replace("STARTPOSITION 1", "STARTPOSITION 3")).json()["QueryResponse"]
    assert [i["Id"] for i in first["Invoice"]] == ["0", "1"] and [i["Id"] for i in second["Invoice"]] == ["2"] and first["maxResults"] == 2
