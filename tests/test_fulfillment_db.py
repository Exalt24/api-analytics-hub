"""Partner fulfilment against real Postgres and the stand-in partner server: the send with its retries and dead letter, the signed status events, the state machine, the audit chain, and the
proof that no personal or health detail is kept or logged.

Each refusal is paired with the case that must still work (a refused skip ahead is applied when its turn comes; a refused signature is followed by the same event correctly signed).
The partner is `tests/fulfillment_stand_in.py`, a real localhost HTTP server; the client waits for nothing (its sleep is injected).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
import time
from pathlib import Path

import asyncpg
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fulfillment_stand_in import StandIn  # noqa: E402
from portal_fixtures import as_owner_count, needs_db, psycopg, reset_world, with_portal  # noqa: E402
from billing_fixtures import DSN  # noqa: E402

pytestmark = needs_db

from app.fulfillment import audit, service, states, webhook  # noqa: E402
from app.fulfillment.client import PartnerClient  # noqa: E402
from app.fulfillment.errors import OrderNotFound, OrderStateError  # noqa: E402

API_KEY = "partner-api-key-invented-for-tests"
SECRET = "partner-signing-secret-invented-for-tests"
SECRETS = (SECRET,)
SENSITIVE = ["Jane Roe", "1980-01-31", "12 Elm Street", "metformin", "diabetes", "555-0100"]


@pytest.fixture()
def world():
    class W:
        pass
    w = W()
    w.a, w.b = reset_world("tenant-a", "tenant-b")
    return w


def with_fulfilment(fn, **client_kw):
    """Run `fn(rig, partner, client, sleeps)` with the portal rig, a started stand-in partner and a client on it that waits for nothing."""
    async def body(rig):
        partner = StandIn(API_KEY, SECRET).start()
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
        http = httpx.AsyncClient(timeout=5)
        try:
            client = PartnerClient(http, partner.base_url, API_KEY, sleep=sleep, rand=lambda: 0.0, **client_kw)
            return await fn(rig, partner, client, sleeps)
        finally:
            await http.aclose()
            partner.stop()
    return with_portal(body)


async def new_order(rig, world, user="clinic1", clinic="c1"):
    return await service.create_order(rig.acting(world.a, user), world.a.tenant, clinic_id=world.a.clinics[clinic], actor=world.a.user(user))


async def post(rig, partner, order, status, *, event_id, now=None, header_override=None, payload_override=None, **kw):
    """Deliver one signed partner event through the receiver's HTTP answer. Returns (status code, body)."""
    body, header = partner.event(order["order_ref"], status, event_id=event_id, **kw)
    return await webhook.handle_http(rig.db, rig.frouter, payload_override or body, header_override or header, SECRETS, now=now)


async def order_now(rig, world, order, user="admin"):
    return await service.get_order(rig.acting(world.a, user), world.a.tenant, order["id"])


async def sent_order(rig, world, partner, client, **kw):
    order = await new_order(rig, world)
    res = await service.submit_order(rig.acting(world.a, "clinic1"), client, world.a.tenant, order["id"], actor="clinic1")
    assert res.status == "submitted"
    return order


async def table_text(table: str) -> list[str]:
    conn = await asyncpg.connect(DSN)
    try:
        return [r[0] for r in await conn.fetch("select t::text from %s t" % table)]
    finally:
        await conn.close()


# ---- creating an order ---------------------------------------------------------------------------------------------------------------------

def test_an_order_is_an_opaque_reference_and_a_clinic_and_nothing_else(world):
    async def go(rig, partner, client, sleeps):
        a = await new_order(rig, world)
        b = await new_order(rig, world)
        got = await order_now(rig, world, a, user="clinic1")
        return a, b, got, await table_text("fulfillment_order")
    a, b, got, rows = with_fulfilment(go)
    import re
    assert re.fullmatch(r"ord_[0-9a-f]{24}", a["order_ref"]) and a["order_ref"] != b["order_ref"]
    assert (a["status"], a["clinic_id"], a["carrier"], a["tracking_number"]) == ("created", world.a.clinics["c1"], None, None)
    assert [r["kind"] for r in got["audit"]] == ["created"] and len(rows) == 2


def test_a_clinic_user_creates_for_its_own_clinic_only_and_the_refusal_is_the_databases(world):
    async def go(rig, partner, client, sleeps):
        mine = await new_order(rig, world, user="clinic1", clinic="c1")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await new_order(rig, world, user="clinic1", clinic="c2")               # another clinic of the same tenant
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await new_order(rig, world, user="rep1", clinic="c1")                    # a rep may read orders, never make one
        theirs = await new_order(rig, world, user="admin", clinic="c3")              # the control: an admin may
        return mine, theirs, await as_owner_count("select count(*) from fulfillment_order")
    mine, theirs, count = with_fulfilment(go)
    assert mine["clinic_id"] == world.a.clinics["c1"] and theirs["clinic_id"] == world.a.clinics["c3"] and count == 2


def test_an_order_whose_reference_is_not_opaque_or_that_carries_a_carrier_before_it_ships_is_refused_by_the_table(world):
    with_fulfilment(lambda *a: asyncio.sleep(0))
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        base = "insert into fulfillment_order (tenant_id, clinic_id, order_ref, created_by, status, carrier, tracking_number) values (%s, %s, %s, 'x', %s, %s, %s)"
        t, c = world.a.tenant, world.a.clinics["c1"]
        for ref in ("Jane Roe", "ord_short", "ord_" + "G" * 24, "ord_" + "a" * 25):
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(base, (t, c, ref, "created", None, None))
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(base, (t, c, "ord_" + "a" * 24, "created", "SwiftPost", "TRK123456"))         # a carrier before it shipped
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(base, (t, c, "ord_" + "b" * 24, "shipped", None, None))                       # shipped needs both
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(base, (t, c, "ord_" + "c" * 24, "shipped", "SwiftPost", "bad tracking!"))
        cur.execute(base, (t, c, "ord_" + "d" * 24, "shipped", "SwiftPost", "TRK-123456"))            # the control: a well formed shipped row
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(base, (t, c, "ord_" + "d" * 24, "created", None, None))


# ---- sending ---------------------------------------------------------------------------------------------------------------------------------

def test_a_send_makes_one_order_at_the_partner_with_the_orders_key_and_a_second_send_is_a_replay(world):
    async def go(rig, partner, client, sleeps):
        order = await new_order(rig, world)
        db = rig.acting(world.a, "clinic1")
        first = await service.submit_order(db, client, world.a.tenant, order["id"], actor="clinic1")
        again = await service.submit_order(db, client, world.a.tenant, order["id"], actor="clinic1")
        got = await order_now(rig, world, order)
        return first, again, partner, got
    first, again, partner, got = with_fulfilment(go)
    assert (first.status, first.replayed, first.order["status"]) == ("submitted", False, "submitted") and again.replayed and len(partner.requests) == 1 and len(partner.orders) == 1
    assert [r["kind"] for r in got["audit"]] == ["created", "submit_attempt", "submitted"]
    assert json.loads(partner.requests[0]["body"]) == {"order_ref": got["order_ref"], "clinic_ref": "tenant-a-c1"}      # the two fields of the contract, nothing about a person


def test_two_people_sending_at_once_make_one_order_at_the_partner_and_one_submitted_row(world):
    async def go(rig, partner, client, sleeps):
        order = await new_order(rig, world)
        results = await asyncio.gather(*[service.submit_order(rig.acting(world.a, "clinic1"), client, world.a.tenant, order["id"], actor="clinic1") for _ in range(5)])
        return results, partner, await order_now(rig, world, order)
    results, partner, got = with_fulfilment(go)
    assert {r.order["status"] for r in results} == {"submitted"} and len(partner.orders) == 1
    assert [r["kind"] for r in got["audit"]].count("submitted") == 1 and len({r["headers"]["Idempotency-Key"] for r in partner.requests}) == 1


def test_a_transient_failure_is_retried_inside_the_send_and_the_order_still_goes_once(world):
    async def go(rig, partner, client, sleeps):
        partner.fail_posts = ["503", "429"]
        order = await new_order(rig, world)
        res = await service.submit_order(rig.acting(world.a, "clinic1"), client, world.a.tenant, order["id"], actor="clinic1")
        return res, partner, sleeps
    res, partner, sleeps = with_fulfilment(go)
    assert (res.status, res.attempts) == ("submitted", 3) and len(partner.orders) == 1 and len(partner.requests) == 3 and sleeps == [0.5, 2.0]


def test_a_permanent_refusal_is_a_dead_letter_with_a_code_and_the_order_stays_created_until_it_is_requeued(world):
    async def go(rig, partner, client, sleeps):
        partner.fail_posts = ["400"]
        order = await new_order(rig, world)
        db = rig.acting(world.a, "clinic1")
        res = await service.submit_order(db, client, world.a.tenant, order["id"], actor="clinic1")
        dead = await service.list_dead_letters(rig.acting(world.a, "admin"), world.a.tenant)
        with pytest.raises(OrderStateError):
            await service.submit_order(db, client, world.a.tenant, order["id"], actor="clinic1")           # refused until a person requeues it
        sent_before = len(partner.requests)
        await service.requeue(db, world.a.tenant, order["id"], actor="clinic1")
        with pytest.raises(OrderStateError):
            await service.requeue(db, world.a.tenant, order["id"], actor="clinic1")                         # nothing left to requeue
        ok = await service.submit_order(db, client, world.a.tenant, order["id"], actor="clinic1")
        return res, dead, sent_before, ok, partner, await order_now(rig, world, order), await service.list_dead_letters(rig.acting(world.a, "admin"), world.a.tenant, open_only=False)
    res, dead, sent_before, ok, partner, got, history = with_fulfilment(go)
    assert (res.status, res.reason, res.attempts, res.order["status"]) == ("dead_letter", "permanent_rejection", 1, "created")
    assert [(d["reason_code"], d["http_status"], d["attempts"]) for d in dead] == [("permanent_rejection", 400, 1)] and sent_before == 1
    assert ok.status == "submitted" and len(partner.orders) == 1
    assert [r["kind"] for r in got["audit"]] == ["created", "submit_attempt", "dead_lettered", "requeued", "submit_attempt", "submitted"]
    assert [d["requeued"] for d in history] == [True]


def test_retries_that_run_out_and_a_partner_that_never_answers_are_dead_letters_with_their_own_reasons(world):
    async def go(rig, partner, client, sleeps):
        db = rig.acting(world.a, "clinic1")
        partner.fail_posts = ["503"] * 4
        one = await new_order(rig, world)
        r1 = await service.submit_order(db, client, world.a.tenant, one["id"], actor="clinic1")
        partner.fail_posts = ["dropped"] * 4
        two = await new_order(rig, world)
        r2 = await service.submit_order(db, client, world.a.tenant, two["id"], actor="clinic1")
        return r1, r2, await service.list_dead_letters(rig.acting(world.a, "admin"), world.a.tenant), len(partner.orders)
    r1, r2, dead, orders = with_fulfilment(go)
    assert (r1.reason, r1.attempts, r2.reason, r2.attempts) == ("retries_exhausted", 4, "unreachable", 4) and orders == 0
    assert sorted((d["reason_code"], d["http_status"]) for d in dead) == [("retries_exhausted", 503), ("unreachable", None)]


def test_when_every_answer_is_lost_the_order_exists_at_the_partner_and_a_requeued_send_finds_it_instead_of_making_a_second(world):
    async def go(rig, partner, client, sleeps):
        partner.fail_posts = ["lost"] * 4
        order = await new_order(rig, world)
        db = rig.acting(world.a, "clinic1")
        res = await service.submit_order(db, client, world.a.tenant, order["id"], actor="clinic1")
        held = len(partner.orders)
        await service.requeue(db, world.a.tenant, order["id"], actor="clinic1")
        ok = await service.submit_order(db, client, world.a.tenant, order["id"], actor="clinic1")
        return res, held, ok, partner
    res, held, ok, partner = with_fulfilment(go)
    assert res.status == "dead_letter" and held == 1 and ok.status == "submitted" and len(partner.orders) == 1
    assert len({r["headers"]["Idempotency-Key"] for r in partner.requests}) == 1                       # the same key on every attempt, before and after the requeue


def test_only_an_order_that_is_created_can_be_cancelled_here_and_a_cancelled_order_is_never_sent(world):
    async def go(rig, partner, client, sleeps):
        db = rig.acting(world.a, "clinic1")
        order = await new_order(rig, world)
        cancelled = await service.cancel_order(db, world.a.tenant, order["id"], actor="clinic1")
        again = await service.cancel_order(db, world.a.tenant, order["id"], actor="clinic1")
        with pytest.raises(OrderStateError):
            await service.submit_order(db, client, world.a.tenant, order["id"], actor="clinic1")
        sent = await sent_order(rig, world, partner, client)
        with pytest.raises(OrderStateError):
            await service.cancel_order(db, world.a.tenant, sent["id"], actor="clinic1")
        return cancelled, again, len(partner.requests)
    cancelled, again, requests = with_fulfilment(go)
    assert cancelled["status"] == again["status"] == "cancelled" and requests == 1


# ---- the partner's signed events ----------------------------------------------------------------------------------------------------------------

def test_a_forged_tampered_stale_or_unsigned_event_is_a_400_and_writes_nothing_and_the_same_event_signed_properly_is_applied(world):
    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)
        good, header = partner.event(order["order_ref"], "accepted", event_id="evt-1")
        before = await as_owner_count("select count(*) from fulfillment_audit")
        forged, forged_header = partner.event(order["order_ref"], "accepted", event_id="evt-1", secret="some-other-secret")
        tampered = good.replace(b'"accepted"', b'"packed__"')
        stale_body, stale_header = partner.event(order["order_ref"], "accepted", event_id="evt-1", timestamp=int(time.time()) - 3600)
        future_body, future_header = partner.event(order["order_ref"], "accepted", event_id="evt-1", timestamp=int(time.time()) + 3600)
        answers = [await webhook.handle_http(rig.db, rig.frouter, p, h, SECRETS) for p, h in (
            (forged, forged_header), (tampered, header), (stale_body, stale_header), (future_body, future_header), (good, None), (good, ""), (good, "t=1,v1=00"), (good, "t=%d,v1=é" % time.time()))]
        mid = await as_owner_count("select count(*) from fulfillment_audit"), await as_owner_count("select count(*) from fulfillment_event")
        ok = await webhook.handle_http(rig.db, rig.frouter, good, header, SECRETS)
        return answers, before, mid, ok, (await order_now(rig, world, order))["status"]
    answers, before, mid, ok, status = with_fulfilment(go)
    assert all(a == (400, {"error": "invalid event"}) for a in answers) and mid == (before, 0)
    assert ok == (200, {"received": True, "result": "applied"}) and status == "accepted"


def test_a_repeat_of_an_event_changes_nothing_and_writes_nothing_even_with_other_content_under_the_same_id(world):
    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)
        first = await post(rig, partner, order, "accepted", event_id="evt-1")
        rows = await as_owner_count("select count(*) from fulfillment_audit"), await as_owner_count("select count(*) from fulfillment_event")
        again = [await post(rig, partner, order, "accepted", event_id="evt-1") for _ in range(3)]
        different = await post(rig, partner, order, "packed", event_id="evt-1")             # an id seen already is a repeat, whatever it now says
        return first, again, different, rows, (await as_owner_count("select count(*) from fulfillment_audit"), await as_owner_count("select count(*) from fulfillment_event")), (await order_now(rig, world, order))["status"]
    first, again, different, rows, after, status = with_fulfilment(go)
    assert first[1]["result"] == "applied" and [a[1]["result"] for a in again] == ["duplicate"] * 3 and different[1]["result"] == "duplicate"
    assert after == rows and status == "accepted"


def test_the_whole_road_to_delivery_writes_one_audit_row_for_each_move_and_the_chain_verifies(world):
    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)
        for i, (status, kw) in enumerate((("accepted", {}), ("packed", {}), ("shipped", {"carrier": "SwiftPost", "tracking_number": "TRK-123456"}), ("delivered", {}))):
            code, body = await post(rig, partner, order, status, event_id="evt-%d" % i, **kw)
            assert (code, body["result"]) == (200, "applied")
        got = await order_now(rig, world, order)
        return got, await service.verify(rig.acting(world.a, "admin"), world.a.tenant, order["id"], actor="admin")
    got, problems = with_fulfilment(go)
    assert got["status"] == "delivered" and (got["carrier"], got["tracking_number"]) == ("SwiftPost", "TRK-123456") and problems == []
    moves = [(r["from_status"], r["to_status"]) for r in got["audit"] if r["kind"] in ("submitted", "transition")]
    assert moves == [("created", "submitted"), ("submitted", "accepted"), ("accepted", "packed"), ("packed", "shipped"), ("shipped", "delivered")]
    assert all(states.can_move(a, b) for a, b in moves) and [r["seq"] for r in got["audit"]] == list(range(1, len(got["audit"]) + 1))


def test_shipped_needs_a_carrier_and_a_tracking_number_and_a_malformed_one_changes_nothing(world):
    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)
        await post(rig, partner, order, "accepted", event_id="e1")
        await post(rig, partner, order, "packed", event_id="e2")
        bad = [await post(rig, partner, order, "shipped", event_id="b%d" % i, **kw) for i, kw in enumerate(
            ({}, {"carrier": "SwiftPost"}, {"tracking_number": "TRK-123456"}, {"carrier": "SwiftPost", "tracking_number": "no spaces allowed"}, {"carrier": "x", "tracking_number": "TRK-123456"}))]
        stray = await post(rig, partner, order, "delivered", event_id="d1", carrier="SwiftPost")          # a carrier belongs to the shipped event only
        mid = (await order_now(rig, world, order))["status"]
        ok = await post(rig, partner, order, "shipped", event_id="s1", carrier="SwiftPost", tracking_number="TRK-123456")
        return bad, stray, mid, ok
    bad, stray, mid, ok = with_fulfilment(go)
    assert all(b == (400, {"error": "invalid event"}) for b in bad) and stray == (400, {"error": "invalid event"}) and mid == "packed" and ok[1]["result"] == "applied"


def test_a_move_that_is_not_legal_yet_is_refused_and_applied_when_the_partner_sends_it_again_after_the_move_before_it(world):
    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)
        early = await post(rig, partner, order, "shipped", event_id="s1", carrier="SwiftPost", tracking_number="TRK-123456")        # before accepted and packed
        mid = await order_now(rig, world, order)
        await post(rig, partner, order, "accepted", event_id="a1")
        await post(rig, partner, order, "packed", event_id="p1")
        retried = await post(rig, partner, order, "shipped", event_id="s1", carrier="SwiftPost", tracking_number="TRK-123456")      # the same event again
        return early, mid, retried, await order_now(rig, world, order)
    early, mid, retried, got = with_fulfilment(go)
    assert early == (409, {"error": "not legal now", "result": "refused"}) and mid["status"] == "submitted" and mid["tracking_number"] is None
    assert [r["kind"] for r in mid["audit"]][-1] == "refused" and retried[1]["result"] == "applied" and got["status"] == "shipped"


def test_an_older_or_repeated_status_never_moves_an_order_backwards_it_is_recorded_as_ignored(world):
    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)
        for i, (status, kw) in enumerate((("accepted", {}), ("packed", {}), ("shipped", {"carrier": "SwiftPost", "tracking_number": "TRK-123456"}))):
            await post(rig, partner, order, status, event_id="e%d" % i, **kw)
        late = [await post(rig, partner, order, s, event_id="late-" + s) for s in ("accepted", "packed", "submitted")]
        same = await post(rig, partner, order, "packed", event_id="late-again")
        return late, same, await order_now(rig, world, order)
    late, same, got = with_fulfilment(go)
    assert [l[1]["result"] for l in late] == ["ignored"] * 3 and same[1]["result"] == "ignored" and got["status"] == "shipped"
    assert [r["kind"] for r in got["audit"]].count("ignored") == 4 and got["tracking_number"] == "TRK-123456"


def test_a_cancel_after_delivery_and_a_ship_of_a_cancelled_order_are_refused_and_the_end_stays_the_end(world):
    async def go(rig, partner, client, sleeps):
        delivered = await sent_order(rig, world, partner, client)
        for i, (status, kw) in enumerate((("accepted", {}), ("packed", {}), ("shipped", {"carrier": "SwiftPost", "tracking_number": "TRK-123456"}), ("delivered", {}))):
            await post(rig, partner, delivered, status, event_id="d%d" % i, **kw)
        cancel_late = await post(rig, partner, delivered, "cancelled", event_id="dc")
        cancelled = await sent_order(rig, world, partner, client)
        await post(rig, partner, cancelled, "cancelled", event_id="c0")
        ship_cancelled = await post(rig, partner, cancelled, "shipped", event_id="c1", carrier="SwiftPost", tracking_number="TRK-654321")
        accept_cancelled = await post(rig, partner, cancelled, "accepted", event_id="c2")
        rejected = await sent_order(rig, world, partner, client)
        await post(rig, partner, rejected, "rejected", event_id="r0")
        pack_rejected = await post(rig, partner, rejected, "packed", event_id="r1")
        return cancel_late, ship_cancelled, accept_cancelled, pack_rejected, [(await order_now(rig, world, o))["status"] for o in (delivered, cancelled, rejected)]
    cancel_late, ship_cancelled, accept_cancelled, pack_rejected, statuses = with_fulfilment(go)
    assert cancel_late[0] == ship_cancelled[0] == pack_rejected[0] == 409 and accept_cancelled[1]["result"] == "ignored" and statuses == ["delivered", "cancelled", "rejected"]


def test_an_event_for_an_order_that_is_not_ours_is_a_404_and_a_body_that_is_not_an_event_is_a_400(world):
    async def go(rig, partner, client, sleeps):
        unknown = {"order_ref": "ord_" + "0" * 24}
        code404 = await post(rig, partner, unknown, "accepted", event_id="x1")
        bad = []
        for raw in (b"not json", b"[]", json.dumps({"event_id": "x", "order_ref": "Jane Roe", "status": "accepted"}).encode(), json.dumps({"event_id": "x", "order_ref": "ord_" + "0" * 24, "status": "teleported"}).encode()):
            import hashlib, hmac
            ts = int(time.time())
            header = "t=%d,v1=%s" % (ts, hmac.new(SECRET.encode(), str(ts).encode() + b"." + raw, hashlib.sha256).hexdigest())
            bad.append(await webhook.handle_http(rig.db, rig.frouter, raw, header, SECRETS))
        return code404, bad
    code404, bad = with_fulfilment(go)
    assert code404 == (404, {"error": "unknown order"}) and all(b == (400, {"error": "invalid event"}) for b in bad)


def test_during_a_secret_rotation_an_event_signed_with_the_old_secret_is_accepted_while_both_are_active(world):
    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)
        body, header = partner.event(order["order_ref"], "accepted", event_id="r1", secret="the-previous-secret")
        refused = await webhook.handle_http(rig.db, rig.frouter, body, header, SECRETS)
        ok = await webhook.handle_http(rig.db, rig.frouter, body, header, (SECRET, "the-previous-secret"))
        return refused, ok
    refused, ok = with_fulfilment(go)
    assert refused[0] == 400 and ok[1]["result"] == "applied"


# ---- the database says the same, whatever the code does ------------------------------------------------------------------------------------------

def test_a_hand_written_move_that_is_not_in_the_table_is_refused_by_the_database_even_for_the_owner(world):
    async def go(rig, partner, client, sleeps):
        order = await new_order(rig, world)
        admin = rig.acting(world.a, "admin")
        async with admin.tenant(world.a.tenant) as conn:
            with pytest.raises(Exception, match="cannot move from created to delivered"):
                await conn.execute("update fulfillment_order set status = 'delivered', carrier = 'SwiftPost', tracking_number = 'TRK-123456' where id = $1::uuid", order["id"])
        async with admin.tenant(world.a.tenant) as conn:
            await conn.execute("update fulfillment_order set status = 'submitted' where id = $1::uuid", order["id"])          # the control: a legal move goes through
        return order
    order = with_fulfilment(go)
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RestrictViolation):
            cur.execute("update fulfillment_order set status = 'created' where id = %s", (order["id"],))                      # backwards, as the owner
        with pytest.raises(psycopg.errors.RestrictViolation):
            cur.execute("update fulfillment_order set order_ref = %s where id = %s", ("ord_" + "e" * 24, order["id"]))        # the reference never changes
        with pytest.raises(psycopg.errors.RestrictViolation):
            cur.execute("delete from fulfillment_order where id = %s", (order["id"],))


def test_the_audit_and_event_rows_are_append_only_for_the_request_role_and_for_the_owner(world):
    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)
        await post(rig, partner, order, "accepted", event_id="e1")
        for sql in ("update fulfillment_audit set actor = 'x'", "delete from fulfillment_audit", "update fulfillment_event set result = 'ignored'", "delete from fulfillment_event"):
            async with rig.acting(world.a, "admin").tenant(world.a.tenant) as conn:          # a fresh transaction each: a refusal aborts the one it was in
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute(sql)
        return order
    with_fulfilment(go)
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        for sql in ("update fulfillment_audit set actor = 'x'", "delete from fulfillment_audit", "update fulfillment_event set result = 'ignored'", "delete from fulfillment_event"):
            with pytest.raises(psycopg.errors.RestrictViolation):
                cur.execute(sql)


def test_the_router_role_reads_an_orders_id_tenant_and_reference_and_nothing_more(world):
    async def go(rig, partner, client, sleeps):
        order = await new_order(rig, world)
        async with rig.frouter.unscoped() as conn:
            row = await conn.fetchrow("select id, tenant_id, order_ref from fulfillment_order where order_ref = $1", order["order_ref"])
        refusals = []
        for sql in ("select status from fulfillment_order", "select * from fulfillment_order", "select * from fulfillment_audit", "select * from clinic", "select * from billing_payment", "select * from portal_user"):
            async with rig.frouter.unscoped() as conn:
                try:
                    await conn.fetch(sql)
                    refusals.append(None)
                except asyncpg.InsufficientPrivilegeError:
                    refusals.append("denied")
        async with rig.frouter.unscoped() as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("update fulfillment_order set status = 'cancelled'")
        return row, order, refusals
    row, order, refusals = with_fulfilment(go)
    assert str(row["id"]) == order["id"] and refusals == ["denied"] * 6


# ---- no personal or health detail is kept, audited or logged -------------------------------------------------------------------------------------

def test_nothing_readable_about_a_person_reaches_a_log_an_order_row_an_audit_row_or_any_fulfilment_table(world, caplog):
    caplog.set_level(logging.DEBUG)

    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)
        extra = {"recipient_name": SENSITIVE[0], "date_of_birth": SENSITIVE[1], "ship_to": SENSITIVE[2], "drug": SENSITIVE[3], "diagnosis": SENSITIVE[4], "phone": SENSITIVE[5],
                 "notes": "Jane Roe takes metformin for diabetes", "tracking_number_note": SENSITIVE[2]}
        results = []
        for i, (status, kw) in enumerate((("accepted", {}), ("packed", {}), ("shipped", {"carrier": "SwiftPost", "tracking_number": "TRK-123456"}), ("delivered", {}))):
            results.append(await post(rig, partner, order, status, event_id="evt-%d" % i, extra=extra, **kw))
        results.append(await post(rig, partner, order, "accepted", event_id="evt-late", extra=extra))             # an ignored one
        results.append(await post(rig, partner, order, "cancelled", event_id="evt-refused", extra=extra))          # a refused one
        results.append(await post(rig, partner, order, "teleported", event_id="evt-bad", extra=extra))             # a rejected one
        results.append(await post(rig, partner, order, "delivered", event_id="evt-0", extra=extra))                # a repeat
        problems = await service.verify(rig.acting(world.a, "admin"), world.a.tenant, order["id"], actor="admin")
        got = await order_now(rig, world, order)
        dump = []
        for table in ("fulfillment_order", "fulfillment_submission", "fulfillment_dead_letter", "fulfillment_event", "fulfillment_audit"):
            dump += await table_text(table)
        return results, problems, got, dump, partner
    results, problems, got, dump, partner = with_fulfilment(go)
    assert [r[0] for r in results] == [200, 200, 200, 200, 200, 409, 400, 200] and problems == []
    haystacks = {"log": caplog.text, "tables": "\n".join(dump), "order view": json.dumps(got), "answers": json.dumps(results)}
    for where, text in haystacks.items():
        for secret in SENSITIVE + ["Jane", "Elm", "metformin", "Roe"]:
            assert secret not in text, "%r reached the %s" % (secret, where)
    assert any(r["detail"].get("tracking_number") == "TRK-123456" for r in got["audit"])                    # the control: what is kept IS kept
    # and what we sent the partner was the two fields of the contract, with nothing about a person in it
    assert all(set(json.loads(r["body"])) == {"order_ref", "clinic_ref"} for r in partner.requests)


def test_a_detail_with_a_name_in_it_is_stripped_before_it_is_hashed_or_stored():
    from app.fulfillment.audit import clean_detail
    got = clean_detail({"attempts": 2, "http_status": 503, "reason": "retries_exhausted", "carrier": "SwiftPost", "tracking_number": "TRK-123456", "patient": "Jane Roe",
                        "notes": "diabetes", "reason_text": "Jane Roe", "result": "Jane Roe, 12 Elm Street", "ok": True, "problems": 0, "current": None, "x" * 5: "y"})
    assert got == {"attempts": 2, "http_status": 503, "reason": "retries_exhausted", "carrier": "SwiftPost", "tracking_number": "TRK-123456", "ok": True, "problems": 0, "current": None}
    assert clean_detail({"carrier": {"nested": "Jane"}, "reason": ["Jane"], "attempts": 10 ** 15, "result": "a" * 200}) == {}
    assert clean_detail(None) == {}


# ---- the audit chain proves itself --------------------------------------------------------------------------------------------------------------------

def tamper(sql: str, *args):
    """Run a statement as the owner with the append only trigger switched off, then switch it back on: the only way an edit of the log can happen."""
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("alter table fulfillment_audit disable trigger fulfillment_audit_append_only")
        try:
            cur.execute(sql, args)
        finally:
            cur.execute("alter table fulfillment_audit enable trigger fulfillment_audit_append_only")


def test_an_edited_deleted_or_reordered_audit_row_is_named_by_verify_and_an_untouched_log_verifies(world):
    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)
        await post(rig, partner, order, "accepted", event_id="e1")
        await post(rig, partner, order, "packed", event_id="e2")
        admin = rig.acting(world.a, "admin")
        clean = await service.verify(admin, world.a.tenant, order["id"], actor="admin")
        tamper("update fulfillment_audit set actor = 'someone else' where order_id = %s and seq = 3", order["id"])
        edited = await service.verify(admin, world.a.tenant, order["id"], actor="admin")
        tamper("update fulfillment_audit set actor = 'system' where order_id = %s and seq = 3", order["id"])           # put back what it was
        tamper("delete from fulfillment_audit where order_id = %s and seq = 2", order["id"])
        deleted = await service.verify(admin, world.a.tenant, order["id"], actor="admin")
        return clean, edited, deleted
    clean, edited, deleted = with_fulfilment(go)
    assert clean == []
    assert any(p.code == "row_edited" and p.seq == 3 for p in edited)
    assert any(p.code == "row_missing" for p in deleted) and any(p.code == "link_broken" for p in deleted)


def test_a_status_the_log_does_not_explain_is_reported(world):
    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)
        admin = rig.acting(world.a, "admin")
        with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("alter table fulfillment_order disable trigger fulfillment_order_guard")
            try:
                cur.execute("update fulfillment_order set status = 'cancelled' where id = %s", (order["id"],))
            finally:
                cur.execute("alter table fulfillment_order enable trigger fulfillment_order_guard")
        return await service.verify(admin, world.a.tenant, order["id"], actor="admin")
    problems = with_fulfilment(go)
    assert [p.code for p in problems] == ["status_unexplained"]


# ---- who sees which order -----------------------------------------------------------------------------------------------------------------------------

def test_an_order_is_seen_by_its_clinic_its_prescriber_its_rep_and_an_admin_and_by_nobody_else_and_another_tenant_sees_none(world):
    async def go(rig, partner, client, sleeps):
        o1 = await new_order(rig, world, user="clinic1", clinic="c1")
        o2 = await new_order(rig, world, user="clinic2", clinic="c2")
        o3 = await new_order(rig, world, user="admin", clinic="c3")
        seen = {}
        for user in ("clinic1", "clinic2", "prescriber1", "rep1", "rep2", "admin"):
            seen[user] = sorted(o["order_ref"] for o in await service.list_orders(rig.acting(world.a, user), world.a.tenant))
        with pytest.raises(OrderNotFound):
            await service.get_order(rig.acting(world.a, "clinic2"), world.a.tenant, o1["id"])          # someone else's order answers like one that does not exist
        with pytest.raises(OrderNotFound):
            await service.get_order(rig.acting(world.a, "admin"), world.a.tenant, "not-a-uuid")
        other = await service.list_orders(rig.acting(world.b, "admin"), world.b.tenant)
        wrong_tenant = await service.list_orders(rig.acting(world.a, "admin"), world.b.tenant)         # tenant b's id with tenant a's user: the user does not exist there
        return o1, o2, o3, seen, other, wrong_tenant
    o1, o2, o3, seen, other, wrong_tenant = with_fulfilment(go)
    refs = lambda *os: sorted(o["order_ref"] for o in os)       # noqa: E731
    assert seen["clinic1"] == refs(o1) and seen["clinic2"] == refs(o2) and seen["prescriber1"] == refs(o1)
    assert seen["rep1"] == refs(o1, o2) and seen["rep2"] == refs(o3) and seen["admin"] == refs(o1, o2, o3) and other == [] and wrong_tenant == []


# ---- the machine over many seeded worlds ----------------------------------------------------------------------------------------------------------------

LEGIT = [("accepted", {}), ("packed", {}), ("shipped", {"carrier": "SwiftPost", "tracking_number": "TRK-123456"}), ("delivered", {})]


@pytest.mark.parametrize("seed", range(15))
def test_over_seeded_worlds_events_arriving_late_twice_and_out_of_order_still_end_in_the_right_state_by_a_legal_path(world, seed):
    """The partner sends the four events of a delivery in a random order, some of them twice, and resends a refused one until it is applied (as a partner retrying a 409 would). Some worlds
    send a cancel racing the accepted, which ends cancelled whichever arrives first. Whatever the order: no move outside the table, no step back, one audit row for each move, and the end the model says."""
    rng = random.Random(seed)

    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)
        cancel = rng.random() < 0.3
        events = [("e%d-%s" % (seed, s), s, kw) for s, kw in (LEGIT[:1] if cancel else LEGIT)]       # a cancel races an accepted, never a packed
        if cancel:
            events.append(("e%d-cancel" % seed, "cancelled", {}))
        queue = events[:]
        rng.shuffle(queue)
        queue += [rng.choice(events) for _ in range(rng.randrange(0, 4))]            # duplicates
        results = []
        for _ in range(8):                                                           # the partner retries what was refused, bounded
            refused = []
            for eid, status, kw in queue:
                code, body = await post(rig, partner, order, status, event_id=eid, **kw)
                results.append((eid, code, body.get("result")))
                if code == 409:
                    refused.append((eid, status, kw))
            queue = refused
            if not queue:
                break
        got = await order_now(rig, world, order)
        return events, cancel, results, got, await service.verify(rig.acting(world.a, "admin"), world.a.tenant, order["id"], actor="admin"), await as_owner_count("select count(*) from fulfillment_event")
    events, cancel, results, got, problems, event_rows = with_fulfilment(go)
    moves = [(r["from_status"], r["to_status"]) for r in got["audit"] if r["kind"] in ("submitted", "transition")]
    assert all(states.can_move(a, b) for a, b in moves), moves
    assert all(b == moves[i + 1][0] for i, (a, b) in enumerate(moves[:-1])), "each move starts where the one before ended"
    if cancel:
        assert got["status"] == "cancelled" and moves[-1][1] == "cancelled"
    else:
        assert got["status"] == "delivered" and [m[1] for m in moves] == ["submitted", "accepted", "packed", "shipped", "delivered"]
    assert problems == [] and event_rows <= len({e[0] for e in events}) and not any(code == 400 for _, code, _ in results)
    assert [r["kind"] for r in got["audit"]].count("transition") == len(moves) - 1


# ---- more of what the log and the receiver must refuse ----------------------------------------------------------------------------------------------

def test_a_name_under_a_whitelisted_key_is_dropped_too_because_each_key_has_its_own_shape():
    from app.fulfillment.audit import clean_detail
    got = clean_detail({"reason": "Jane Roe", "result": "diabetes mellitus", "current": "Jane", "target": "shipped", "carrier": "Jane Roe", "tracking_number": "12 Elm Street",
                        "partner_order_id": "Jane Roe", "attempts": "3", "ok": "yes", "http_status": True, "problems": 2})
    assert got == {"target": "shipped", "problems": 2}
    ok = clean_detail({"reason": "not_legal_now", "result": "sent", "current": "packed", "target": "cancelled", "carrier": "SwiftPost", "tracking_number": "TRK-123456",
                       "partner_order_id": "po_000001", "attempts": 3, "ok": True, "http_status": 503, "problems": 0, "occurred_at": 1760000000})
    assert len(ok) == 12 and ok["carrier"] == "SwiftPost" and ok["reason"] == "not_legal_now"


def test_an_edited_detail_is_named_by_verify_because_the_detail_is_part_of_the_hash(world):
    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)
        admin = rig.acting(world.a, "admin")
        tamper("update fulfillment_audit set detail = '{\"attempts\": 99}'::jsonb where order_id = %s and seq = 2", order["id"])
        return await service.verify(admin, world.a.tenant, order["id"], actor="admin")
    problems = with_fulfilment(go)
    assert any(p.code == "row_edited" and p.seq == 2 for p in problems)


def test_an_event_without_a_usable_id_or_with_a_bad_one_is_a_400_and_a_good_one_still_applies(world):
    import hashlib
    import hmac

    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)
        sent = []
        for body in ({"order_ref": order["order_ref"], "status": "accepted"}, {"event_id": "has spaces", "order_ref": order["order_ref"], "status": "accepted"},
                     {"event_id": 7, "order_ref": order["order_ref"], "status": "accepted"}, {"event_id": "e" * 121, "order_ref": order["order_ref"], "status": "accepted"},
                     {"event_id": "ok-1", "order_ref": order["order_ref"], "status": 5}):
            raw = json.dumps(body).encode()
            ts = int(time.time())
            header = "t=%d,v1=%s" % (ts, hmac.new(SECRET.encode(), str(ts).encode() + b"." + raw, hashlib.sha256).hexdigest())
            sent.append(await webhook.handle_http(rig.db, rig.frouter, raw, header, SECRETS))
        ok = await post(rig, partner, order, "accepted", event_id="ok-1")
        return sent, ok, await as_owner_count("select count(*) from fulfillment_event")
    sent, ok, events = with_fulfilment(go)
    assert all(s == (400, {"error": "invalid event"}) for s in sent) and ok[1]["result"] == "applied" and events == 1


def test_a_role_that_may_only_read_orders_cannot_write_the_children_of_an_order_and_another_clinic_cannot_read_them(world):
    async def go(rig, partner, client, sleeps):
        order = await sent_order(rig, world, partner, client)                 # clinic c1; rep1 covers c1 and may read; rep2 covers c3; clinic2 is another clinic
        await post(rig, partner, order, "accepted", event_id="e1")
        out = {}
        tables = ("fulfillment_submission", "fulfillment_dead_letter", "fulfillment_event", "fulfillment_audit")
        for label in ("clinic1", "rep1", "rep2", "clinic2", "admin"):
            async with rig.acting(world.a, label).tenant(world.a.tenant) as conn:
                out[label] = [await conn.fetchval("select count(*) from %s" % t) for t in tables]
        writes = {}
        attempts = {
            "audit": "insert into fulfillment_audit (tenant_id, order_id, clinic_id, seq, kind, actor, at, prev_hash, entry_hash) values ($1::uuid, $2::uuid, $3::uuid, 99, 'ignored', 'x', now(), $4, $4)",
            "event": "insert into fulfillment_event (tenant_id, order_id, clinic_id, partner_event_id, event_status, result) values ($1::uuid, $2::uuid, $3::uuid, 'forged', 'packed', 'applied')",
            "dead letter": "insert into fulfillment_dead_letter (tenant_id, order_id, clinic_id, reason_code, attempts) values ($1::uuid, $2::uuid, $3::uuid, 'permanent_rejection', 1)",
        }
        for label in ("rep1", "clinic2"):
            for name, sql in attempts.items():
                try:
                    async with rig.acting(world.a, label).tenant(world.a.tenant) as conn:
                        await conn.execute(sql, world.a.tenant, order["id"], world.a.clinics["c1"], "0" * 64) if name == "audit" else await conn.execute(sql, world.a.tenant, order["id"], world.a.clinics["c1"])
                    writes[(label, name)] = "allowed"
                except asyncpg.InsufficientPrivilegeError:
                    writes[(label, name)] = "denied"
        async with rig.acting(world.a, "rep1").tenant(world.a.tenant) as conn:
            updated = await conn.execute("update fulfillment_submission set attempts = 50")
        async with rig.acting(world.a, "clinic1").tenant(world.a.tenant) as conn:            # the control: the clinic's own user may write its own order's children
            await conn.execute("insert into fulfillment_dead_letter (tenant_id, order_id, clinic_id, reason_code, attempts) values ($1::uuid, $2::uuid, $3::uuid, 'permanent_rejection', 1)",
                               world.a.tenant, order["id"], world.a.clinics["c1"])
        return out, writes, updated
    out, writes, updated = with_fulfilment(go)
    assert out["clinic1"] == out["rep1"] == out["admin"] == [1, 0, 1, out["admin"][3]] and out["admin"][3] >= 3
    assert out["rep2"] == out["clinic2"] == [0, 0, 0, 0]
    assert set(writes.values()) == {"denied"} and updated == "UPDATE 0"
