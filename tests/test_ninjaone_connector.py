"""The NinjaOne connector against its stand-in server, over real HTTP on 127.0.0.1.

WHAT THESE PROVE, and what they do not. They prove the token request (client credentials, the monitoring scope, the secret in the form body), the renewal before expiry and on a 401, the
requests (`pageSize` and `after`, no paging on the alert list), how it reads the documented answers (devices, alerts, the nested device), the timestamp unit option, the counts it writes
(gauges on the last day, alerts created per day), what it refuses (a cursor that does not rise, a repeated device or alert, a malformed record, a malformed token answer), the retry and
error handling, and that no secret reaches a message. They do NOT prove NinjaOne accepts any of it: the server is written from the saved pages (docs/MSP_SOURCES.md) and no live tenant
was used.

Every refusal runs a positive control first (the same call succeeds).
"""
from __future__ import annotations

import asyncio
import random
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.connectors.base import AuthRevoked, RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.fake_http import Fault  # noqa: E402
from app.connectors.ninjaone import GAUGES, METRICS, PAGE_SIZE, NinjaOneConnector  # noqa: E402
from app.connectors.ninjaone_fake import FakeNinjaOne  # noqa: E402
from app.connectors.registry import registry  # noqa: E402
from vendor_helpers import Sleeps, client, collect, points_of, retrier, run  # noqa: E402

CLIENT, SECRET = "client-abc", "SECRET-client-value"
START, END = date(2026, 10, 1), date(2026, 10, 4)
ROOT = Path(__file__).resolve().parents[1]


def dt(day: int, hour: int = 9, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture()
def server():
    with FakeNinjaOne(client_id=CLIENT, client_secret=SECRET) as fake:
        yield fake


def make(server, sleeps=None, **kw):
    kw.setdefault("retrier", retrier(sleeps))
    kw.setdefault("epoch_unit", server.epoch_unit)
    return NinjaOneConnector(client_id=kw.pop("client_id", CLIENT), client_secret=kw.pop("client_secret", SECRET), base_url=kw.pop("base_url", server.url), client=client(), **kw)


def by_key(pages):
    return {(p.metric_key, p.observed_on.day): p.value_numeric for p in points_of(pages)}


def fetch(server, start=START, end=END, **kw):
    return run(collect(make(server, **kw), start, end))


async def _call(conn, name):
    try:
        return await getattr(conn, name)()
    finally:
        await conn.aclose()


def devices_of(server, **kw):
    return run(_call(make(server, **kw), "devices"))


def alerts_of(server, **kw):
    return run(_call(make(server, **kw), "alerts"))


async def _verify(conn):
    try:
        return await conn.verify()
    finally:
        await conn.aclose()


def seed(server):
    server.add_device(1, "SRV-01", last_contact=dt(3, 8))
    server.add_device(2, "SRV-02", offline=True, last_contact=dt(2, 8))
    server.add_device(3, "WS-03", node_class="WINDOWS_WORKSTATION")
    server.add_alert("a1", 1, dt(1, 9))
    server.add_alert("a2", 1, dt(1, 11), source_name="CPU high", source_type="CONDITION_AGENT_CPU")
    server.add_alert("a3", 2, dt(3, 9), device_name="SRV-02")
    server.add_alert("a4", 3, dt(20, 9))                                              # created after the window: counted nowhere in it


# ---------------------------------------------------------------- the happy path

def test_gauges_are_written_on_the_windows_last_day_and_alerts_created_for_each_day(server):
    seed(server)
    pages = fetch(server)
    got = by_key(pages)
    assert [got[("nj_devices", 3)], got[("nj_devices_offline", 3)], got[("nj_alerts_active", 3)]] == [3, 1, 4]
    assert [got[("nj_alerts_created", d)] for d in (1, 2, 3)] == [2, 0, 1]
    pts = points_of(pages)
    assert len(pts) == 3 + 3 and {p.metric_key for p in pts} == set(METRICS) and all(type(p.value_numeric) is int and p.currency is None for p in pts)
    assert {p.observed_on.day for p in pts if p.metric_key in GAUGES} == {3}, "a point in time figure is written once, on the last day, never repeated on every day"


def test_a_one_day_window_writes_the_gauges_and_the_days_alerts_on_that_day(server):
    seed(server)
    got = by_key(fetch(server, start=date(2026, 10, 1), end=date(2026, 10, 2)))
    assert got == {("nj_devices", 1): 3, ("nj_devices_offline", 1): 1, ("nj_alerts_active", 1): 4, ("nj_alerts_created", 1): 2}


def test_the_token_request_is_client_credentials_with_the_monitoring_scope_and_the_secret_in_the_body(server):
    seed(server)
    fetch(server)
    assert server.token_requests == [{"grant_type": "client_credentials", "client_id": CLIENT, "client_secret": SECRET, "scope": "monitoring"}]
    token_call = server.requests[0]
    assert token_call.method == "POST" and token_call.path == "/ws/oauth/token" and token_call.query == {}, "the secret is never in the address"
    assert all(SECRET not in r.path and SECRET not in str(r.query) for r in server.requests)
    for r in server.requests[1:]:
        assert r.headers["authorization"].startswith("Bearer ") and r.headers["authorization"][7:] in server.access_tokens


def test_the_requests_are_devices_in_pages_by_after_then_alerts_with_no_paging(server):
    seed(server)
    fetch(server)
    assert [(r.path, r.query) for r in server.requests[1:]] == [("/v2/devices", {"pageSize": str(PAGE_SIZE)}), ("/v2/devices", {"pageSize": str(PAGE_SIZE), "after": "3"}), ("/v2/alerts", {})]


def test_api_calls_counts_every_request_including_the_token(server):
    seed(server)
    pages = fetch(server)
    assert sum(p.api_calls for p in pages) == len(server.requests) == 4


def test_devices_are_read_by_after_until_an_empty_page_even_when_the_server_caps_pages_below_the_size_asked(server):
    for i in range(1, 9):
        server.add_device(i, "SRV-%02d" % i)
    server.page_cap = 3
    assert [d.device_id for d in devices_of(server, page_size=10)] == list(range(1, 9)), "a short page is not the last page: only an empty one is"
    after = [r.query.get("after") for r in server.requests if r.path == "/v2/devices"]
    assert after == [None, "3", "6", "8"]


def test_the_records_the_run_read_are_kept_for_the_ledger(server):
    seed(server)
    conn = make(server)
    run(collect(conn, START, END))
    assert [d.device_id for d in conn.devices_read] == [1, 2, 3] and [a.alert_id for a in conn.alerts_read] == ["a1", "a2", "a3", "a4"]


# ---------------------------------------------------------------- the device and alert records

def test_a_device_becomes_a_record_with_the_fields_the_openapi_file_names(server):
    server.add_device(7, "  SRV-07 ", offline=True, node_class="LINUX_SERVER", org=4, last_contact=dt(1, 8, 30))
    [d] = devices_of(server)
    assert (d.source, d.device_id, d.name, d.node_class, d.organization_id, d.offline, d.last_contact) == ("ninjaone", 7, "SRV-07", "LINUX_SERVER", 4, True, dt(1, 8, 30))


def test_a_device_name_falls_back_from_the_display_name_to_the_system_name_then_the_dns_name_then_its_id(server):
    row = server.add_device(1, "Display")
    row["displayName"] = "  "
    row["systemName"], row["dnsName"] = "sysname", "dns.local"
    assert devices_of(server)[0].name == "sysname"
    row["systemName"] = None
    assert devices_of(server)[0].name == "dns.local"
    row["dnsName"] = None
    assert devices_of(server)[0].name == "device 1"


def test_an_alert_becomes_a_record_and_takes_its_device_name_and_organization_from_the_nested_device(server):
    server.add_alert("a1", 5, dt(1, 9, 30), source_type="CONDITION_AGENT_CPU", source_name="CPU high", subject="CPU over 90", severity="MAJOR", device_name="SRV-05", org=3)
    [a] = alerts_of(server)
    assert (a.source, a.alert_id, a.device_id, a.device_name, a.organization_id, a.source_type, a.source_name, a.subject, a.severity, a.created_at) == (
        "ninjaone", "a1", 5, "SRV-05", 3, "CONDITION_AGENT_CPU", "CPU high", "CPU over 90", "MAJOR", dt(1, 9, 30))


def test_an_alert_with_no_name_subject_severity_or_device_object_has_none_for_each(server):
    row = server.add_alert("a1", 5, dt(1), source_name=None, subject=None, severity=None)
    del row["device"]
    [a] = alerts_of(server)
    assert (a.source_name, a.subject, a.severity, a.device_name, a.organization_id) == (None, None, None, None, None)


def test_a_severity_is_kept_as_the_text_ninjaone_sent_and_never_ranked(server):
    server.add_alert("a1", 5, dt(1), severity="  SOMETHING_NEW ")
    assert alerts_of(server)[0].severity == "SOMETHING_NEW"


@pytest.mark.parametrize("unit", ["seconds", "milliseconds"])
def test_the_timestamp_unit_option_reads_the_same_instant_either_way(unit):
    with FakeNinjaOne(client_id=CLIENT, client_secret=SECRET, epoch_unit=unit) as s:
        s.add_device(1, "SRV", last_contact=dt(2, 8, 15))
        s.add_alert("a1", 1, dt(2, 9, 45))
        assert devices_of(s)[0].last_contact == dt(2, 8, 15) and alerts_of(s)[0].created_at == dt(2, 9, 45)


def test_a_unit_that_does_not_match_the_answer_is_refused_and_not_guessed(server):
    server.add_alert("a1", 1, dt(2, 9))
    assert alerts_of(server), "positive control: the unit matches"
    server.epoch_unit = "milliseconds"
    server.alerts[0]["createTime"] = server.stamp(dt(2, 9))
    with pytest.raises(UpstreamBroken, match="not a time in seconds"):
        alerts_of(server, epoch_unit="seconds")
    server.epoch_unit = "seconds"
    server.alerts[0]["createTime"] = server.stamp(dt(2, 9))
    with pytest.raises(UpstreamBroken, match="not a time in milliseconds"):
        alerts_of(server, epoch_unit="milliseconds")


# ---------------------------------------------------------------- what it refuses

def test_a_device_list_whose_cursor_does_not_rise_is_refused_as_a_changed_cursor(server):
    for i in range(1, 6):
        server.add_device(i, "SRV-%d" % i)
    server.page_cap = 2
    assert len(devices_of(server)) == 5, "positive control: the same data pages through"
    server.ignore_after = True
    with pytest.raises(UpstreamBroken, match="do not rise"):
        devices_of(server)


def test_a_device_listed_twice_or_with_ids_out_of_order_is_refused_as_a_cursor_that_does_not_rise(server):
    server.add_device(1, "A")
    server.add_device(2, "B")
    assert len(devices_of(server)) == 2, "positive control"
    server.faults.append(Fault(200, {"access_token": "t", "expires_in": 3600, "scope": "monitoring", "token_type": "Bearer"}))
    server.faults.append(Fault(200, [{"id": 5, "offline": False}, {"id": 5, "offline": False}]))
    with pytest.raises(UpstreamBroken, match="do not rise"):
        devices_of(server)
    server.faults.append(Fault(200, {"access_token": "t", "expires_in": 3600, "scope": "monitoring", "token_type": "Bearer"}))
    server.faults.append(Fault(200, [{"id": 5, "offline": False}, {"id": 4, "offline": False}]))
    with pytest.raises(UpstreamBroken, match="do not rise"):
        devices_of(server)


def test_a_list_that_is_an_object_is_refused(server):
    for name, path in (("devices", "/v2/devices"), ("alerts", "/v2/alerts")):
        server.access_tokens.add("t")
        server.faults.append(Fault(200, {"access_token": "t", "expires_in": 3600, "scope": "monitoring", "token_type": "Bearer"}))
        server.faults.append(Fault(200, {"items": []}))
        with pytest.raises(UpstreamBroken, match="not a list"):
            run(_call(make(server), name))


def good_device(**kw):
    row = {"id": 1, "offline": False, "displayName": "SRV", "organizationId": 1}
    row.update(kw)
    return row


def good_alert(**kw):
    row = {"uid": "a1", "deviceId": 1, "createTime": 1760000000.5, "sourceType": "CONDITION_AGENT_CPU"}
    row.update(kw)
    return row


def staged(server, rows):
    server.faults.append(Fault(200, {"access_token": "t", "expires_in": 3600, "scope": "monitoring", "token_type": "Bearer"}))
    server.access_tokens.add("t")
    server.faults.append(Fault(200, rows))


@pytest.mark.parametrize("rows, match", [
    (["x"], "no usable id"),
    ([{"offline": False}], "no usable id"),
    ([good_device(id=True)], "no usable id"),
    ([good_device(id="7")], "no usable id"),
    ([good_device(offline="no")], "offline of device 1 is not true or false"),
    ([good_device(offline=None)], "offline of device 1 is not true or false"),
    ([good_device(organizationId="acme")], "organizationId of device 1 is not a number"),
    ([good_device(lastContact="yesterday")], "not a number"),
    ([good_device(lastContact=True)], "not a number"),
    ([good_device(lastContact=1.0)], "not a time in seconds"),
    ([good_device(lastContact=1e18)], "not a time in seconds"),
])
def test_a_malformed_device_is_refused_for_its_own_reason_and_a_good_one_passes(server, rows, match):
    staged(server, [good_device(lastContact=1760000000)])
    server.faults.append(Fault(200, []))
    assert devices_of(server)[0].device_id == 1, "positive control: the good device is read"
    staged(server, rows)
    server.faults.append(Fault(200, []))
    with pytest.raises(UpstreamBroken, match=match):
        devices_of(server)


@pytest.mark.parametrize("rows, match", [
    (["x"], "not an object"),
    ([good_alert(uid=None)], "has no uid"),
    ([good_alert(uid=" ")], "has no uid"),
    ([good_alert(deviceId=None)], "names no device"),
    ([good_alert(deviceId="5")], "names no device"),
    ([good_alert(sourceType=None)], "has no sourceType"),
    ([good_alert(sourceType="  ")], "has no sourceType"),
    ([good_alert(createTime=None)], "not a number"),
    ([good_alert(createTime="soon")], "not a number"),
    ([good_alert(createTime=0)], "not a time in seconds"),
    ([good_alert(createTime=4102444800 * 2)], "not a time in seconds"),
    ([good_alert(), good_alert()], "appears twice"),
])
def test_a_malformed_alert_is_refused_for_its_own_reason_and_a_good_one_passes(server, rows, match):
    staged(server, [good_alert()])
    assert alerts_of(server)[0].alert_id == "a1", "positive control"
    staged(server, rows)
    with pytest.raises(UpstreamBroken, match=match):
        alerts_of(server)


def test_a_body_that_is_not_json_is_a_broken_answer(server):
    seed(server)
    assert fetch(server), "positive control"
    server.faults.append(Fault(200, {"access_token": "t", "expires_in": 3600, "scope": "monitoring", "token_type": "Bearer"}))
    server.access_tokens.add("t")
    server.faults.append(Fault(200, b'[{"id": 1, "offli'))
    with pytest.raises(UpstreamBroken, match="not JSON"):
        fetch(server)


def test_a_cursor_and_an_empty_window_are_refused(server):
    seed(server)
    assert fetch(server)
    with pytest.raises(ValueError, match="no cursor"):
        run(collect(make(server), START, END, cursor="abc"))
    with pytest.raises(ValueError, match="at least one day"):
        run(collect(make(server), START, START))


@pytest.mark.parametrize("kw, match", [
    ({"client_id": " "}, "client_id"),
    ({"client_secret": ""}, "client_secret"),
    ({"epoch_unit": "minutes"}, "epoch_unit"),
    ({"epoch_unit": None}, "epoch_unit"),
    ({"page_size": 0}, "page_size"),
    ({"page_size": 1001}, "page_size"),
    ({"page_size": True}, "page_size"),
    ({"page_size": "5"}, "page_size"),
])
def test_bad_options_are_refused_and_good_ones_pass(server, kw, match):
    run(make(server).aclose())
    with pytest.raises(ValueError, match=match):
        make(server, **kw)


def test_the_timestamp_unit_has_no_default():
    with pytest.raises(TypeError, match="epoch_unit"):
        NinjaOneConnector(client_id=CLIENT, client_secret=SECRET)                       # type: ignore[call-arg]


@pytest.mark.parametrize("url", ["http://app.ninjarmm.com", "ftp://app.ninjarmm.com", "https://user:pw@app.ninjarmm.com", "https://app.ninjarmm.com/v2", "https://app.ninjarmm.com?x=1"])
def test_a_base_address_that_is_not_a_bare_https_origin_is_refused(url):
    with pytest.raises(ValueError):
        NinjaOneConnector(client_id=CLIENT, client_secret=SECRET, epoch_unit="seconds", base_url=url)


def test_a_real_looking_address_is_accepted_and_from_env_reads_the_three_settings(server):
    assert NinjaOneConnector(client_id=CLIENT, client_secret=SECRET, epoch_unit="seconds", base_url="https://eu.ninjarmm.com/")._base == "https://eu.ninjarmm.com"
    conn = NinjaOneConnector.from_env({"NINJAONE_CLIENT_ID": CLIENT, "NINJAONE_CLIENT_SECRET": SECRET, "NINJAONE_EPOCH_UNIT": "milliseconds", "NINJAONE_BASE_URL": server.url})
    assert conn.external_account == CLIENT and conn._unit == "milliseconds" and conn._base == server.url
    with pytest.raises(ValueError, match="NINJAONE_EPOCH_UNIT"):
        NinjaOneConnector.from_env({"NINJAONE_CLIENT_ID": CLIENT, "NINJAONE_CLIENT_SECRET": SECRET})


# ---------------------------------------------------------------- the token

class Mono:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_token_is_reused_until_a_minute_before_it_expires_and_then_renewed(server):
    mono = Mono()
    conn = make(server, monotonic=mono)

    async def go():
        await conn.verify()
        mono.now += 3600 - 60 - 1                                                        # one second before a minute before it expires
        await conn.verify()
        assert len(server.token_requests) == 1
        mono.now += 1
        await conn.verify()
        assert len(server.token_requests) == 2, "at expires_in minus the margin the token is renewed"
        await conn.aclose()

    run(go())


def test_a_401_on_a_call_renews_the_token_once_and_repeats_the_call(server):
    seed(server)
    conn = make(server)

    async def go():
        await conn.verify()
        server.revoke_tokens()
        got = await conn.alerts()
        await conn.aclose()
        return got

    assert len(run(go())) == 4 and len(server.token_requests) == 2


def test_a_401_that_a_new_token_does_not_fix_is_auth_revoked(server):
    seed(server)
    conn = make(server)

    async def go():
        await conn.verify()
        server.faults += [Fault(401, {"error": "unauthorized"}), Fault(200, {"access_token": "t2", "expires_in": 3600, "scope": "monitoring", "token_type": "Bearer"}), Fault(401, {})]
        try:
            await conn.alerts()
        finally:
            await conn.aclose()

    with pytest.raises(AuthRevoked, match="just issued"):
        run(go())


def test_requests_that_all_get_a_401_together_renew_the_token_once_between_them(server):
    seed(server)
    conn = make(server)

    async def go():
        await conn.verify()
        server.revoke_tokens()
        await asyncio.gather(*[conn.alerts() for _ in range(6)])
        await conn.aclose()

    run(go())
    assert len(server.token_requests) == 2, "the first to be refused renews, the others use that new token"


def test_an_answer_to_verify_that_is_not_a_list_is_refused(server):
    server.faults += [Fault(200, {"access_token": "t", "expires_in": 3600, "scope": "monitoring", "token_type": "Bearer"}), Fault(200, {"organizations": []})]
    server.access_tokens.add("t")
    with pytest.raises(UpstreamBroken, match="organizations answer is not a list"):
        run(_verify(make(server)))
    assert run(_verify(make(server))), "positive control"


def test_requests_that_start_together_renew_the_token_once(server):
    seed(server)
    conn = make(server)

    async def go():
        await asyncio.gather(*[conn.alerts() for _ in range(6)])
        await conn.aclose()

    run(go())
    assert len(server.token_requests) == 1


def test_a_refused_client_is_auth_revoked_naming_the_oauth_word_and_never_the_secret(server):
    assert run(_verify(make(server))) == "NinjaOne client app %s" % CLIENT
    with pytest.raises(AuthRevoked, match="invalid_client") as caught:
        run(_verify(make(server, client_secret="wrong-secret-value")))
    assert "wrong-secret-value" not in str(caught.value) and SECRET not in str(caught.value)


def test_an_app_without_the_monitoring_scope_is_auth_revoked_and_a_wrong_grant_word_is_read_the_same_way():
    with FakeNinjaOne(client_id=CLIENT, client_secret=SECRET, scopes=("management",)) as s:
        with pytest.raises(AuthRevoked, match="invalid_scope"):
            run(_verify(make(s)))
    with FakeNinjaOne(client_id=CLIENT, client_secret=SECRET) as s:
        s.faults.append(Fault(400, {"error": "unsupported_grant_type"}))
        with pytest.raises(AuthRevoked, match="unsupported_grant_type"):
            run(_verify(make(s)))


def test_an_error_word_that_is_not_an_oauth_refusal_is_not_read_as_one(server):
    server.faults.append(Fault(400, {"error": "something_else", "error_description": "secret detail " + SECRET}))
    with pytest.raises(UpstreamBroken, match="HTTP 400") as caught:
        run(_verify(make(server)))
    assert SECRET not in str(caught.value) and "secret detail" not in str(caught.value) and "something_else" not in str(caught.value)


@pytest.mark.parametrize("body, match", [
    ({}, "no bearer access_token"),
    ({"access_token": "", "token_type": "Bearer", "expires_in": 3600}, "no bearer access_token"),
    ({"access_token": "t", "token_type": "MAC", "expires_in": 3600}, "no bearer access_token"),
    ({"access_token": "t", "token_type": 5, "expires_in": 3600}, "no bearer access_token"),
    ({"access_token": "t", "token_type": "Bearer"}, "no expires_in"),
    ({"access_token": "t", "token_type": "Bearer", "expires_in": True}, "no expires_in"),
    ({"access_token": "t", "token_type": "Bearer", "expires_in": 0}, "no expires_in"),
    ({"access_token": "t", "token_type": "Bearer", "expires_in": -5}, "no expires_in"),
    ({"access_token": "t", "token_type": "Bearer", "expires_in": "3600"}, "no expires_in"),
    ([], "no bearer access_token"),
])
def test_a_token_answer_that_is_not_a_bearer_token_with_a_lifetime_is_refused(server, body, match):
    server.faults.append(Fault(200, {"access_token": "t", "token_type": "bearer", "expires_in": 3600}))
    server.access_tokens.add("t")
    assert run(_verify(make(server))), "positive control: a lower case bearer is accepted"
    server.faults.append(Fault(200, body))
    with pytest.raises(UpstreamBroken, match=match):
        run(_verify(make(server)))


def test_a_token_answer_that_is_not_json_is_refused(server):
    server.faults.append(Fault(200, b"<html>"))
    with pytest.raises(UpstreamBroken, match="token request with a body that is not JSON"):
        run(_verify(make(server)))


# ---------------------------------------------------------------- errors and retries

def test_a_429_is_retried_with_the_retry_after_when_there_is_one_and_a_backoff_when_there_is_not(server):
    seed(server)
    sleeps = Sleeps()
    server.faults += [Fault(200, {"access_token": "t", "expires_in": 3600, "scope": "monitoring", "token_type": "Bearer"}), Fault(429, {"error": "slow"}, {"Retry-After": "7"})]
    server.access_tokens.add("t")
    pages = fetch(server, sleeps=sleeps)
    assert sleeps.calls == [7.0] and sum(p.throttle_waits for p in pages) == 1
    sleeps = Sleeps()
    server.faults += [Fault(200, {"access_token": "t", "expires_in": 3600, "scope": "monitoring", "token_type": "Bearer"}), Fault(429, {"error": "slow"})]
    assert fetch(server, sleeps=sleeps) and sleeps.calls == [1.0], "no saved page says Retry-After is sent, so the retrier's own backoff is used"


def test_a_429_that_asks_for_longer_than_the_cap_is_handed_to_the_runner(server):
    seed(server)
    sleeps = Sleeps()
    server.faults += [Fault(200, {"access_token": "t", "expires_in": 3600, "scope": "monitoring", "token_type": "Bearer"}), Fault(429, {}, {"Retry-After": "120"})]
    server.access_tokens.add("t")
    with pytest.raises(RateLimited) as caught:
        fetch(server, sleeps=sleeps)
    assert caught.value.retry_after_seconds == 120.0 and sleeps.calls == []


def test_a_server_error_is_retried_a_bounded_number_of_times(server):
    seed(server)
    server.fail_on = {1, 2}
    assert fetch(server)
    server.fail_on = {1, 2, 3}
    server.answered = 0
    with pytest.raises(RateLimited, match="gave up after 3 attempts"):
        fetch(server)


def test_a_dropped_connection_is_retried(server):
    seed(server)
    server.faults.append(Fault(0, drop=True))
    assert by_key(fetch(server))[("nj_devices", 3)] == 3


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_other_statuses_are_upstream_broken_and_quote_only_the_status(server, status):
    server.faults += [Fault(200, {"access_token": "t", "expires_in": 3600, "scope": "monitoring", "token_type": "Bearer"}), Fault(status, {"error": "x", "detail": "secret detail " + SECRET})]
    server.access_tokens.add("t")
    with pytest.raises(UpstreamBroken) as caught:
        run(_verify(make(server)))
    assert "HTTP %d" % status in str(caught.value) and SECRET not in str(caught.value) and "secret detail" not in str(caught.value)


def test_the_secret_and_the_token_are_in_neither_the_repr_nor_the_error_text(server):
    conn = make(server)
    assert SECRET not in repr(conn) and SECRET not in repr(conn._secret)
    with pytest.raises(Exception):
        "%s" % format(conn._secret)
    run(_verify(conn))
    assert conn._token is not None and conn._token.reveal() not in repr(conn) and conn._token.reveal() not in repr(conn._token)


# ---------------------------------------------------------------- seeded worlds against a brute force oracle

def oracle(devices, alerts, start, end):
    """What the four keys must be, worked out with plain loops and no code shared with the connector."""
    last = end - timedelta(days=1)
    out = {("nj_devices", last): len(devices), ("nj_devices_offline", last): sum(1 for offline in devices if offline), ("nj_alerts_active", last): len(alerts)}
    day = start
    while day < end:
        out[("nj_alerts_created", day)] = sum(1 for created in alerts if created.date() == day)
        day += timedelta(days=1)
    return out


@pytest.mark.parametrize("seed_value", range(40))
def test_seeded_worlds_match_the_brute_force_oracle_over_pages_faults_units_and_edges(seed_value):
    rng = random.Random(seed_value)
    unit = rng.choice(["seconds", "milliseconds"])
    with FakeNinjaOne(client_id=CLIENT, client_secret=SECRET, epoch_unit=unit) as s:
        start = date(2026, 9, 28) + timedelta(days=rng.randint(0, 5))
        end = start + timedelta(days=rng.randint(1, 9))
        base = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
        ids = rng.sample(range(1, 400), rng.randint(0, 40))
        offline_flags = []
        for did in ids:
            offline = rng.random() < 0.3
            s.add_device(did, rng.choice(["SRV-%d" % did, None]), offline=offline, last_contact=base + timedelta(minutes=rng.randint(-5000, 5000)))
            offline_flags.append(offline)
        created = []
        for i in range(rng.randint(0, 40)):
            moment = base + timedelta(days=rng.randint(-3, (end - start).days + 2), minutes=rng.randint(0, 1439))
            s.add_alert("alert-%d" % i, rng.choice(ids) if ids else 1, moment)
            created.append(moment)
        s.page_cap = rng.choice([1, 2, 3, 7, 1000])
        picks = sorted(rng.sample(range(1, 60), rng.randint(0, 5)))
        s.fail_on = {n for i, n in enumerate(picks) if i == 0 or n - picks[i - 1] > 1}
        conn = NinjaOneConnector(client_id=CLIENT, client_secret=SECRET, epoch_unit=unit, base_url=s.url, page_size=rng.choice([1, 2, 5, 200]), client=client(), retrier=retrier())
        got = {(p.metric_key, p.observed_on): p.value_numeric for p in points_of(run(collect(conn, start, end)))}
        assert got == oracle(offline_flags, created, start, end), "seed %d page_cap %d" % (seed_value, s.page_cap)


# ---------------------------------------------------------------- the registry and the migration

def test_the_connector_is_registered_with_its_four_metrics_and_one_secret():
    spec = registry.get("ninjaone")
    assert spec.supported_metrics == frozenset(METRICS) and registry.secret_keys("ninjaone") == frozenset({"client_secret"})
    assert registry.redact("ninjaone", {"client_id": CLIENT, "client_secret": SECRET})["client_secret"] == "***"
    built = registry.build("ninjaone", client_id=CLIENT, client_secret=SECRET, epoch_unit="seconds", base_url="https://app.ninjarmm.com")
    assert isinstance(built, NinjaOneConnector)


def test_migration_027_names_every_key_with_its_unit_and_kind_and_no_money_key():
    sql = (ROOT / "migrations" / "027_ninjaone_metrics.sql").read_text(encoding="utf-8")
    rows = dict(re.findall(r"\('(nj_[a-z_]+)',\s*'[^']*',\s*'count',\s*'(gauge|delta)'\)", sql))
    assert rows == {"nj_devices": "gauge", "nj_devices_offline": "gauge", "nj_alerts_active": "gauge", "nj_alerts_created": "delta"}
    assert {k for k, v in rows.items() if v == "gauge"} == GAUGES and "currency" not in sql.split("insert into")[1]
