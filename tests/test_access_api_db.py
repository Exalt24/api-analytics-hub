"""The API layer of the access matrix, over HTTP against real Postgres, the fake Stripe and the stand-in partner.

PART 1, GENERATED from the table: for every user of the portal and every (resource, action) cell there is a probe route guarded by `requires_access`, and the user's own key is sent to
it. 200 is expected exactly when the table gives the user's role something other than `none` there, else 403: 6 users x 20 cells = 120 requests, the expectation never retyped.
PART 2, the real routes: orders, invoices, payments and commissions driven as the users who may and must not, with the row level security underneath answering the same way (a clinic's
request for another clinic's order is a 404 because the database shows it nothing). PART 3: the credential is the only source of who is asking.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fulfillment_stand_in import StandIn  # noqa: E402
from portal_fixtures import ACH_PM, CUSTOMER, MANDATE, USD, as_owner_count, needs_db, psycopg, reset_world, with_portal  # noqa: E402
from billing_fixtures import DSN, SECRETS, counts  # noqa: E402

pytestmark = needs_db

from app.access import matrix  # noqa: E402
from app.access.principal import PortalAuthenticator, PortalPrincipal, requires_access  # noqa: E402
from app.api.portal import PortalRuntime, router  # noqa: E402
from app.billing import webhooks  # noqa: E402
from app.fulfillment.client import PartnerClient  # noqa: E402

PARTNER_KEY, PARTNER_SECRET = "partner-api-key-invented-for-tests", "partner-signing-secret-invented-for-tests"
USERS = ("clinic1", "clinic2", "prescriber1", "rep1", "rep2", "admin")
LINES = [{"description": "Consultation kit", "quantity": 3, "unit_amount_minor": 2500}, {"description": "Courier", "quantity": 1, "unit_amount_minor": 1500}]


@pytest.fixture()
def world():
    class W:
        pass
    w = W()
    w.a, w.b = reset_world("tenant-a", "tenant-b")
    return w


def make_app(rig, partner: StandIn | None = None, client: PartnerClient | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def ok():
        return {"ok": True}
    for resource in matrix.RESOURCES:
        for action in matrix.ACTIONS:
            from fastapi import Depends
            app.add_api_route("/probe/%s/%s" % (resource, action), ok, methods=["GET"], dependencies=[Depends(requires_access(resource, action))])
    app.state.portal = PortalRuntime(rig.db, PortalAuthenticator(rig.db), provider=rig.provider, partner=client, fulfillment_router=rig.frouter, partner_secrets=(PARTNER_SECRET,))
    return app


def with_api(fn):
    """Run `fn(rig, http, partner)`: `http` is an httpx client on the app, with no credential; `partner` is the started stand-in the partner client talks to."""
    async def body(rig):
        partner = StandIn(PARTNER_KEY, PARTNER_SECRET).start()
        waits = httpx.AsyncClient(timeout=5)

        async def no_wait(_):
            return None
        client = PartnerClient(waits, partner.base_url, PARTNER_KEY, sleep=no_wait, rand=lambda: 0.0)
        app = make_app(rig, partner, client)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            try:
                return await fn(rig, http, partner)
            finally:
                await waits.aclose()
                partner.stop()
    return with_portal(body)


def auth(world, label: str, **extra) -> dict:
    return {"Authorization": "Bearer " + world.a.keys[label], **extra}


def idem(world, label: str, key: str) -> dict:
    return auth(world, label, **{"Idempotency-Key": key})


def role_of(world, label: str) -> str:
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select role from portal_user where id = %s", (world.a.users[label],))
        return cur.fetchone()[0]


# ---- part 1: generated from the table ---------------------------------------------------------------------------------------------------------

def test_every_user_and_cell_of_the_matrix_is_allowed_or_forbidden_by_the_api_as_the_table_says(world):
    roles = {label: role_of(world, label) for label in USERS}

    async def go(rig, http, partner):
        got = {}
        for label in USERS:
            for resource, action in ((r, a) for r in matrix.RESOURCES for a in matrix.ACTIONS):
                got[(label, resource, action)] = (await http.get("/probe/%s/%s" % (resource, action), headers=auth(world, label))).status_code
        return got
    got = with_api(go)
    assert len(got) == 120
    wrong = [(k, code) for k, code in got.items() if code != (200 if matrix.allows(roles[k[0]], k[1], k[2]) else 403)]
    assert not wrong, wrong
    assert {c for c in got.values()} == {200, 403}, "the table must grant something and refuse something, or this test proves nothing"


def test_no_key_an_unknown_key_a_revoked_key_and_a_malformed_header_are_one_401_and_a_valid_key_afterwards_works(world):
    async def go(rig, http, partner):
        out = []
        for headers in ({}, {"Authorization": "Bearer pk_test_nothing"}, {"Authorization": "Bearer "}, {"Authorization": "Basic abc"}, {"Authorization": world.a.keys["admin"]}):
            r = await http.get("/probe/orders/read", headers=headers)
            out.append((r.status_code, r.json()["detail"] if r.status_code == 401 else None))
        with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("update portal_credential set revoked_at = now() where user_id = %s", (world.a.users["clinic1"],))
        revoked = await http.get("/probe/orders/read", headers=auth(world, "clinic1"))
        with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("update portal_credential set revoked_at = null where user_id = %s", (world.a.users["clinic1"],))
        return out, revoked, await http.get("/probe/orders/read", headers=auth(world, "clinic1"))
    out, revoked, after = with_api(go)
    assert [o[0] for o in out] == [401] * 5 and revoked.status_code == 401 and revoked.json() == {"detail": "invalid credentials"} and after.status_code == 200


def test_an_unknown_role_holds_nothing_in_the_guard_and_a_cell_that_does_not_exist_is_refused_when_the_route_is_defined():
    import asyncio
    guard = requires_access("orders", "read")
    for role in ("viewer", "", "ADMIN", None):
        principal = PortalPrincipal("t", "u", role or "", None)
        with pytest.raises(Exception) as err:
            asyncio.run(guard(principal))
        assert getattr(err.value, "status_code", None) == 403
    assert asyncio.run(guard(PortalPrincipal("t", "u", "admin", None))).role == "admin"
    with pytest.raises(ValueError):
        requires_access("orders", "delete")
    with pytest.raises(ValueError):
        requires_access("secrets", "read")


# ---- part 2: the real routes -------------------------------------------------------------------------------------------------------------------

def test_an_order_is_made_for_the_users_own_clinic_listed_by_who_may_see_it_and_a_stranger_gets_a_404_like_a_missing_id(world):
    async def go(rig, http, partner):
        mine = (await http.post("/api/portal/orders", json={}, headers=auth(world, "clinic1"))).json()
        theirs = (await http.post("/api/portal/orders", json={}, headers=auth(world, "clinic2"))).json()
        admin_made = await http.post("/api/portal/orders", json={"clinic_id": world.a.clinics["c3"]}, headers=auth(world, "admin"))
        admin_none = await http.post("/api/portal/orders", json={}, headers=auth(world, "admin"))
        other_clinic = await http.post("/api/portal/orders", json={"clinic_id": world.a.clinics["c2"]}, headers=auth(world, "clinic1"))
        rep_makes = await http.post("/api/portal/orders", json={}, headers=auth(world, "rep1"))
        lists = {u: sorted(o["clinic_id"] for o in (await http.get("/api/portal/orders", headers=auth(world, u))).json()["orders"]) for u in USERS}
        stranger = await http.get("/api/portal/orders/" + mine["id"], headers=auth(world, "clinic2"))
        missing = await http.get("/api/portal/orders/" + "0" * 8 + "-0000-0000-0000-" + "0" * 12, headers=auth(world, "clinic2"))
        junk = await http.get("/api/portal/orders/not-a-uuid", headers=auth(world, "clinic2"))
        own = await http.get("/api/portal/orders/" + mine["id"], headers=auth(world, "prescriber1"))
        return mine, theirs, admin_made, admin_none, other_clinic, rep_makes, lists, stranger, missing, junk, own
    mine, theirs, admin_made, admin_none, other_clinic, rep_makes, lists, stranger, missing, junk, own = with_api(go)
    c = world.a.clinics
    assert (admin_made.status_code, admin_none.status_code, other_clinic.status_code, rep_makes.status_code) == (201, 422, 403, 403)
    assert lists["clinic1"] == [c["c1"]] and lists["clinic2"] == [c["c2"]] and lists["prescriber1"] == [c["c1"]] and lists["rep1"] == sorted([c["c1"], c["c2"]])
    assert lists["rep2"] == [c["c3"]] and lists["admin"] == sorted([c["c1"], c["c2"], c["c3"]])
    assert stranger.status_code == missing.status_code == junk.status_code == 404 and stranger.json() == missing.json() == junk.json() == {"detail": "no such order"}
    assert own.status_code == 200 and own.json()["id"] == mine["id"]


def test_an_order_takes_no_free_text_a_name_in_the_body_is_a_422_and_nothing_is_created(world):
    async def go(rig, http, partner):
        before = await as_owner_count("select count(*) from fulfillment_order")
        bad = [await http.post("/api/portal/orders", json=body, headers=auth(world, "clinic1")) for body in (
            {"patient_name": "Jane Roe"}, {"clinic_id": None, "notes": "metformin for diabetes"}, {"address": "12 Elm Street", "dob": "1980-01-31"})]
        ok = await http.post("/api/portal/orders", json={}, headers=auth(world, "clinic1"))
        return before, bad, ok, await as_owner_count("select count(*) from fulfillment_order"), await as_owner_count("select count(*) from fulfillment_audit")
    before, bad, ok, after, audits = with_api(go)
    assert [b.status_code for b in bad] == [422, 422, 422] and ok.status_code == 201 and (before, after, audits) == (0, 1, 1)


def test_sending_an_order_is_for_a_role_that_may_update_it_and_the_partner_gets_the_reference_and_the_clinic_code_only(world):
    async def go(rig, http, partner):
        order = (await http.post("/api/portal/orders", json={}, headers=auth(world, "clinic1"))).json()
        rep = await http.post("/api/portal/orders/%s/submit" % order["id"], headers=auth(world, "rep1"))
        stranger = await http.post("/api/portal/orders/%s/submit" % order["id"], headers=auth(world, "clinic2"))
        sent = await http.post("/api/portal/orders/%s/submit" % order["id"], headers=auth(world, "prescriber1"))
        again = await http.post("/api/portal/orders/%s/submit" % order["id"], headers=auth(world, "clinic1"))
        return order, rep, stranger, sent, again, partner
    order, rep, stranger, sent, again, partner = with_api(go)
    assert rep.status_code == 403 and stranger.status_code == 404 and sent.status_code == 200 and sent.json()["status"] == "submitted" and again.json()["replayed"] is True
    assert len(partner.orders) == 1 and json.loads(partner.requests[0]["body"]) == {"order_ref": order["order_ref"], "clinic_ref": "tenant-a-c1"}


def test_the_partner_webhook_applies_a_signed_event_and_refuses_a_forged_one(world):
    async def go(rig, http, partner):
        order = (await http.post("/api/portal/orders", json={}, headers=auth(world, "clinic1"))).json()
        await http.post("/api/portal/orders/%s/submit" % order["id"], headers=auth(world, "clinic1"))
        body, header = partner.event(order["order_ref"], "accepted", event_id="evt-1", extra={"recipient_name": "Jane Roe"})
        forged = await http.post("/api/portal/webhooks/fulfillment", content=body, headers={"X-Partner-Signature": header[:-4] + "0000"})
        ok = await http.post("/api/portal/webhooks/fulfillment", content=body, headers={"X-Partner-Signature": header})
        again = await http.post("/api/portal/webhooks/fulfillment", content=body, headers={"X-Partner-Signature": header})
        mine = await http.get("/api/portal/orders/" + order["id"], headers=auth(world, "clinic1"))
        return forged, ok, again, mine
    forged, ok, again, mine = with_api(go)
    assert forged.status_code == 400 and ok.json() == {"received": True, "result": "applied"} and again.json()["result"] == "duplicate"
    assert mine.json()["status"] == "accepted" and "Jane" not in mine.text


def test_an_invoice_is_made_and_finalized_by_an_admin_read_by_its_clinic_and_rep_and_hidden_from_the_others(world):
    async def go(rig, http, partner):
        no_key = await http.post("/api/portal/invoices", json={"clinic_id": world.a.clinics["c1"], "lines": LINES}, headers=auth(world, "admin"))
        made = await http.post("/api/portal/invoices", json={"clinic_id": world.a.clinics["c1"], "lines": LINES}, headers=idem(world, "admin", "inv-1"))
        again = await http.post("/api/portal/invoices", json={"clinic_id": world.a.clinics["c1"], "lines": LINES}, headers=idem(world, "admin", "inv-1"))
        reused = await http.post("/api/portal/invoices", json={"clinic_id": world.a.clinics["c2"], "lines": LINES}, headers=idem(world, "admin", "inv-1"))
        clinic_makes = await http.post("/api/portal/invoices", json={"clinic_id": world.a.clinics["c1"], "lines": LINES}, headers=idem(world, "clinic1", "inv-2"))
        bad_line = await http.post("/api/portal/invoices", json={"clinic_id": world.a.clinics["c1"], "lines": [{"description": "x", "quantity": 0, "unit_amount_minor": 5}]}, headers=idem(world, "admin", "inv-3"))
        extra = await http.post("/api/portal/invoices", json={"clinic_id": world.a.clinics["c1"], "lines": LINES, "patient": "Jane Roe"}, headers=idem(world, "admin", "inv-4"))
        iid = made.json()["id"]
        clinic_finalizes = await http.post("/api/portal/invoices/%s/finalize" % iid, headers=auth(world, "clinic1"))
        finalized = await http.post("/api/portal/invoices/%s/finalize" % iid, headers=auth(world, "admin"))
        seen = {u: (await http.get("/api/portal/invoices/" + iid, headers=auth(world, u))).status_code for u in USERS}
        lists = {u: await http.get("/api/portal/invoices", headers=auth(world, u)) for u in USERS}
        listed = {u: (len(r.json()["invoices"]) if r.status_code == 200 else r.status_code) for u, r in lists.items()}
        return no_key, made, again, reused, clinic_makes, bad_line, extra, clinic_finalizes, finalized, seen, listed
    no_key, made, again, reused, clinic_makes, bad_line, extra, clinic_finalizes, finalized, seen, listed = with_api(go)
    assert (no_key.status_code, made.status_code, again.json()["replayed"], reused.status_code, clinic_makes.status_code, bad_line.status_code, extra.status_code) == (400, 201, True, 409, 403, 422, 422)
    assert clinic_finalizes.status_code == 403 and finalized.json()["total_minor"] == 9000 and finalized.json()["status"] == "open"
    # a prescriber is not given invoices at all (403 from the matrix); another clinic's user and another rep are given invoices, just not this one (404, as for an id that does not exist)
    assert seen == {"clinic1": 200, "clinic2": 404, "prescriber1": 403, "rep1": 200, "rep2": 404, "admin": 200}
    assert listed == {"clinic1": 1, "clinic2": 0, "prescriber1": 403, "rep1": 1, "rep2": 0, "admin": 1}


def test_a_clinic_pays_its_own_invoice_from_the_wallet_a_rep_reads_the_commission_and_only_an_admin_refunds(world):
    async def go(rig, http, partner):
        iid = (await http.post("/api/portal/invoices", json={"clinic_id": world.a.clinics["c1"], "lines": LINES}, headers=idem(world, "admin", "inv-1"))).json()["id"]
        await http.post("/api/portal/invoices/%s/finalize" % iid, headers=auth(world, "admin"))
        broke = await http.post("/api/portal/invoices/%s/pay" % iid, json={"method": "wallet"}, headers=idem(world, "clinic1", "pay-1"))
        topup = await http.post("/api/portal/payments", json={"amount_minor": 12000, "currency": "USD", "payment_method": "pm_fake_ok"}, headers=idem(world, "clinic1", "top-1"))
        body, header = rig.events.signed(rig.events.payment_intent("payment_intent.succeeded", rig.fake.payment_intents[(await _provider_id(rig, world, topup.json()["payment_id"]))]))
        await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify)
        other_clinic = await http.post("/api/portal/invoices/%s/pay" % iid, json={"method": "wallet"}, headers=idem(world, "clinic2", "pay-x"))
        rep_pays = await http.post("/api/portal/invoices/%s/pay" % iid, json={"method": "wallet"}, headers=idem(world, "rep1", "pay-y"))
        paid = await http.post("/api/portal/invoices/%s/pay" % iid, json={"method": "wallet"}, headers=idem(world, "clinic1", "pay-2"))
        again = await http.post("/api/portal/invoices/%s/pay" % iid, json={"method": "wallet"}, headers=idem(world, "clinic1", "pay-3"))
        commissions = {u: (await http.get("/api/portal/commissions", headers=auth(world, u))) for u in USERS}
        clinic_refunds = await http.post("/api/portal/invoices/%s/refund" % iid, json={"amount_minor": 1000}, headers=idem(world, "clinic1", "rf-1"))
        refunded = await http.post("/api/portal/invoices/%s/refund" % iid, json={"amount_minor": 1000}, headers=idem(world, "admin", "rf-1"))
        after = {u: [(c["kind"], c["amount_minor"]) for c in (await http.get("/api/portal/commissions", headers=auth(world, u))).json()["commissions"]] for u in ("rep1", "rep2", "admin")}
        return broke, topup, other_clinic, rep_pays, paid, again, commissions, clinic_refunds, refunded, after, await counts(rig.db, world.a.tenant)
    broke, topup, other_clinic, rep_pays, paid, again, commissions, clinic_refunds, refunded, after, c = with_api(go)
    assert broke.json()["status"] == "insufficient_funds" and topup.status_code == 201 and topup.json()["status"] == "succeeded"
    assert other_clinic.status_code == 404 and rep_pays.status_code == 403
    assert paid.json()["status"] == "paid" and paid.json()["invoice"]["pay_method"] == "wallet" and again.json()["replayed"] is True
    assert {u: r.status_code for u, r in commissions.items()} == {"clinic1": 403, "clinic2": 403, "prescriber1": 403, "rep1": 200, "rep2": 200, "admin": 200}
    assert [c["amount_minor"] for c in commissions["rep1"].json()["commissions"]] == [450] and commissions["rep2"].json()["commissions"] == []      # c1 pays rep1 500 bp of 9000
    assert clinic_refunds.status_code == 403 and refunded.json()["status"] == "refunded" and after["rep1"] == [("accrual", 450), ("reversal", -50)] and after["rep2"] == []
    assert after["admin"] == after["rep1"] and c["spendable"] == 12000 - 9000 + 1000


async def _provider_id(rig, world, payment_id):
    async with rig.db.tenant(world.a.tenant) as conn:
        return await conn.fetchval("select provider_payment_id from billing_payment where id = $1::uuid", payment_id)


def test_a_clinic_pays_an_invoice_by_bank_debit_and_it_stays_open_until_the_event_and_a_bad_debit_request_is_a_422(world):
    async def go(rig, http, partner):
        iid = (await http.post("/api/portal/invoices", json={"clinic_id": world.a.clinics["c1"], "lines": LINES}, headers=idem(world, "admin", "inv-1"))).json()["id"]
        await http.post("/api/portal/invoices/%s/finalize" % iid, headers=auth(world, "admin"))
        no_mandate = await http.post("/api/portal/invoices/%s/pay" % iid, json={"method": "us_bank_account", "payment_method": ACH_PM, "customer": CUSTOMER}, headers=idem(world, "clinic1", "p0"))
        extra = await http.post("/api/portal/invoices/%s/pay" % iid, json={"method": "us_bank_account", "routing_number": "000000000"}, headers=idem(world, "clinic1", "p0b"))
        pending = await http.post("/api/portal/invoices/%s/pay" % iid, json={"method": "us_bank_account", "payment_method": ACH_PM, "customer": CUSTOMER, "mandate": MANDATE}, headers=idem(world, "clinic1", "p1"))
        second = await http.post("/api/portal/invoices/%s/pay" % iid, json={"method": "card", "payment_method": "pm_fake_ok"}, headers=idem(world, "clinic1", "p2"))
        void = await http.post("/api/portal/invoices/%s/void" % iid, headers=auth(world, "admin"))
        mid = (await http.get("/api/portal/invoices/" + iid, headers=auth(world, "clinic1"))).json()["status"]
        pid = await _provider_id(rig, world, (await http.get("/api/portal/payments", headers=auth(world, "clinic1"))).json()["payments"][0]["id"])
        body, header = rig.events.signed(rig.events.payment_intent("payment_intent.succeeded", rig.fake.settle_ach(pid)))
        await webhooks.receive(rig.db, rig.router, body, header, SECRETS, verify=rig.verify)
        seen = {u: len((await http.get("/api/portal/payments", headers=auth(world, u))).json()["payments"]) for u in ("clinic1", "clinic2", "rep1", "rep2", "admin")}
        no_access = await http.get("/api/portal/payments", headers=auth(world, "prescriber1"))
        return no_mandate, extra, pending, second, void, mid, (await http.get("/api/portal/invoices/" + iid, headers=auth(world, "clinic1"))).json(), seen, no_access
    no_mandate, extra, pending, second, void, mid, final, seen, no_access = with_api(go)
    assert no_mandate.status_code == 422 and extra.status_code == 422 and pending.json()["status"] == "processing" and second.status_code == 409 and void.status_code == 409 and mid == "open"
    assert final["status"] == "paid" and final["pay_method"] == "us_bank_account" and seen == {"clinic1": 1, "clinic2": 0, "rep1": 1, "rep2": 0, "admin": 1} and no_access.status_code == 403


def test_a_provider_that_is_not_configured_is_a_503_not_a_crash_and_a_wallet_payment_needs_none(world):
    async def go(rig, http, partner):
        # the rig has a fake provider and the app was given it; take it away from the app to show the answer
        http._transport.app.state.portal.provider = None
        http._transport.app.state.portal.fulfillment_router = None
        iid = (await http.post("/api/portal/invoices", json={"clinic_id": world.a.clinics["c1"], "lines": LINES}, headers=idem(world, "admin", "inv-1"))).json()["id"]
        await http.post("/api/portal/invoices/%s/finalize" % iid, headers=auth(world, "admin"))
        card = await http.post("/api/portal/invoices/%s/pay" % iid, json={"method": "card", "payment_method": "pm_fake_ok"}, headers=idem(world, "clinic1", "p1"))
        topup = await http.post("/api/portal/payments", json={"amount_minor": 100, "currency": "USD", "payment_method": "pm_fake_ok"}, headers=idem(world, "clinic1", "t1"))
        wallet = await http.post("/api/portal/invoices/%s/pay" % iid, json={"method": "wallet"}, headers=idem(world, "clinic1", "p2"))
        hook = await http.post("/api/portal/webhooks/fulfillment", content=b"{}", headers={"X-Partner-Signature": "t=1,v1=00"})
        return card, topup, wallet, hook
    card, topup, wallet, hook = with_api(go)
    assert (card.status_code, topup.status_code, wallet.status_code, hook.status_code) == (503, 503, 200, 503) and wallet.json()["status"] == "insufficient_funds"


# ---- part 3: the credential is the only source of who is asking -----------------------------------------------------------------------------------

def test_a_header_a_query_or_a_body_that_names_another_tenant_clinic_or_role_changes_nothing(world):
    async def go(rig, http, partner):
        await http.post("/api/portal/orders", json={}, headers=auth(world, "clinic1"))
        await http.post("/api/portal/orders", json={}, headers=auth(world, "clinic2"))
        forged = {"X-Tenant-Id": world.b.tenant, "X-Role": "admin", "X-User-Id": world.a.users["admin"], "X-Clinic-Id": world.a.clinics["c2"]}
        listed = await http.get("/api/portal/orders", params={"tenant_id": world.b.tenant, "role": "admin", "clinic_id": world.a.clinics["c2"]}, headers=auth(world, "clinic1", **forged))
        probe = await http.get("/probe/commissions/read", headers=auth(world, "clinic1", **forged))
        other_tenant = await http.get("/api/portal/orders", headers={"Authorization": "Bearer " + world.b.keys["admin"]})
        return listed.json()["orders"], probe.status_code, other_tenant.json()["orders"]
    mine, probe, other = with_api(go)
    assert [o["clinic_id"] for o in mine] == [world.a.clinics["c1"]] and probe == 403 and other == []


# ---- each route asks for the cell of what it does ------------------------------------------------------------------------------------------------

#: written by hand, as a statement of what each route is: the cell of the matrix its action is. The row level security underneath would refuse most of a wrong guard with the same status,
#: so the guard on the route is checked on its own here.
ROUTE_CELLS = {
    ("POST", "/api/portal/orders"): ("orders", "create"), ("GET", "/api/portal/orders"): ("orders", "read"), ("GET", "/api/portal/orders/{order_id}"): ("orders", "read"),
    ("POST", "/api/portal/orders/{order_id}/submit"): ("orders", "update"), ("POST", "/api/portal/orders/{order_id}/cancel"): ("orders", "update"),
    ("POST", "/api/portal/orders/{order_id}/requeue"): ("orders", "update"),
    ("POST", "/api/portal/invoices"): ("invoices", "create"), ("GET", "/api/portal/invoices"): ("invoices", "read"), ("GET", "/api/portal/invoices/{invoice_id}"): ("invoices", "read"),
    ("POST", "/api/portal/invoices/{invoice_id}/finalize"): ("invoices", "update"), ("POST", "/api/portal/invoices/{invoice_id}/void"): ("invoices", "update"),
    ("POST", "/api/portal/invoices/{invoice_id}/pay"): ("invoices", "settle"), ("POST", "/api/portal/invoices/{invoice_id}/refund"): ("invoices", "refund"),
    ("GET", "/api/portal/payments"): ("payments", "read"), ("POST", "/api/portal/payments"): ("payments", "create"), ("GET", "/api/portal/commissions"): ("commissions", "read"),
}


def test_every_portal_route_asks_for_the_cell_of_what_it_does_and_the_partner_webhook_asks_for_none():
    found = {}
    for route in router.routes:
        cells = {dep.call.cell for dep in route.dependant.dependencies if hasattr(dep.call, "cell")}
        for method in route.methods:
            found[(method, route.path)] = cells
    webhook = found.pop(("POST", "/api/portal/webhooks/fulfillment"))
    assert webhook == set(), "the webhook is authenticated by its signature, not by a user"
    assert found == {key: {cell} for key, cell in ROUTE_CELLS.items()}


def test_a_role_the_table_has_no_row_for_is_refused_at_authentication_not_given_a_default(world, monkeypatch):
    async def go(rig, http, partner):
        before = (await http.get("/probe/orders/read", headers=auth(world, "admin"))).status_code
        monkeypatch.setattr(matrix, "ROLES", ("clinic", "prescriber", "sales_rep"))          # as if the table had been written without admin
        after = await http.get("/probe/orders/read", headers=auth(world, "admin"))
        still = (await http.get("/probe/orders/read", headers=auth(world, "clinic1"))).status_code
        return before, after, still
    before, after, still = with_api(go)
    assert (before, after.status_code, after.json(), still) == (200, 403, {"detail": "unknown role"}, 200)
