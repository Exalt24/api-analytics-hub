"""The Microsoft Graph connector against its stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the sign-in request (client credentials, secrets in the body, the .default scope), the token's life (renewed before it
expires, on a 401 once, once for many callers), the requests the connector builds for a SharePoint list, a Teams channel and a workbook download, how it reads the documented
answers (value, @odata.nextLink, createdDateTime, the 302 to a pre-authenticated address), the day each row lands on (UTC), what it refuses, that the token never goes to a host
an answer names or to the download address, and that the workbook endpoints (which the pages say take no application permission) are never called. They do not prove Microsoft
accepts any of it: the server is written from the saved pages (docs/HARVEST_GRAPH_SOURCES.md) and no live tenant was used.

Every refusal runs a positive control first (the same call succeeds).
"""
from __future__ import annotations

import asyncio
import io
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors import msgraph  # noqa: E402
from app.connectors.base import AuthExpired, AuthRevoked, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.fake_http import Fault  # noqa: E402
from app.connectors.msgraph import METRICS, MicrosoftGraphConnector, parse_graph_datetime  # noqa: E402
from app.connectors.msgraph_fake import FakeGraph, gdt, graph_error  # noqa: E402
from app.connectors.registry import registry  # noqa: E402
from vendor_helpers import Sleeps, client, collect, points_of, retrier, run  # noqa: E402

CLIENT_ID, SECRET = "app-client-id", "app-client-SECRET"
START, END = date(2026, 10, 1), date(2026, 10, 4)


def utc(day, hour=12, minute=0, second=0, month=10):
    return datetime(2026, month, day, hour, minute, second, tzinfo=timezone.utc)


@pytest.fixture()
def server():
    with FakeGraph(client_secret=SECRET) as fake:
        yield fake


def make(server, sleeps=None, team=True, **kw):
    kw.setdefault("retrier", retrier(sleeps))
    if team:
        kw.setdefault("team_id", server.team)
        kw.setdefault("channel_id", server.channel)
    return MicrosoftGraphConnector(tenant_id=server.tenant, client_id=kw.pop("client_id", CLIENT_ID), client_secret=kw.pop("client_secret", SECRET),
                                   site_id=server.site, list_id=server.list_id, graph_base=server.url, login_base=server.url, client=client(), **kw)


def by_key(pages):
    return {(p.metric_key, p.observed_on.day): p.value_numeric for p in points_of(pages)}


def fetch(server, start=START, end=END, **kw):
    return run(collect(make(server, **kw), start, end))


def graph_calls(server):
    return [r for r in server.requests if r.path.startswith("/v1.0/")]


def seed(server):
    server.add_item(1, utc(1, 8), modified=utc(2, 9))
    server.add_item(2, utc(1, 23, 59, 59))
    server.add_item(3, utc(3, 0, 0, 0))
    server.add_item(4, utc(4, 0, 0, 0))                                         # the window is half open: this is the 4th
    server.add_item(5, datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc))
    server.add_message(10, utc(1, 10))
    server.add_message(11, utc(2, 10), modified=utc(9, 10))                     # created in the window, changed later: still a day 2 message
    server.add_message(12, utc(2, 11), kind="systemEventMessage")
    server.add_message(13, utc(3, 11), deleted=utc(3, 12))
    server.add_message(14, utc(4, 0, 0, 0))


# ---------------------------------------------------------------- the happy path

def test_three_counts_per_day_by_the_utc_day_with_a_half_open_window(server):
    seed(server)
    pages = fetch(server)
    got = by_key(pages)
    assert [got[("sp_register_items_created", d)] for d in (1, 2, 3)] == [2, 0, 1], "the 4th 00:00:00Z and 30 Sep are outside the window"
    assert [got[("sp_register_items_modified", d)] for d in (1, 2, 3)] == [1, 1, 1]
    assert [got[("teams_channel_messages", d)] for d in (1, 2, 3)] == [1, 1, 0], "a system message and a deleted one are not messages"
    pts = points_of(pages)
    assert len(pts) == 3 * 3 and {p.metric_key for p in pts} == set(METRICS)
    assert all(type(p.value_numeric) is int and p.currency is None for p in pts)


def test_without_a_team_and_channel_only_the_register_keys_are_written(server):
    seed(server)
    pages = fetch(server, team=False)
    assert {p.metric_key for p in points_of(pages)} == {"sp_register_items_created", "sp_register_items_modified"}
    assert not [r for r in server.requests if "/teams/" in r.path]


def test_the_requests_ask_for_fields_and_the_top_page_size_and_carry_the_bearer_token(server):
    seed(server)
    fetch(server)
    calls = graph_calls(server)
    items = [r for r in calls if r.path.endswith("/items")]
    assert len(items) == 1 and items[0].query == {"expand": "fields"}
    teams = [r for r in calls if "/teams/" in r.path]
    assert teams[0].query == {"$top": "50"}
    assert all(r.headers["authorization"].startswith("Bearer eyJ0eXAi.") for r in calls)


def test_api_calls_counts_the_token_request_and_every_page(server):
    seed(server)
    pages = fetch(server)
    assert sum(p.api_calls for p in pages) == len(server.requests) == 3


def test_verify_reads_one_item_and_names_the_tenant_and_list(server):
    seed(server)
    conn = make(server)
    label = run(_verify(conn))
    assert label == "Microsoft 365 tenant %s, list %s" % (server.tenant, server.list_id)
    assert [r.query for r in graph_calls(server)] == [{"$top": "1"}]


async def _verify(conn):
    try:
        return await conn.verify()
    finally:
        await conn.aclose()


def test_pages_are_followed_by_the_next_link_as_given(server):
    for i in range(1, 8):
        server.add_item(i, utc(1 + i % 3))
    server.page_size = 3
    got = by_key(fetch(server, team=False))
    assert sum(got[("sp_register_items_created", d)] for d in (1, 2, 3)) == 7
    items = [r for r in graph_calls(server) if r.path.endswith("/items")]
    assert len(items) == 3 and "$skiptoken" in items[1].query and items[1].query["expand"] == "fields", "the next link keeps the original query and adds the skip token"


def test_every_page_of_the_channel_is_read_because_the_order_is_by_last_change(server):
    for i in range(1, 121):
        server.add_message(i, utc(1 + i % 3, 8), modified=utc(1 + i % 3, 8) + timedelta(days=i % 40))
    got = by_key(fetch(server))
    assert sum(got[("teams_channel_messages", d)] for d in (1, 2, 3)) == 120
    assert len([r for r in graph_calls(server) if "/teams/" in r.path]) == 3, "120 messages at 50 a page"


def test_the_typed_reads_return_the_rows_and_the_messages(server):
    server.add_item(1, utc(1), fields={"Title": "Bridge A", "Budget": 12000, "Rate": 12.5})
    server.add_message(7, utc(2, 8), text="<p>Drawing issued</p>", author="Robin Kline")
    server.add_message(8, utc(2, 9), author=None)
    server.add_message(9, utc(10, 8), text="outside the window")
    conn = make(server)
    items = run(conn.register_items())
    messages = run(conn.channel_messages(window_start=START, window_end=END))
    run(conn.aclose())
    assert [(i.id, dict(i.fields)) for i in items] == [("1", {"Title": "Bridge A", "Budget": 12000, "Rate": 12.5})]
    assert [(m.id, m.author, m.text, m.body_type) for m in sorted(messages, key=lambda m: m.id)] == [("7", "Robin Kline", "<p>Drawing issued</p>", "html"), ("8", None, "Hello", "html")]


# ---------------------------------------------------------------- the token

def test_the_first_call_exchanges_the_secret_in_the_body_and_never_in_the_query(server):
    seed(server)
    fetch(server)
    mint = [r for r in server.requests if r.path.endswith("/oauth2/v2.0/token")]
    assert len(mint) == 1 and mint[0].method == "POST" and mint[0].query == {}
    body = mint[0].body.decode()
    assert "grant_type=client_credentials" in body and "scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" in body and "client_secret=" + SECRET in body
    assert all(SECRET not in r.path for r in server.requests)


def test_a_token_is_renewed_a_minute_before_it_expires_and_not_before(server):
    seed(server)
    now = [1000.0]
    conn = make(server, clock=lambda: now[0], team=False)

    async def two_reads():
        await conn.register_items()
        now[0] += 3599 - 60 - 1                                         # one second short of the renewal point
        await conn.register_items()
        first = server.token_requests
        now[0] += 2                                                     # past it
        await conn.register_items()
        return first, server.token_requests

    before, after = run(two_reads())
    run(conn.aclose())
    assert (before, after) == (1, 2)


def test_an_expired_session_mid_sync_is_renewed_once_and_the_request_repeated(server):
    seed(server)
    conn = make(server, team=False)

    async def go():
        await conn.register_items()
        server.expire_access_tokens()
        return await conn.register_items()

    items = run(go())
    run(conn.aclose())
    assert len(items) == 5 and server.token_requests == 2


def test_a_token_that_is_refused_again_after_the_renewal_is_auth_expired(server):
    seed(server)
    conn = make(server, team=False)
    real = server.answer

    def always_401(seen):
        if seen.path.startswith("/v1.0/"):
            return 401, {}, graph_error("InvalidAuthenticationToken")
        return real(seen)

    server.answer = always_401
    with pytest.raises(AuthExpired):
        run(conn.register_items())
    assert server.token_requests == 2, "renewed exactly once, then reported"
    run(conn.aclose())


def test_many_callers_with_one_rejected_token_cause_one_new_token(server):
    conn = make(server, team=False)

    async def go():
        stale = await conn._tokens.access_token()
        return await asyncio.gather(*[conn._tokens.refresh_after(stale) for _ in range(12)])

    tokens = run(go())
    run(conn.aclose())
    assert len(set(tokens)) == 1 and server.token_requests == 2


def test_a_refused_app_registration_is_auth_revoked_so_a_person_must_fix_it(server):
    seed(server)
    assert fetch(server, team=False), "positive control"
    for bad in ({"client_secret": "wrong"}, {"client_id": "someone-else"}):
        with pytest.raises(AuthRevoked, match="invalid_client"):
            fetch(server, team=False, **bad)


@pytest.mark.parametrize("answer, match", [
    ({"token_type": "MAC", "expires_in": 3599, "access_token": "x"}, "Bearer"),
    ({"token_type": "Bearer", "expires_in": 0, "access_token": "x"}, "expires_in"),
    ({"token_type": "Bearer", "expires_in": "3599", "access_token": "x"}, "expires_in"),
    ({"token_type": "Bearer", "expires_in": 3599}, "access_token"),
    ({"token_type": "Bearer", "expires_in": 3599, "access_token": ""}, "access_token"),
])
def test_a_token_answer_that_breaks_the_documented_shape_is_refused(server, answer, match):
    server.add_item(1, utc(1))
    assert fetch(server, team=False)
    server.token_faults.append(Fault(200, answer))
    with pytest.raises(UpstreamBroken, match=match):
        fetch(server, team=False)


def test_the_token_endpoint_throttling_is_waited_for_like_any_other_call(server):
    server.add_item(1, utc(1))
    sleeps = Sleeps()
    server.token_faults.append(Fault(429, {"error": "temporarily_unavailable"}, {"Retry-After": "4"}))
    assert fetch(server, team=False, sleeps=sleeps)
    assert sleeps.calls == [4.0]


# ---------------------------------------------------------------- what it refuses

def test_a_next_link_on_another_host_is_never_followed_and_the_token_never_goes_there(server):
    for i in range(1, 6):
        server.add_item(i, utc(1))
    server.page_size = 2
    assert by_key(fetch(server, team=False))[("sp_register_items_created", 1)] == 5, "positive control"
    for evil in ("https://evil.example.test/v1.0/sites/x/lists/y/items?$skiptoken=1", "http://127.0.0.1:9/v1.0/x", "/v1.0/relative", server.url + "/v1.0/../secret",
                 server.url + "/beta/sites/x/lists/y/items?$skiptoken=1", server.url + "/v2.0/anything"):
        server.requests.clear()
        real = server._items

        def poisoned(seen, roles, evil=evil, real=real):
            status, headers, payload = real(seen, roles)
            import json as _json
            body = _json.loads(payload)
            if body.get("@odata.nextLink"):
                body["@odata.nextLink"] = evil
            return status, headers, _json.dumps(body).encode()

        server._items = poisoned
        with pytest.raises(UpstreamBroken, match="outside Microsoft Graph"):
            fetch(server, team=False)
        server._items = real
        assert all(r.headers["host"].startswith("127.0.0.1:%s" % server.url.rsplit(":", 1)[1]) for r in server.requests)


def test_a_path_outside_v1_0_is_never_called_with_the_token(server):
    conn = make(server, team=False)
    assert run(conn._get("/v1.0/sites/%s/lists/%s/items" % (server.site, server.list_id), {"$top": "1"}))
    for bad in ("/v2/anything", "/beta/sites", "https://evil.example.test/v1.0/x"):
        with pytest.raises(UpstreamBroken, match="outside /v1.0/"):
            run(conn._get(bad))
    run(conn.aclose())
    assert [r.path for r in graph_calls(server)] == ["/v1.0/sites/%s/lists/%s/items" % (server.site, server.list_id)]


def test_a_next_link_that_names_itself_is_a_loop_not_a_page(server):
    for i in range(1, 6):
        server.add_item(i, utc(1))
    server.page_size = 2
    real = server._items

    def loop(seen, roles):
        status, headers, payload = real(seen, roles)
        import json as _json
        body = _json.loads(payload)
        body["@odata.nextLink"] = server.url + "/v1.0/sites/%s/lists/%s/items?expand=fields&$skiptoken=Mg" % (server.site, server.list_id)
        return status, headers, _json.dumps(body).encode()

    server._items = loop
    with pytest.raises(UpstreamBroken, match="names itself"):
        fetch(server, team=False)


def _items_page(items):
    return {"value": items}


def good_item(**kw):
    i = {"id": "1", "createdDateTime": "2026-10-02T08:00:00Z", "lastModifiedDateTime": "2026-10-02T09:00:00Z", "fields": {"Title": "x"}}
    i.update(kw)
    return i


@pytest.mark.parametrize("items", [
    [good_item(), good_item()],
    [good_item(id="")],
    [good_item(id=5)],
    [good_item(fields=None)],
    [good_item(createdDateTime="2026-10-02")],
    [good_item(createdDateTime="2026-13-02T08:00:00Z")],
    [good_item(lastModifiedDateTime="yesterday")],
    [good_item(createdDateTime="2026-10-02T10:00:00Z", lastModifiedDateTime="2026-10-02T09:00:00Z")],
    ["a string"],
])
def test_a_malformed_list_item_is_refused_and_a_good_one_passes(server, items):
    server.faults.append(Fault(200, _items_page([good_item()])))
    assert by_key(fetch(server, team=False))[("sp_register_items_created", 2)] == 1, "positive control"
    server.faults.append(Fault(200, _items_page(items)))
    with pytest.raises(UpstreamBroken):
        fetch(server, team=False)


def test_an_answer_with_no_value_list_is_refused(server):
    server.faults.append(Fault(200, {"unexpected": []}))
    with pytest.raises(UpstreamBroken, match="no value list"):
        fetch(server, team=False)


def test_a_message_with_no_body_or_a_repeated_id_is_refused(server):
    msg = {"id": "1", "messageType": "message", "createdDateTime": "2026-10-02T08:00:00Z", "lastModifiedDateTime": "2026-10-02T08:00:00Z",
           "deletedDateTime": None, "body": {"contentType": "html", "content": "hi"}, "from": None}
    server.faults.append(Fault(200, {"value": []}))
    server.faults.append(Fault(200, {"value": [msg]}))
    assert by_key(fetch(server))[("teams_channel_messages", 2)] == 1, "positive control"
    for bad in ([msg, msg], [dict(msg, body=None)], [dict(msg, body={"content": 3, "contentType": "html"})], [dict(msg, id="")]):
        server.faults.append(Fault(200, {"value": []}))
        server.faults.append(Fault(200, {"value": bad}))
        with pytest.raises(UpstreamBroken):
            fetch(server)


def test_a_cursor_and_an_empty_window_are_refused(server):
    seed(server)
    assert fetch(server)
    with pytest.raises(ValueError, match="no cursor"):
        run(collect(make(server), START, END, cursor="abc"))
    with pytest.raises(ValueError, match="at least one day"):
        run(collect(make(server), START, START))


@pytest.mark.parametrize("kw, match", [
    ({"tenant_id": "a/b"}, "tenant_id"),
    ({"tenant_id": "../x"}, "tenant_id"),
    ({"client_id": " "}, "client_id"),
    ({"client_secret": ""}, "client_secret"),
    ({"site_id": "has space"}, "site_id"),
    ({"list_id": ""}, "list_id"),
    ({"team_id": "t"}, "together"),
    ({"channel_id": "c"}, "together"),
    ({"graph_base": "http://graph.microsoft.com"}, "https"),
    ({"graph_base": "https://graph.microsoft.com/v1.0"}, "bare"),
    ({"login_base": "https://user:pw@login.microsoftonline.com"}, "bare"),
])
def test_bad_options_are_refused_and_good_ones_pass(server, kw, match):
    base = dict(tenant_id=server.tenant, client_id=CLIENT_ID, client_secret=SECRET, site_id=server.site, list_id=server.list_id)
    run(MicrosoftGraphConnector(**base).aclose())
    with pytest.raises(ValueError, match=match):
        MicrosoftGraphConnector(**{**base, **kw})


def test_an_app_without_the_permission_is_auth_revoked_for_each_surface(server):
    seed(server)
    server.add_file("d1", "i1", b"PK")
    assert fetch(server)
    for role, call in (("Sites.Read.All", lambda c: c.register_items()), ("ChannelMessage.Read.All", lambda c: c.channel_messages(window_start=START, window_end=END)),
                       ("Files.Read.All", lambda c: c.workbook_rows(drive_id="d1", item_id="i1"))):
        server.roles = {"Sites.Read.All", "ChannelMessage.Read.All", "Files.Read.All"} - {role}
        server.access_tokens.clear()
        conn = make(server)
        with pytest.raises(AuthRevoked, match="accessDenied"):
            run(_call(conn, call))


async def _call(conn, call):
    try:
        return await call(conn)
    finally:
        await conn.aclose()


def test_errors_quote_only_the_status_and_the_error_code_never_the_message(server):
    server.faults.append(Fault(404, graph_error("itemNotFound", "the secret detail tenant-data-here")))
    with pytest.raises(UpstreamBroken) as caught:
        run(_call(make(server), lambda c: c.register_items()))
    assert "HTTP 404 itemNotFound" in str(caught.value) and "secret detail" not in str(caught.value)
    server.faults.append(Fault(400, graph_error("bad code with spaces!", "x")))
    with pytest.raises(UpstreamBroken) as caught:
        run(_call(make(server), lambda c: c.register_items()))
    assert str(caught.value).endswith("(HTTP 400)"), "a code that is not a word is dropped, not quoted"


def test_the_secret_is_in_neither_the_repr_nor_an_error(server):
    conn = make(server)
    assert SECRET not in repr(conn) and SECRET not in repr(conn._tokens._secret)
    run(conn.aclose())
    with pytest.raises(AuthRevoked) as caught:
        fetch(server, client_secret="a-different-SECRET-value")
    assert "a-different-SECRET-value" not in str(caught.value)


# ---------------------------------------------------------------- retries

def test_a_429_with_a_short_retry_after_is_waited_for_once(server):
    seed(server)
    sleeps = Sleeps()
    conn = make(server, sleeps=sleeps, team=False)
    server.faults.append(Fault(429, graph_error("TooManyRequests"), {"Retry-After": "10"}))
    items = run(conn.register_items())
    run(conn.aclose())
    assert len(items) == 5 and sleeps.calls == [10.0]


def test_a_429_that_asks_for_longer_than_the_cap_is_handed_to_the_runner(server):
    seed(server)
    sleeps = Sleeps()
    conn = make(server, sleeps=sleeps, team=False)
    run(conn._tokens.access_token())
    server.faults.append(Fault(429, graph_error("TooManyRequests"), {"Retry-After": "300"}))
    with pytest.raises(RateLimited) as caught:
        run(conn.register_items())
    run(conn.aclose())
    assert caught.value.retry_after_seconds == 300.0 and sleeps.calls == []


def test_a_server_error_is_retried_a_bounded_number_of_times(server):
    seed(server)
    conn = make(server, team=False)
    run(conn._tokens.access_token())
    server.faults += [Fault(503, graph_error("serviceNotAvailable")), Fault(504, graph_error("gatewayTimeout"))]
    assert len(run(conn.register_items())) == 5
    server.faults += [Fault(503, graph_error("serviceNotAvailable")) for _ in range(3)]
    with pytest.raises(RateLimited, match="gave up after 3 attempts"):
        run(conn.register_items())
    run(conn.aclose())


# ---------------------------------------------------------------- the Excel register

def workbook_bytes(build):
    import openpyxl

    book = openpyxl.Workbook()
    build(book)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def register_book(book):
    ws = book.active
    ws.title = "Register"
    ws.append(["Job", "Client", "Fee", "Due"])
    ws.append(["24-101", "Council", 12500.5, datetime(2026, 11, 5)])
    ws.append([None, None, None, None])                                   # a blank row in the middle
    ws.append(["24-102", "Developer", None, None])
    other = book.create_sheet("Notes")
    other.append(["A", "B"])
    other.append(["x", "y"])
    from openpyxl.worksheet.table import Table

    other.add_table(Table(displayName="Open", ref="A1:B2"))


@pytest.fixture()
def book(server):
    server.add_file("drive-1", "item-1", workbook_bytes(register_book))
    return server


def read(server, **kw):
    return run(_call(make(server, team=False), lambda c: c.workbook_rows(drive_id="drive-1", item_id="item-1", **kw)))


def test_a_workbook_is_downloaded_and_read_as_rows_keyed_by_the_header_row(book):
    rows = read(book)
    assert [(r.row, dict(r.values)) for r in rows] == [
        (2, {"Job": "24-101", "Client": "Council", "Fee": 12500.5, "Due": datetime(2026, 11, 5)}),
        (4, {"Job": "24-102", "Client": "Developer", "Fee": None, "Due": None}),
    ], "the blank row is skipped and the row numbers are the sheet's own"


def test_a_named_sheet_and_a_named_table_are_read(book):
    assert [dict(r.values) for r in read(book, sheet="Notes")] == [{"A": "x", "B": "y"}]
    assert [dict(r.values) for r in read(book, table="Open")] == [{"A": "x", "B": "y"}]


def test_the_token_goes_to_graph_and_never_to_the_download_address_and_no_workbook_endpoint_is_called(book):
    read(book)
    assert book.leaked_token is False
    assert [r.path for r in book.requests if r.path.startswith("/v1.0/")] == ["/v1.0/drives/drive-1/items/item-1/content"]
    assert not [r for r in book.requests if "/workbook/" in r.path]
    assert [r.headers.get("authorization") for r in book.requests if r.path.startswith("/download/")] == [None]


def test_the_workbook_endpoints_are_refused_to_an_app_token_which_is_why_the_file_is_downloaded(book):
    conn = make(book, team=False)
    with pytest.raises(AuthRevoked, match="accessDenied"):
        run(conn._get("/v1.0/drives/drive-1/items/item-1/workbook/tables/Open/rows", {"$top": "5"}))
    run(conn.aclose())


@pytest.mark.parametrize("kw, match", [({"sheet": "Missing"}, "no sheet"), ({"table": "Missing"}, "no table")])
def test_a_sheet_or_table_that_is_not_there_is_refused(book, kw, match):
    assert read(book)
    with pytest.raises(UpstreamBroken, match=match):
        read(book, **kw)


def test_a_sheet_and_a_table_together_are_refused(book):
    with pytest.raises(ValueError, match="not both"):
        read(book, sheet="Notes", table="Open")


@pytest.mark.parametrize("header, match", [(["Job", None, "Fee"], "empty cell"), (["Job", "  ", "Fee"], "empty cell"), (["Job", "Fee", "Job"], "repeats")])
def test_a_header_row_that_would_lose_a_column_is_refused(server, header, match):
    def bad(book):
        ws = book.active
        ws.append(header)
        ws.append(["1", "2", "3"])

    server.add_file("drive-1", "item-1", workbook_bytes(lambda b: (b.active.append(["Job", "Client", "Fee"]), b.active.append([1, 2, 3]))))
    assert len(read(server)) == 1, "positive control"
    server.add_file("drive-1", "item-1", workbook_bytes(bad))
    with pytest.raises(UpstreamBroken, match=match):
        read(server)


def test_a_file_that_is_not_a_workbook_or_is_too_large_is_refused(server, monkeypatch):
    server.add_file("drive-1", "item-1", workbook_bytes(register_book))
    assert read(server)
    server.add_file("drive-1", "item-1", b"%PDF-1.7 not a workbook")
    with pytest.raises(UpstreamBroken, match="not an .xlsx"):
        read(server)
    server.add_file("drive-1", "item-1", b"PK\x03\x04 but not really a zip")
    with pytest.raises(UpstreamBroken, match="could not be read"):
        read(server)
    server.add_file("drive-1", "item-1", workbook_bytes(register_book))
    monkeypatch.setattr(msgraph, "MAX_WORKBOOK_BYTES", 100)
    with pytest.raises(UpstreamBroken, match="larger than"):
        read(server)


def test_a_missing_file_is_upstream_broken_and_a_redirect_with_no_https_location_is_refused(server):
    with pytest.raises(UpstreamBroken, match="itemNotFound"):
        read(server)
    server.add_file("drive-1", "item-1", workbook_bytes(register_book))
    assert read(server)
    real = server._content

    def bad_location(drive, item, roles):
        return 302, {"Location": "ftp://files.example.test/x"}, b""

    server._content = bad_location
    with pytest.raises(UpstreamBroken, match="no usable https Location"):
        read(server)
    server._content = real


@pytest.mark.parametrize("kw", [{"drive_id": "has space", "item_id": "i"}, {"drive_id": "d", "item_id": ""}])
def test_a_drive_or_item_id_that_is_not_an_id_is_refused(server, kw):
    conn = make(server, team=False)
    with pytest.raises(ValueError, match="not a usable Graph id"):
        run(conn.workbook_rows(**kw))
    run(conn.aclose())


# ---------------------------------------------------------------- datetimes

@pytest.mark.parametrize("text, expected", [
    ("2021-03-28T21:11:12.395Z", datetime(2021, 3, 28, 21, 11, 12, 395000, tzinfo=timezone.utc)),
    ("2026-10-02T08:00:00Z", datetime(2026, 10, 2, 8, tzinfo=timezone.utc)),
    ("2026-10-02T08:00:00.1234567Z", datetime(2026, 10, 2, 8, 0, 0, 123456, tzinfo=timezone.utc)),
    ("2026-10-02T01:30:00+08:00", datetime(2026, 10, 1, 17, 30, tzinfo=timezone.utc)),
    ("2026-10-01T23:30:00-05:00", datetime(2026, 10, 2, 4, 30, tzinfo=timezone.utc)),
])
def test_a_graph_datetime_is_read_into_the_utc_instant(text, expected):
    assert parse_graph_datetime(text, "x") == expected


@pytest.mark.parametrize("text", ["2026-10-02", "2026-10-02T08:00:00", "2026-10-02 08:00:00Z", "2026-02-30T08:00:00Z", "2026-10-02T25:00:00Z", None, 5, ""])
def test_a_text_that_is_not_an_iso_instant_is_refused(text):
    with pytest.raises(UpstreamBroken):
        parse_graph_datetime(text, "x")


def test_a_row_stamped_in_another_zone_lands_on_its_utc_day(server):
    server.items.append({"id": "9", "createdDateTime": "2026-10-02T01:30:00+08:00", "lastModifiedDateTime": "2026-10-02T01:30:00+08:00", "fields": {}})
    got = by_key(fetch(server, team=False))
    assert got[("sp_register_items_created", 1)] == 1 and got[("sp_register_items_created", 2)] == 0, "01:30 in Manila is the evening before in UTC"


# ---------------------------------------------------------------- seeded worlds against a brute force oracle

@pytest.mark.parametrize("seed_value", range(40))
def test_seeded_worlds_match_the_brute_force_oracle_over_pages_zones_and_faults(seed_value):
    rng = random.Random(seed_value)
    with FakeGraph(client_secret=SECRET) as s:
        start = date(2026, 10, 1) + timedelta(days=rng.randint(0, 5))
        end = start + timedelta(days=rng.randint(1, 8))
        created, modified, messages = [], [], []
        for i in range(rng.randint(0, 45)):
            c = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=rng.randint(-2, (end - start).days + 1), seconds=rng.choice([0, 1, 86399, rng.randint(0, 86399)]))
            m = c + timedelta(seconds=rng.choice([0, 0, 1, 90000, rng.randint(0, 400000)]))
            s.add_item(i + 1, c, modified=m)
            created.append(c.date())
            modified.append(m.date())
        for i in range(rng.randint(0, 130)):
            c = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=rng.randint(-2, (end - start).days + 1), seconds=rng.randint(0, 86399))
            kind = rng.choice(["message"] * 6 + ["systemEventMessage"])
            deleted = c + timedelta(hours=1) if rng.random() < 0.1 else None
            s.add_message(1000 + i, c, modified=c + timedelta(seconds=rng.randint(0, 900000)), kind=kind, deleted=deleted)
            if kind == "message" and deleted is None:
                messages.append(c.date())
        s.page_size = rng.choice([1, 2, 5, 200])
        s.faults += [Fault(rng.choice([503, 504]), graph_error("serviceNotAvailable")) for _ in range(rng.randint(0, 2))]
        got = {(p.metric_key, p.observed_on): p.value_numeric for p in points_of(run(collect(make(s), start, end)))}
        day = start
        while day < end:
            assert got[("sp_register_items_created", day)] == created.count(day), (seed_value, day)
            assert got[("sp_register_items_modified", day)] == modified.count(day), (seed_value, day)
            assert got[("teams_channel_messages", day)] == messages.count(day), (seed_value, day)
            day += timedelta(days=1)


# ---------------------------------------------------------------- the registry and the migration

def test_the_connector_is_registered_with_three_metrics_and_one_secret_beside_the_ids():
    spec = registry.get("msgraph")
    assert spec.supported_metrics == frozenset(METRICS) and registry.secret_keys("msgraph") == frozenset({"client_secret"})
    redacted = registry.redact("msgraph", {"tenant_id": "t", "client_id": "c", "client_secret": SECRET, "site_id": "s", "list_id": "l"})
    assert redacted["client_secret"] == "***" and redacted["tenant_id"] == "t"
    built = registry.build("msgraph", tenant_id="contoso.onmicrosoft.com", client_id="c", client_secret=SECRET, site_id="s", list_id="l")
    assert isinstance(built, MicrosoftGraphConnector)


def test_migration_018_inserts_every_key_this_connector_writes():
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "018_harvest_and_graph_metrics.sql").read_text(encoding="utf-8")
    for key in METRICS:
        assert "('%s'," % key in sql, key


def test_from_env_builds_the_connection_and_names_what_is_missing(server):
    env = {"MSGRAPH_TENANT_ID": server.tenant, "MSGRAPH_CLIENT_ID": CLIENT_ID, "MSGRAPH_CLIENT_SECRET": SECRET, "MSGRAPH_SITE_ID": server.site, "MSGRAPH_LIST_ID": server.list_id,
           "MSGRAPH_GRAPH_BASE": server.url, "MSGRAPH_LOGIN_BASE": server.url}
    server.add_item(1, utc(1))
    conn = MicrosoftGraphConnector.from_env(env, client=client(), retrier=retrier())
    assert len(run(_call(conn, lambda c: c.register_items()))) == 1
    with pytest.raises(ValueError, match="MSGRAPH_LIST_ID"):
        MicrosoftGraphConnector.from_env({k: v for k, v in env.items() if k != "MSGRAPH_LIST_ID"})
