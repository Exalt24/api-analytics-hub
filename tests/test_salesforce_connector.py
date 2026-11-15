"""The Salesforce connector against its stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the requests the connector builds (the SOQL text with unquoted date and datetime literals, the Bearer and
Sforce-Query-Options headers, the refresh token flow with its secrets in the body), how it reads the documented answers (totalSize, done, records, nextRecordsUrl,
describe), the day each row lands on (UTC for a datetime, as written for a date), the money and currency rules, the OAuth behaviour (refresh once on a 401, one refresh
for many callers, a rotated refresh token), the limit handling (Sforce-Limit-Info, REQUEST_LIMIT_EXCEEDED), what it refuses, and that retries are bounded. They do not
prove Salesforce accepts any of it: the server is written from the saved pages (docs/AD_AND_BUSINESS_SOURCES.md) and no live org was used.

Every refusal runs a positive control first (the same call succeeds).
"""
from __future__ import annotations

import asyncio
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors.base import AuthExpired, AuthRevoked, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.fake_http import Fault  # noqa: E402
from app.connectors.registry import registry  # noqa: E402
from app.connectors.salesforce import METRICS, SalesforceConnector, parse_datetime_utc  # noqa: E402
from app.connectors.salesforce_fake import FakeSalesforce, error_body  # noqa: E402
from vendor_helpers import Sleeps, client, collect, points_of, retrier, run  # noqa: E402

CLIENT_ID, SECRET, REFRESH = "3MVG9-client-id", "SECRET-consumer-secret", "REFRESH-token-SECRET"
START, END = date(2026, 10, 1), date(2026, 10, 4)


def utc(day, hour=12, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=timezone.utc)


@pytest.fixture()
def server():
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH) as fake:
        yield fake


def make(server, sleeps=None, **kw):
    kw.setdefault("retrier", retrier(sleeps))
    if "access_token" not in kw:
        kw.setdefault("client_id", CLIENT_ID)
        kw.setdefault("client_secret", SECRET)
        kw.setdefault("refresh_token", REFRESH)
    kw.setdefault("login_url", server.url)
    return SalesforceConnector(instance_url=server.url, currency=kw.pop("currency", "USD"), client=client(), **kw)


def warm(conn):
    """Mint the access token now, so a fault queued afterwards lands on the data request and not on the token endpoint."""
    run(conn._tokens.access_token())
    return conn


def by_key(pages):
    return {(p.metric_key, p.observed_on.day): p.value_numeric for p in points_of(pages)}


def queries(server):
    return [r.query["q"] for r in server.requests if r.path.endswith("/query")]


def fetch(server, start=START, end=END, **kw):
    return run(collect(make(server, **kw), start, end))


# ---------------------------------------------------------------- the happy path

def seed(server):
    server.add_lead(1, utc(1, 8), email="a@x.com", phone="+1 212 555 0100", source="Web")
    server.add_lead(2, utc(1, 9), converted_on=date(2026, 10, 2))
    server.add_lead(3, utc(3, 23, 59, 59))
    server.add_lead(4, utc(4, 0, 0, 0))                                       # the window is half open: this is the 4th
    server.add_lead(5, datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc))
    server.add_opportunity(1, utc(1, 10), close=date(2026, 10, 2), won=True, amount=Decimal("1500.25"))
    server.add_opportunity(2, utc(2, 10), close=date(2026, 10, 2), won=True, amount=None)
    server.add_opportunity(3, utc(2, 11), close=date(2026, 10, 3), won=False, amount=Decimal("999"))
    server.add_opportunity(4, utc(3, 11), close=date(2026, 10, 3), won=True, amount=-200)


def test_five_counts_per_day_are_written_and_money_carries_the_currency(server):
    seed(server)
    pages = fetch(server)
    got = by_key(pages)
    assert [got[("sf_leads_created", d)] for d in (1, 2, 3)] == [2, 0, 1], "the 4th 00:00:00Z and 30 Sep are outside the half open window"
    assert [got[("sf_leads_converted", d)] for d in (1, 2, 3)] == [0, 1, 0]
    assert [got[("sf_opportunities_created", d)] for d in (1, 2, 3)] == [1, 2, 1]
    assert [got[("sf_opportunities_won", d)] for d in (1, 2, 3)] == [0, 2, 1]
    assert [got[("sf_won_amount", d)] for d in (1, 2, 3)] == [0, 150025, -20000], "exact decimals, a null Amount adds nothing, a negative is kept"
    pts = points_of(pages)
    assert len(pts) == 5 * 3 and {p.metric_key for p in pts} == set(METRICS)
    assert all(type(p.value_numeric) is int for p in pts)
    assert {(p.metric_key, p.currency) for p in pts if p.currency} == {("sf_won_amount", "USD")}


def test_the_queries_are_soql_with_unquoted_literals_a_stable_order_and_the_bearer_token(server):
    seed(server)
    fetch(server)
    assert queries(server) == [
        "SELECT Id, CreatedDate FROM Lead WHERE CreatedDate >= 2026-10-01T00:00:00Z AND CreatedDate < 2026-10-04T00:00:00Z ORDER BY CreatedDate, Id",
        "SELECT Id, IsConverted, ConvertedDate FROM Lead WHERE IsConverted = true AND ConvertedDate >= 2026-10-01 AND ConvertedDate < 2026-10-04 ORDER BY ConvertedDate, Id",
        "SELECT Id, CreatedDate FROM Opportunity WHERE CreatedDate >= 2026-10-01T00:00:00Z AND CreatedDate < 2026-10-04T00:00:00Z ORDER BY CreatedDate, Id",
        "SELECT Id, IsWon, CloseDate, Amount FROM Opportunity WHERE IsWon = true AND CloseDate >= 2026-10-01 AND CloseDate < 2026-10-04 ORDER BY CloseDate, Id",
    ]
    data = [r for r in server.requests if r.path.startswith("/services/data/")]
    assert all(r.headers["authorization"].startswith("Bearer 00D") for r in data)
    assert all(r.headers["sforce-query-options"] == "batchSize=2000" for r in data if r.path.endswith("/query"))


def test_api_calls_counts_every_request_the_sync_made_including_the_describes_and_the_token_exchange(server):
    seed(server)
    pages = fetch(server)
    assert sum(p.api_calls for p in pages) == len(server.requests) == 1 + 2 + 4


def test_a_day_with_nothing_is_zeros_and_another_window_is_never_counted(server):
    server.add_lead(1, utc(1))
    got = by_key(fetch(server, date(2026, 10, 2), date(2026, 10, 3)))
    assert set(got.values()) == {0} and len(got) == 5


def test_a_one_day_window_asks_that_day(server):
    server.add_lead(1, utc(7, 5))
    assert by_key(fetch(server, date(2026, 10, 7), date(2026, 10, 8)))[("sf_leads_created", 7)] == 1


@pytest.mark.parametrize("text, expect", [
    ("2026-10-01T12:34:56.000+0000", datetime(2026, 10, 1, 12, 34, 56, tzinfo=timezone.utc)),
    ("2026-10-01T12:34:56Z", datetime(2026, 10, 1, 12, 34, 56, tzinfo=timezone.utc)),
    ("2026-10-02T01:30:00.000+0530", datetime(2026, 10, 1, 20, 0, 0, tzinfo=timezone.utc)),
    ("2026-10-01T00:30:00-08:00", datetime(2026, 10, 1, 8, 30, 0, tzinfo=timezone.utc)),
])
def test_a_salesforce_datetime_is_read_into_the_utc_day(text, expect):
    got = parse_datetime_utc(text, "x")
    assert got == expect and got.utcoffset() == timedelta(0) and got.date() == expect.date(), "an aware datetime equals its twin in another zone, so the zone is checked too"


@pytest.mark.parametrize("bad", [None, 5, "", "2026-10-01", "2026-10-01 12:00:00", "2026-13-01T00:00:00.000+0000", "2026-10-01T25:00:00Z", "2026-10-01T12:00:00"])
def test_a_datetime_that_is_not_salesforces_is_refused(bad):
    with pytest.raises(UpstreamBroken):
        parse_datetime_utc(bad, "x")


# ---------------------------------------------------------------- paging

def test_a_long_result_is_read_page_by_page_through_next_records_url_with_no_parameters():
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH, min_batch=1) as server:
        for n in range(1, 11):
            server.add_lead(n, utc(1 + n % 3, n))
        pages = fetch(server, batch_size=3)
        got = by_key(pages)
        assert sum(got[("sf_leads_created", d)] for d in (1, 2, 3)) == 10
        more = [r for r in server.requests if "/query/" in r.path]
        assert len(more) == 3, "ten rows in pages of three: one first request and three follow ups"
        assert all(r.query == {} for r in more), "a nextRecordsUrl is called as it is, with no parameters"
        assert all(r.headers["sforce-query-options"] == "batchSize=3" for r in server.requests if r.path.endswith("/query") or "/query/" in r.path)


def page(records, **kw):
    body = {"totalSize": len(records), "done": True, "records": records}
    body.update(kw)
    return body


def lead_rec(n, created="2026-10-01T10:00:00.000+0000"):
    return {"attributes": {"type": "Lead", "url": "/x"}, "Id": "00Q%015d" % n, "CreatedDate": created}


def refused_query(server, body, match):
    """A fault on the FIRST QUERY: the two describes are answered normally first."""
    fetch(server)
    conn = make(server)
    run(conn._describe("Lead"))
    run(conn._describe("Opportunity"))
    server.faults.append(Fault(200, body))
    with pytest.raises(UpstreamBroken, match=match):
        run(collect(conn, START, END))


def test_a_next_page_url_that_leaves_the_data_api_is_never_followed_so_the_token_cannot_leave(server):
    for bad in ("http://evil.example/steal", "https://evil.example/services/data/v62.0/query/x-1", "/other/path", "/services/data/../../x", ""):
        refused_query(server, page([lead_rec(1)], done=False, totalSize=5, nextRecordsUrl=bad), "next page")
    assert not [r for r in server.requests if "evil" in r.path]


def test_done_with_a_next_page_and_a_total_that_does_not_match_are_refused(server):
    refused_query(server, page([lead_rec(1)], nextRecordsUrl="/services/data/v62.0/query/01g-1"), "done and also")
    refused_query(server, page([lead_rec(1)], totalSize=2), "totalSize")
    refused_query(server, page([lead_rec(1)], totalSize=0), "totalSize")


def test_a_repeated_next_page_url_is_refused_so_a_loop_cannot_spin(server):
    nxt = "/services/data/v62.0/query/01g000000000000-1"
    body = page([lead_rec(1)], done=False, totalSize=9, nextRecordsUrl=nxt)
    fetch(server)
    conn = make(server)
    run(conn._describe("Lead"))
    run(conn._describe("Opportunity"))
    server.faults[:] = [Fault(200, body), Fault(200, body)]
    with pytest.raises(UpstreamBroken, match="next page"):
        run(collect(conn, START, END))


@pytest.mark.parametrize("body, match", [
    ({"done": True, "totalSize": 0}, "lacks"),
    ({"done": "yes", "totalSize": 0, "records": []}, "lacks"),
    ({"done": True, "totalSize": True, "records": []}, "lacks"),
    ({"done": True, "totalSize": 0, "records": "nope"}, "lacks"),
    (["not", "an", "object"], "lacks"),
    (page(["nope"]), "not an object"),
    (page([{**lead_rec(1), "attributes": {"type": "Opportunity"}}]), "not a Lead"),
    (page([{**lead_rec(1), "attributes": None}]), "not a Lead"),
    (page([{**lead_rec(1), "Id": "short"}]), "usable Id"),
    (page([{**lead_rec(1), "Id": None}]), "usable Id"),
    (page([lead_rec(1), lead_rec(1)]), "twice"),
    (page([lead_rec(1, created="2026-10-09T10:00:00.000+0000")]), "outside the window"),
    (page([lead_rec(1, created="2026-09-30T23:59:59.000+0000")]), "outside the window"),
    (page([lead_rec(1, created="yesterday")]), "datetime"),
    (page([lead_rec(1, created=None)]), "datetime"),
])
def test_a_malformed_answer_is_refused(server, body, match):
    refused_query(server, body, match)


def test_a_body_that_is_not_json_is_refused(server):
    fetch(server)
    conn = make(server)
    run(conn._describe("Lead"))
    run(conn._describe("Opportunity"))
    server.faults.append(Fault(200, b"<html>maintenance</html>"))
    with pytest.raises(UpstreamBroken, match="not JSON"):
        run(collect(conn, START, END))


def test_a_converted_query_row_that_is_not_converted_and_a_won_row_that_is_not_won_are_refused(server):
    fetch(server)
    conn = make(server)
    run(conn._describe("Lead"))
    run(conn._describe("Opportunity"))
    fault_pages = [page([lead_rec(1)]), page([])]
    server.faults[:] = [Fault(200, fault_pages[0]), Fault(200, page([{"attributes": {"type": "Lead"}, "Id": "00Q%015d" % 2, "IsConverted": False, "ConvertedDate": "2026-10-02"}]))]
    with pytest.raises(UpstreamBroken, match="not converted"):
        run(collect(conn, START, END))
    conn = make(server)
    run(conn._describe("Lead"))
    run(conn._describe("Opportunity"))
    opp = {"attributes": {"type": "Opportunity"}, "Id": "006%015d" % 1, "IsWon": False, "CloseDate": "2026-10-02", "Amount": 5}
    server.faults[:] = [Fault(200, page([])), Fault(200, page([])), Fault(200, page([])), Fault(200, page([opp]))]
    with pytest.raises(UpstreamBroken, match="not won"):
        run(collect(conn, START, END))


# ---------------------------------------------------------------- money and currency

def test_a_multi_currency_org_asks_for_the_currency_and_refuses_a_row_in_another_one():
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH, multicurrency=True) as server:
        server.add_opportunity(1, utc(1), close=date(2026, 10, 2), won=True, amount=Decimal("10.00"), currency="USD")
        assert by_key(fetch(server))[("sf_won_amount", 2)] == 1000
        assert "Amount, CurrencyIsoCode FROM Opportunity" in queries(server)[-1]
        server.add_opportunity(2, utc(1), close=date(2026, 10, 2), won=True, amount=Decimal("99.00"), currency="EUR")
        with pytest.raises(UpstreamBroken, match="refusing to add across currencies"):
            fetch(server)


def test_a_single_currency_org_never_asks_for_a_currency_field(server):
    fetch(server)
    assert not any("CurrencyIsoCode" in q for q in queries(server))


@pytest.mark.parametrize("amount, minor", [(Decimal("19.99"), 1999), (Decimal("0.005"), 1), (Decimal("1234567.89"), 123456789), (0, 0), (7, 700)])
def test_amounts_are_exact_in_minor_units(server, amount, minor):
    server.add_opportunity(1, utc(1), close=date(2026, 10, 2), won=True, amount=amount)
    assert by_key(fetch(server))[("sf_won_amount", 2)] == minor


def test_a_zero_decimal_currency_is_stored_in_whole_units(server):
    server.add_opportunity(1, utc(1), close=date(2026, 10, 2), won=True, amount=Decimal("1500"))
    pts = points_of(fetch(server, currency="JPY"))
    assert {p.value_numeric for p in pts if p.metric_key == "sf_won_amount" and p.observed_on.day == 2} == {1500}


def test_a_float_amount_string_is_refused_as_not_a_number(server):
    refused_query_opp = Fault(200, page([{"attributes": {"type": "Opportunity"}, "Id": "006%015d" % 1, "IsWon": True, "CloseDate": "2026-10-02", "Amount": "lots"}]))
    fetch(server)
    conn = make(server)
    run(conn._describe("Lead"))
    run(conn._describe("Opportunity"))
    server.faults[:] = [Fault(200, page([])), Fault(200, page([])), Fault(200, page([])), refused_query_opp]
    with pytest.raises(UpstreamBroken, match="Amount"):
        run(collect(conn, START, END))


# ---------------------------------------------------------------- describe

def test_a_field_hidden_from_this_user_is_named_before_any_query_runs():
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH, hidden_fields=("Amount",)) as server:
        with pytest.raises(UpstreamBroken, match="Opportunity has no field Amount"):
            fetch(server)
        assert queries(server) == []


def test_a_field_of_the_wrong_type_is_refused(server):
    fetch(server)
    conn = warm(make(server))
    body = {"name": "Lead", "fields": [{"name": n, "type": "string"} for n in ("Id", "CreatedDate", "IsConverted", "ConvertedDate", "Email", "Phone", "MobilePhone", "LeadSource")]}
    server.faults.append(Fault(200, body))
    with pytest.raises(UpstreamBroken, match="CreatedDate is a string"):
        run(conn._describe("Lead"))


@pytest.mark.parametrize("body", [{"name": "Account", "fields": []}, {"name": "Lead"}, {"name": "Lead", "fields": "x"}, ["x"]])
def test_a_describe_answer_that_is_not_the_object_is_refused(server, body):
    fetch(server)
    conn = warm(make(server))
    server.faults.append(Fault(200, body))
    with pytest.raises(UpstreamBroken, match="describe"):
        run(conn._describe("Lead"))


def test_verify_describes_both_objects_and_checks_the_lead_to_opportunity_link(server):
    label = run(make(server).verify())
    assert server.url.split("//")[1] in label
    assert [r.path.rsplit("/", 2)[-2] for r in server.requests if r.path.endswith("/describe")] == ["Lead", "Opportunity"]
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH, hidden_fields=("ConvertedOpportunityId",)) as other:
        with pytest.raises(UpstreamBroken, match="ConvertedOpportunityId"):
            run(make(other).verify())


def test_verify_refuses_a_converted_opportunity_link_that_points_somewhere_else(server):
    conn = warm(make(server))
    names = ("Id", "CreatedDate", "IsConverted", "ConvertedDate", "Email", "Phone", "MobilePhone", "LeadSource")
    fields = [{"name": n, "type": "string"} for n in names]
    fields = [{**f, "type": {"Id": "id", "CreatedDate": "datetime", "IsConverted": "boolean", "ConvertedDate": "date"}.get(f["name"], f["type"])} for f in fields]
    fields.append({"name": "ConvertedOpportunityId", "type": "reference", "referenceTo": ["Account"]})
    server.faults.append(Fault(200, {"name": "Lead", "fields": fields}))
    with pytest.raises(UpstreamBroken, match="does not point at Opportunity"):
        run(conn.verify())


# ---------------------------------------------------------------- OAuth: the refresh token flow

def test_the_first_call_exchanges_the_refresh_token_with_every_secret_in_the_body_never_the_query(server):
    fetch(server)
    token_calls = [r for r in server.requests if r.path == "/services/oauth2/token"]
    assert len(token_calls) == 1
    t = token_calls[0]
    assert t.method == "POST" and t.query == {}, "secrets in a query string are what the page says never to do"
    body = t.body.decode()
    assert "grant_type=refresh_token" in body and "client_id=" + CLIENT_ID in body and "client_secret=" + SECRET in body and "refresh_token=" + REFRESH in body
    assert t.headers["content-type"].startswith("application/x-www-form-urlencoded")


def test_one_token_serves_every_request_of_a_sync(server):
    conn = make(server)
    run(collect(conn, START, END))
    assert conn._tokens.refresh_calls == 1
    assert len({r.headers["authorization"] for r in server.requests if r.path.startswith("/services/data/")}) == 1


def test_a_401_mid_sync_refreshes_once_and_repeats_the_request(server):
    seed(server)
    conn = make(server)
    run(conn.verify())
    server.expire_access_tokens()
    pages = run(collect(conn, START, END))
    assert conn._tokens.refresh_calls == 2 and by_key(pages)[("sf_leads_created", 1)] == 2


def test_a_401_that_survives_a_refresh_is_an_expired_token_and_is_not_retried_in_a_loop():
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH, dead_tokens=True) as server:
        conn = make(server)
        with pytest.raises(AuthExpired):
            run(collect(conn, START, END))
        assert conn._tokens.refresh_calls == 2, "one refresh at the start, one after the 401, and no third"
        assert len([r for r in server.requests if r.path.startswith("/services/data/")]) == 2


def test_a_pasted_access_token_cannot_refresh_so_a_401_is_an_expired_token(server):
    token = server.issue_access_token()
    server.add_lead(1, utc(1))
    conn = make(server, access_token=token)
    assert by_key(run(collect(conn, START, END)))[("sf_leads_created", 1)] == 1
    server.expire_access_tokens()
    with pytest.raises(AuthExpired):
        run(collect(make(server, access_token=token), START, END))
    assert not [r for r in server.requests if r.path == "/services/oauth2/token"]


@pytest.mark.parametrize("kw, code", [
    ({"refresh_token": "a-token-nobody-issued"}, "invalid_grant"),
    ({"client_secret": "wrong-secret"}, "invalid_client"),
    ({"client_id": "wrong-id"}, "invalid_client_id"),
])
def test_a_refused_refresh_token_or_app_means_a_person_must_reconnect(server, kw, code):
    fetch(server)                                                             # positive control
    with pytest.raises(AuthRevoked, match=code) as info:
        fetch(server, **kw)
    for secret in (SECRET, REFRESH, "a-token-nobody-issued", "wrong-secret"):
        assert secret not in str(info.value)
    assert info.value.retryable is False


def test_the_token_endpoint_retries_a_5xx_with_a_bound_and_then_succeeds_or_hands_back(server):
    sleeps = Sleeps()
    server.faults.append(Fault(503, {}))
    assert fetch(server, sleeps=sleeps) and len(sleeps.calls) == 1
    server.faults[:] = [Fault(503, {})] * 3
    with pytest.raises(RateLimited, match="gave up after 3"):
        fetch(server, sleeps=Sleeps())


@pytest.mark.parametrize("body, match", [({"access_token": "x"}, "token_type"), ({"token_type": "Bearer"}, "no access_token"),
                                          ({"access_token": "", "token_type": "Bearer"}, "no access_token"), ({"access_token": "t", "token_type": "Bearer", "refresh_token": 5}, "refresh_token")])
def test_a_token_answer_that_is_not_a_bearer_token_is_refused(server, body, match):
    fetch(server)
    server.faults.append(Fault(200, body))
    with pytest.raises(UpstreamBroken, match=match):
        fetch(server)


def test_a_rotated_refresh_token_replaces_the_old_one_and_is_handed_to_the_caller():
    got = []
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH, rotate=True) as server:
        conn = make(server, on_rotate=got.append)
        run(conn.verify())
        assert len(got) == 1 and conn.refresh_rotated and got[0] != REFRESH
        server.expire_access_tokens()
        conn._describes.clear()
        run(conn.verify())                                                    # the NEW refresh token is the one that works now
        assert len(got) == 2 and REFRESH not in server.refresh_tokens
        old = make(server)                                                    # a second process still holding the old one is told to reconnect
        with pytest.raises(AuthRevoked, match="invalid_grant"):
            run(old.verify())


def test_many_callers_that_find_the_token_dead_cause_one_refresh_not_many(server):
    conn = make(server)
    run(conn.verify())
    server.expire_access_tokens()
    dead = run(conn._tokens.access_token())

    async def storm():
        return await asyncio.gather(*[conn._tokens.refresh_after(dead) for _ in range(8)])

    tokens = run(storm())
    assert len(set(tokens)) == 1 and conn._tokens.refresh_calls == 2, "the page: simultaneous requests with one refresh token fail"


# ---------------------------------------------------------------- limits

def test_the_limit_header_is_read_from_every_answer():
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH, api_limit=100000, api_used=10000) as server:
        conn = make(server)
        run(collect(conn, START, END))
        assert conn.api_usage == (10000 + len([r for r in server.requests if r.path.startswith("/services/data/")]), 100000)


def test_a_sync_stops_before_a_call_when_few_of_the_orgs_daily_calls_are_left():
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH, api_limit=100, api_used=40) as server:
        conn = make(server, min_remaining=50)
        run(conn._describe("Lead"))                                           # 41 of 100 used: 59 left, above the floor
        server.api_used = 55                                                  # 45 left after the next answer, below the floor
        run(conn._describe("Opportunity"))
        before = len(server.requests)
        with pytest.raises(RateLimited, match="only 44 of the org's 100"):
            run(collect(conn, START, END))
        assert len(server.requests) == before, "no request is made once the floor is crossed"


def test_a_floor_of_zero_turns_the_pacing_off():
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH, api_limit=1000, api_used=960) as server:
        assert run(collect(make(server, min_remaining=0), START, END))
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH, api_limit=1000, api_used=960) as server:
        with pytest.raises(RateLimited):
            run(collect(make(server, min_remaining=50), START, END))


def test_a_403_request_limit_exceeded_is_handed_back_at_once_without_sleeping_or_retrying():
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH, api_limit=3, api_used=3) as server:
        sleeps = Sleeps()
        with pytest.raises(RateLimited, match="daily API request limit") as info:
            run(collect(make(server, sleeps, min_remaining=0), START, END))
        assert sleeps.calls == [] and info.value.retryable
        assert len([r for r in server.requests if r.path.startswith("/services/data/")]) == 1


def test_a_malformed_limit_header_is_ignored_not_fatal(server):
    seed(server)
    server.faults.append(Fault(200, {"name": "Lead", "fields": []}, {"Sforce-Limit-Info": "api-usage=banana"}))
    conn = make(server)
    with pytest.raises(UpstreamBroken):
        run(conn._describe("Lead"))                                           # the (empty) describe is refused for its fields, not for the header
    assert conn.api_usage is None


# ---------------------------------------------------------------- errors and retries

def failing(server, status, body, expected, headers=None):
    fetch(server)                                                             # positive control
    conn = make(server)
    run(conn._describe("Lead"))
    run(conn._describe("Opportunity"))
    server.faults.append(Fault(status, body, headers or {}))
    with pytest.raises(expected) as info:
        run(collect(conn, START, END))
    text = str(info.value)
    assert SECRET not in text and REFRESH not in text and "00D" not in text
    return info.value


def test_401_is_an_expired_token_and_403_is_no_access_and_404_and_400_are_a_broken_upstream(server):
    server.add_lead(1, utc(1))
    token = server.issue_access_token()
    conn = make(server, access_token=token)
    server.faults.append(Fault(401, error_body("INVALID_SESSION_ID")))
    with pytest.raises(AuthExpired):
        run(collect(conn, START, END))
    failing(server, 403, error_body("INSUFFICIENT_ACCESS"), AuthRevoked)
    failing(server, 404, error_body("NOT_FOUND"), UpstreamBroken)
    failing(server, 400, error_body("MALFORMED_QUERY"), UpstreamBroken)
    failing(server, 400, error_body("INVALID_FIELD"), UpstreamBroken)


def test_the_http_status_decides_when_the_body_is_not_salesforces_list(server):
    failing(server, 403, {}, AuthRevoked)
    failing(server, 403, b"<html>blocked</html>", AuthRevoked)
    failing(server, 400, b"", UpstreamBroken)


def test_a_bad_request_is_never_retried(server):
    sleeps = Sleeps()
    fetch(server)
    conn = make(server, sleeps)
    run(conn._describe("Lead"))
    run(conn._describe("Opportunity"))
    server.faults.append(Fault(400, error_body("MALFORMED_QUERY")))
    with pytest.raises(UpstreamBroken, match="MALFORMED_QUERY"):
        run(collect(conn, START, END))
    assert sleeps.calls == []


def test_500_502_503_are_retried_with_a_bound_then_succeed(server):
    for status in (500, 502, 503):
        sleeps = Sleeps()
        conn = warm(make(server, sleeps))
        server.faults[:] = [Fault(status, {})] * 3
        with pytest.raises(RateLimited, match="gave up after 3"):
            run(collect(conn, START, END))
        assert len(sleeps.calls) == 2, status
        conn = warm(make(server, Sleeps()))
        server.faults[:] = [Fault(status, error_body("SERVER_UNAVAILABLE"))]
        assert run(collect(conn, START, END)), status


def test_503_with_a_retry_after_waits_that_long_and_a_longer_wait_than_the_cap_is_handed_back(server):
    sleeps = Sleeps()
    conn = warm(make(server, sleeps))
    server.faults.append(Fault(503, {}, {"Retry-After": "7"}))
    pages = run(collect(conn, START, END))
    assert sleeps.calls == [7.0] and pages[0].throttle_waits == 1
    sleeps = Sleeps()
    conn = warm(make(server, sleeps))
    server.faults.append(Fault(503, {}, {"Retry-After": "900"}))
    with pytest.raises(RateLimited) as info:
        run(collect(conn, START, END))
    assert sleeps.calls == [] and info.value.retry_after_seconds == 900


def test_a_dropped_connection_is_retried_with_a_bound(server):
    conn = warm(make(server, Sleeps()))
    server.faults[:] = [Fault(0, drop=True)] * 3
    with pytest.raises(RateLimited, match="transport failure"):
        run(collect(conn, START, END))
    conn = warm(make(server, Sleeps()))
    server.faults[:] = [Fault(0, drop=True)]
    assert run(collect(conn, START, END))


def test_no_secret_appears_in_a_repr_or_an_error_even_when_salesforce_echoes_it(server):
    conn = make(server)
    for secret in (SECRET, REFRESH):
        assert secret not in repr(conn) and secret not in repr(conn._tokens.__dict__)
    server.faults[:] = [Fault(500, [{"errorCode": "UNKNOWN_EXCEPTION", "message": "echo " + SECRET + REFRESH}])] * 3
    with pytest.raises(RateLimited) as info:
        run(collect(conn, START, END))
    assert SECRET not in str(info.value) and REFRESH not in str(info.value)


# ---------------------------------------------------------------- inputs

def test_a_cursor_and_an_empty_window_are_refused(server):
    with pytest.raises(ValueError, match="no cursor"):
        run(collect(make(server), START, END, cursor="x"))
    with pytest.raises(ValueError, match="at least one day"):
        run(collect(make(server), START, START))
    with pytest.raises(ValueError, match="at least one day"):
        run(make(server).leads(window_start=START, window_end=START))
    fetch(server)


@pytest.mark.parametrize("bad", ["http://acme.my.salesforce.com", "https://acme.my.salesforce.com/path", "https://user:pw@acme.my.salesforce.com",
                                 "https://acme.my.salesforce.com?x=1", "acme.my.salesforce.com", "ftp://acme.my.salesforce.com", "", "https://"])
def test_an_org_address_that_is_not_a_bare_https_address_is_refused(bad):
    with pytest.raises(ValueError, match="instance_url"):
        SalesforceConnector(instance_url=bad, currency="USD", access_token="t")


def test_a_good_org_address_a_login_address_and_the_account_name():
    conn = SalesforceConnector(instance_url="https://Acme.my.salesforce.com/", currency="USD", access_token="t")
    assert conn.external_account == "Acme.my.salesforce.com"
    with pytest.raises(ValueError, match="login_url"):
        SalesforceConnector(instance_url="https://a.my.salesforce.com", currency="USD", client_id="a", client_secret="b", refresh_token="c", login_url="http://login.example.com")


@pytest.mark.parametrize("kw", [dict(), dict(access_token="t", client_id="a", client_secret="b", refresh_token="c"), dict(client_id="a"), dict(client_id="a", client_secret="b")])
def test_exactly_one_way_to_authenticate_is_required(kw):
    with pytest.raises(ValueError, match="either access_token"):
        SalesforceConnector(instance_url="https://a.my.salesforce.com", currency="USD", **kw)


@pytest.mark.parametrize("kw, match", [(dict(currency="usd"), "currency"), (dict(currency="US"), "currency"), (dict(api_version="62"), "api_version"),
                                       (dict(api_version="v6.0"), "api_version"), (dict(batch_size=0), "batch_size"), (dict(batch_size=2001), "batch_size"),
                                       (dict(batch_size=True), "batch_size"), (dict(min_remaining=-1), "min_remaining")])
def test_bad_options_are_refused(kw, match):
    base = dict(instance_url="https://a.my.salesforce.com", currency="USD", access_token="t")
    base.update(kw)
    with pytest.raises(ValueError, match=match):
        SalesforceConnector(**base)


def test_from_env_reads_the_five_values_and_names_what_is_missing(server):
    env = {"SALESFORCE_INSTANCE_URL": server.url, "SALESFORCE_CURRENCY": "USD", "SALESFORCE_CLIENT_ID": CLIENT_ID, "SALESFORCE_CLIENT_SECRET": SECRET,
           "SALESFORCE_REFRESH_TOKEN": REFRESH, "SALESFORCE_LOGIN_URL": server.url}
    conn = SalesforceConnector.from_env(env, client=client(), retrier=retrier())
    assert run(collect(conn, START, END))
    with pytest.raises(ValueError, match="SALESFORCE_CLIENT_SECRET"):
        SalesforceConnector.from_env({k: v for k, v in env.items() if k != "SALESFORCE_CLIENT_SECRET"})


def test_the_connector_is_registered_with_its_five_metrics_and_five_credential_fields():
    spec = registry.get("salesforce")
    assert spec.label == "Salesforce" and spec.supported_metrics == frozenset(METRICS) and len(METRICS) == 5
    assert [f.key for f in spec.credential_fields] == ["instance_url", "currency", "client_id", "client_secret", "refresh_token"]
    assert [f.key for f in spec.credential_fields if f.secret] == ["client_secret", "refresh_token"]
    assert registry.redact("salesforce", {"client_secret": "s", "refresh_token": "r", "currency": "USD"}) == {"client_secret": "***", "refresh_token": "***", "currency": "USD"}


# ---------------------------------------------------------------- the lead rows for matching

def test_leads_come_back_as_records_with_both_phone_fields_and_the_source(server):
    server.add_lead(1, utc(1, 8), email="A@X.com", phone="(212) 555-0100", mobile="+63 917 000 0000", source="Web")
    server.add_lead(2, utc(2, 8), converted_on=date(2026, 10, 3))
    leads = run(make(server).leads(window_start=START, window_end=END))
    assert [l.id for l in leads] == ["00Q%015d" % 1, "00Q%015d" % 2]
    assert leads[0].email == "A@X.com" and leads[0].phones == ("(212) 555-0100", "+63 917 000 0000") and leads[0].source == "Web" and not leads[0].converted
    assert leads[1].phones == () and leads[1].converted and leads[0].created_at.tzinfo is not None
    assert "Email, Phone, MobilePhone, LeadSource, IsConverted FROM Lead" in queries(server)[-1]


def test_a_lead_row_with_a_phone_that_is_not_text_is_refused(server):
    fetch(server)
    conn = make(server)
    run(conn._describe("Lead"))
    row = {**lead_rec(1), "Email": None, "Phone": 5551234, "MobilePhone": None, "LeadSource": None, "IsConverted": False}
    server.faults.append(Fault(200, page([row])))
    with pytest.raises(UpstreamBroken, match="Phone is not text"):
        run(conn.leads(window_start=START, window_end=END))


# ---------------------------------------------------------------- the stand-in refuses what Salesforce refuses

def test_the_stand_in_refuses_a_quoted_date_an_unknown_field_an_unknown_object_a_bad_token_and_secrets_in_the_query(server):
    token = server.issue_access_token()

    def q(soql, tok=token, path="/services/data/v62.0/query"):
        return httpx.get(server.url + path, params={"q": soql}, headers={"Authorization": "Bearer " + tok}, trust_env=False)

    assert q("SELECT Id FROM Lead WHERE CreatedDate >= 2026-10-01T00:00:00Z").status_code == 200
    quoted = q("SELECT Id FROM Lead WHERE CreatedDate >= '2026-10-01T00:00:00Z'")
    assert quoted.status_code == 400 and quoted.json()[0]["errorCode"] == "MALFORMED_QUERY"
    assert q("SELECT Nope FROM Lead").json()[0]["errorCode"] == "INVALID_FIELD"
    assert q("SELECT Id FROM Widget").json()[0]["errorCode"] == "INVALID_TYPE"
    assert q("SELEKT Id").json()[0]["errorCode"] == "MALFORMED_QUERY"
    assert q("SELECT Id FROM Lead", tok="wrong").status_code == 401
    assert q("SELECT Id FROM Lead", path="/services/data/v1.0/query").status_code == 404
    leaky = httpx.post(server.url + "/services/oauth2/token?client_secret=" + SECRET, data={"grant_type": "refresh_token", "client_id": CLIENT_ID,
                       "client_secret": SECRET, "refresh_token": REFRESH}, trust_env=False)
    assert leaky.status_code == 400


# ---------------------------------------------------------------- seeded worlds: the stored numbers equal an independent recount

WORLD_SEEDS = range(60)


def world(seed):
    rng = random.Random(seed)
    first = date(2026, 1, 1) + timedelta(days=rng.randrange(0, 300))
    days = rng.randrange(1, 25)
    start, end = first, first + timedelta(days=days)
    leads, opps = [], []
    for n in range(rng.randrange(0, 40)):
        # created times cluster on the window edges, where the half open rule is easiest to get wrong
        moment = datetime.combine(start - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) + timedelta(seconds=rng.choice([0, 1, 86399, 86400, 86401, 3600 * 24 * days - 1, 3600 * 24 * days, 3600 * 24 * (days + 1) - 1, rng.randrange(0, 3600 * 24 * (days + 2))]))
        converted = start + timedelta(days=rng.randrange(-1, days + 1)) if rng.random() < 0.4 else None
        leads.append((n + 1, moment, converted))
    for n in range(rng.randrange(0, 40)):
        moment = datetime.combine(start - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) + timedelta(seconds=rng.randrange(0, 3600 * 24 * (days + 2)))
        close = start + timedelta(days=rng.randrange(-1, days + 1))
        amount = rng.choice([None, 0, Decimal("0.01"), Decimal("19.99"), Decimal("1234.50"), -50, 10 ** 9])
        opps.append((n + 1, moment, close, rng.random() < 0.5, amount, rng.choice(["USD", "USD", "USD", "EUR"])))
    return start, end, days, leads, opps, rng.randrange(1, 8), rng.random() < 0.5


@pytest.mark.parametrize("seed", WORLD_SEEDS)
def test_in_every_seeded_world_the_stored_numbers_equal_an_independent_recount(seed):
    start, end, days, leads, opps, batch, multi = world(seed)
    with FakeSalesforce(client_id=CLIENT_ID, client_secret=SECRET, refresh_token=REFRESH, multicurrency=multi, min_batch=1) as server:
        for n, moment, converted in leads:
            server.add_lead(n, moment, converted_on=converted)
        for n, moment, close, won, amount, currency in opps:
            server.add_opportunity(n, moment, close=close, won=won, amount=amount, currency=currency)
        wrong_currency = multi and any(won and close_in(start, end, close) and currency != "USD" for _, _, close, won, _, currency in opps)
        conn = make(server, batch_size=batch)
        if wrong_currency:
            with pytest.raises(UpstreamBroken, match="across currencies"):
                run(collect(conn, start, end))
            return
        pages = run(collect(conn, start, end))
        got = {(p.metric_key, p.observed_on): p.value_numeric for p in points_of(pages)}
    assert len(got) == 5 * days
    for d in range(days):
        day = start + timedelta(days=d)
        assert got[("sf_leads_created", day)] == sum(1 for _, m, _ in leads if m.date() == day), (seed, day)
        assert got[("sf_leads_converted", day)] == sum(1 for _, _, c in leads if c == day), (seed, day)
        assert got[("sf_opportunities_created", day)] == sum(1 for _, m, *_ in opps if m.date() == day), (seed, day)
        won = [o for o in opps if o[3] and o[2] == day]
        assert got[("sf_opportunities_won", day)] == len(won), (seed, day)
        assert got[("sf_won_amount", day)] == sum(int((Decimal(o[4]) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)) for o in won if o[4] is not None), (seed, day)


def close_in(start, end, close):
    return start <= close < end
