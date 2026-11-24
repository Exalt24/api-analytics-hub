"""Purchase order tracking as HTTP calls, against real Postgres with real row level security (app/api/inventory.py).

Who may do what (a viewer reads, an operator changes), the same 404 for an order that is not there and one that belongs to another tenant, every refusal's status code paired with the
same call succeeding for the caller who should have it, and the workbook download. The rules themselves are tested in test_inventory_orders_db.py; this file tests the door.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from inventory_fixtures import DSN, TODAY, add_tenant, admin_rows, needs_db, reset_schema, run  # noqa: E402

pytestmark = needs_db

from app.api.auth import PERMISSIONS, Role, generate_key, hash_key  # noqa: E402
from app.db import Database  # noqa: E402
from app.inventory import store  # noqa: E402
from test_inventory_db import World, seed, stock  # noqa: E402


def add_key(tenant: str, role: str) -> str:
    raw = generate_key()
    admin_rows("insert into api_key (tenant_id, key_hash, label, role) values (%s, %s, %s, %s)", (tenant, hash_key(raw), "api-test-" + role, role))
    return raw


@pytest.fixture(scope="module")
def seeded():
    reset_schema()
    w = World()
    w.a, w.b = str(add_tenant("api-a")), str(add_tenant("api-b"))

    async def build():
        db = Database(DSN, max_size=3)
        await db.connect()
        try:
            w.ids = await seed(db, w.a)
            async with db.tenant(w.b) as conn:
                ids = await store.upsert_variants(conn, w.b, [stock(1, "Other tenant's tee", "TEE-1", 2)])
                await store.save_snapshots(conn, w.b, TODAY, [stock(1, "Other tenant's tee", "TEE-1", 2)], ids)
            async with db.tenant(w.a) as conn:
                await store.make_plan(conn, w.a, TODAY)
        finally:
            await db.close()

    run(build())
    w.keys = {"a_operator": add_key(w.a, "operator"), "a_admin": add_key(w.a, "admin"), "a_viewer": add_key(w.a, "viewer"), "b_operator": add_key(w.b, "operator"), "b_viewer": add_key(w.b, "viewer")}
    return w


@pytest.fixture(scope="module")
def client(seeded):
    os.environ["DATABASE_URL"] = DSN
    os.environ.setdefault("DASHBOARD_ORIGIN", "http://localhost:3000")
    from app.api.main import app as fastapi_app

    loop = asyncio.new_event_loop()
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test", timeout=60)
    stack = AsyncExitStack()
    loop.run_until_complete(stack.enter_async_context(fastapi_app.router.lifespan_context(fastapi_app)))
    yield loop, c
    loop.run_until_complete(stack.aclose())
    loop.run_until_complete(c.aclose())
    loop.close()


def call(client, method, url, key=None, **kw):
    loop, c = client
    headers = kw.pop("headers", {})
    if key:
        headers["Authorization"] = "Bearer %s" % key
    return loop.run_until_complete(getattr(c, method)(url, headers=headers, **kw))


def make_draft(client, seeded):
    r = call(client, "post", "/api/inventory/orders", seeded.keys["a_operator"], json={"supplier": "Acme Goods"})
    assert r.status_code == 201, r.text
    return r.json()


def place(client, seeded, order, **body):
    r = call(client, "post", "/api/inventory/orders/%s/status" % order["id"], seeded.keys["a_operator"], json={"to": "ordered", **body})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------------------------------------ who may do what

def test_the_permissions_a_viewer_reads_and_an_operator_or_admin_writes():
    assert "read:inventory" in PERMISSIONS[Role.VIEWER] and "write:inventory" not in PERMISSIONS[Role.VIEWER]
    assert {"read:inventory", "write:inventory"} <= PERMISSIONS[Role.OPERATOR] and {"read:inventory", "write:inventory"} <= PERMISSIONS[Role.ADMIN]
    assert "read:jobs" in PERMISSIONS[Role.VIEWER] and "write:esign" in PERMISSIONS[Role.OPERATOR]        # what was there is still there


def test_no_key_is_a_401_and_a_viewer_may_read_but_not_write(client, seeded):
    for method, url in (("get", "/api/inventory/orders"), ("post", "/api/inventory/orders"), ("get", "/api/inventory/workbook"), ("post", "/api/inventory/late-check")):
        assert call(client, method, url, **({"json": {"supplier": "Acme Goods"}} if method == "post" else {})).status_code == 401
    assert call(client, "get", "/api/inventory/orders", seeded.keys["a_viewer"]).status_code == 200
    denied = call(client, "post", "/api/inventory/orders", seeded.keys["a_viewer"], json={"supplier": "Acme Goods"})
    assert denied.status_code == 403 and "write:inventory" in denied.json()["detail"]
    assert call(client, "post", "/api/inventory/late-check", seeded.keys["a_viewer"]).status_code == 403
    made = call(client, "post", "/api/inventory/orders", seeded.keys["a_admin"], json={"supplier": "Acme Goods"})
    assert made.status_code == 201                                                                                                  # the same call by a role that may
    assert call(client, "post", "/api/inventory/orders/%s/status" % made.json()["id"], seeded.keys["a_admin"], json={"to": "cancelled"}).status_code == 200     # frees the supplier's draft slot


# ------------------------------------------------------------------------------------------------ making and moving an order

def test_an_order_is_made_from_the_plan_placed_and_listed(client, seeded):
    draft = make_draft(client, seeded)
    assert draft["status"] == "draft" and draft["supplier"] == "Acme Goods" and draft["next_statuses"] == ["ordered", "cancelled"]
    assert [(l["sku"], l["quantity_ordered"], l["unit_cost_minor"]) for l in draft["lines"]] == [("TEE-1", 60, 800)] and draft["total_minor"] == 48000
    again = call(client, "post", "/api/inventory/orders", seeded.keys["a_operator"], json={"supplier": "Acme Goods"})
    assert again.status_code == 409 and "already open" in again.json()["detail"]
    placed = place(client, seeded, draft, on=TODAY.isoformat())
    assert placed["status"] == "ordered" and placed["expected_on"] == "2026-12-03" and placed["lead_time_days"] == 9          # 9 days from the order date
    listed = call(client, "get", "/api/inventory/orders?status=open", seeded.keys["a_viewer"]).json()
    assert draft["id"] in [o["id"] for o in listed["orders"]]
    drafts = call(client, "get", "/api/inventory/orders?status=draft", seeded.keys["a_viewer"]).json()["orders"]
    assert all(o["status"] == "draft" for o in drafts) and draft["id"] not in [o["id"] for o in drafts]
    assert call(client, "get", "/api/inventory/orders?status=bogus", seeded.keys["a_viewer"]).status_code == 422


def test_an_unknown_supplier_is_a_404_and_a_supplier_with_nothing_due_a_409(client, seeded):
    assert call(client, "post", "/api/inventory/orders", seeded.keys["a_operator"], json={"supplier": "Nobody Ltd"}).status_code == 404
    assert call(client, "post", "/api/inventory/orders", seeded.keys["a_operator"], json={"supplier": ""}).status_code == 422
    assert call(client, "post", "/api/inventory/orders", seeded.keys["a_operator"], json={"supplier": "Acme Goods", "tenant_id": seeded.b}).status_code == 422      # no tenant in a body


def test_an_illegal_move_is_409_with_the_reason_and_the_order_is_unchanged(client, seeded):
    order = make_draft(client, seeded)
    place(client, seeded, order)
    for to in ("draft", "received", "partly_received", "ordered"):
        r = call(client, "post", "/api/inventory/orders/%s/status" % order["id"], seeded.keys["a_operator"], json={"to": to})
        assert r.status_code == 409, (to, r.text)
    assert "receipt" in call(client, "post", "/api/inventory/orders/%s/status" % order["id"], seeded.keys["a_operator"], json={"to": "received"}).json()["detail"]
    assert call(client, "get", "/api/inventory/orders/%s" % order["id"], seeded.keys["a_viewer"]).json()["status"] == "ordered"
    ok = call(client, "post", "/api/inventory/orders/%s/status" % order["id"], seeded.keys["a_operator"], json={"to": "confirmed", "expected_on": "2026-12-05", "note": "confirmed by email"})
    assert ok.status_code == 200 and ok.json()["expected_on"] == "2026-12-05" and ok.json()["next_statuses"] == ["shipped", "cancelled"]


def test_the_expected_date_can_be_edited_while_open_and_not_after_the_order_is_cancelled(client, seeded):
    order = make_draft(client, seeded)
    place(client, seeded, order)
    key = seeded.keys["a_operator"]
    assert call(client, "post", "/api/inventory/orders/%s/expected" % order["id"], key, json={"expected_on": "2026-12-20"}).json()["expected_on"] == "2026-12-20"
    assert call(client, "post", "/api/inventory/orders/%s/expected" % order["id"], key, json={"expected_on": "not a date"}).status_code == 422
    assert call(client, "post", "/api/inventory/orders/%s/status" % order["id"], key, json={"to": "cancelled"}).status_code == 200
    assert call(client, "post", "/api/inventory/orders/%s/expected" % order["id"], key, json={"expected_on": "2026-12-21"}).status_code == 409


def test_a_receipt_moves_the_order_and_more_than_ordered_needs_to_be_accepted(client, seeded):
    order = make_draft(client, seeded)
    place(client, seeded, order)
    key, line = seeded.keys["a_operator"], order["lines"][0]["id"]
    part = call(client, "post", "/api/inventory/orders/%s/receipts" % order["id"], key, json={"lines": [{"line_id": line, "quantity": 25}]})
    assert part.status_code == 200 and part.json()["status"] == "partly_received" and part.json()["open_units"] == 35
    over = call(client, "post", "/api/inventory/orders/%s/receipts" % order["id"], key, json={"lines": [{"line_id": line, "quantity": 36}]})
    assert over.status_code == 409 and "more than was ordered" in over.json()["detail"]
    assert call(client, "get", "/api/inventory/orders/%s" % order["id"], key).json()["lines"][0]["quantity_received"] == 25
    assert call(client, "post", "/api/inventory/orders/%s/receipts" % order["id"], key, json={"lines": [{"line_id": line, "quantity": 0}]}).status_code == 422
    assert call(client, "post", "/api/inventory/orders/%s/receipts" % order["id"], key, json={"lines": []}).status_code == 422
    assert call(client, "post", "/api/inventory/orders/%s/receipts" % order["id"], key, json={"lines": [{"line_id": line + 9999, "quantity": 1}]}).status_code == 404
    done = call(client, "post", "/api/inventory/orders/%s/receipts" % order["id"], key, json={"lines": [{"line_id": line, "quantity": 38}], "allow_over": True})
    d = done.json()
    assert done.status_code == 200 and d["status"] == "received" and d["lines"][0]["over_received"] is True and d["lines"][0]["quantity_open"] == 0 and d["can_receive"] is False
    assert call(client, "post", "/api/inventory/orders/%s/receipts" % order["id"], key, json={"lines": [{"line_id": line, "quantity": 1}], "allow_over": True}).status_code == 409


def test_a_viewer_cannot_move_or_receive(client, seeded):
    order = make_draft(client, seeded)
    place(client, seeded, order)
    key = seeded.keys["a_viewer"]
    assert call(client, "post", "/api/inventory/orders/%s/status" % order["id"], key, json={"to": "confirmed"}).status_code == 403
    assert call(client, "post", "/api/inventory/orders/%s/receipts" % order["id"], key, json={"lines": [{"line_id": order["lines"][0]["id"], "quantity": 1}]}).status_code == 403
    assert call(client, "post", "/api/inventory/orders/%s/expected" % order["id"], key, json={"expected_on": "2026-12-30"}).status_code == 403


# ------------------------------------------------------------------------------------------------ tenant isolation at the door

def test_another_tenants_order_and_a_missing_one_answer_with_the_same_404(client, seeded):
    order = make_draft(client, seeded)
    place(client, seeded, order)
    missing = str(uuid.uuid4())
    theirs = call(client, "get", "/api/inventory/orders/%s" % order["id"], seeded.keys["b_operator"])
    nobody = call(client, "get", "/api/inventory/orders/%s" % missing, seeded.keys["b_operator"])
    assert theirs.status_code == nobody.status_code == 404 and theirs.json() == nobody.json()
    assert call(client, "get", "/api/inventory/orders/not-a-uuid", seeded.keys["a_viewer"]).status_code == 404
    for url, body in (("status", {"to": "cancelled"}), ("expected", {"expected_on": "2026-12-30"}), ("receipts", {"lines": [{"line_id": order["lines"][0]["id"], "quantity": 1}]})):
        r = call(client, "post", "/api/inventory/orders/%s/%s" % (order["id"], url), seeded.keys["b_operator"], json=body)
        assert r.status_code == 404, (url, r.text)
    assert call(client, "get", "/api/inventory/orders/%s" % order["id"], seeded.keys["a_viewer"]).json()["status"] == "ordered"                    # tenant B changed nothing
    assert call(client, "get", "/api/inventory/orders", seeded.keys["b_viewer"]).json()["orders"] == []


# ------------------------------------------------------------------------------------------------ the late check

def test_the_late_check_raises_each_late_order_once(client, seeded):
    order = make_draft(client, seeded)
    place(client, seeded, order, on="2026-10-01", expected_on="2026-10-20")
    key = seeded.keys["a_operator"]
    first = call(client, "post", "/api/inventory/late-check", key).json()
    second = call(client, "post", "/api/inventory/late-check", key).json()
    assert first["raised"] >= 1 and second["raised"] == 0 and second["already_raised"] >= first["raised"]
    assert call(client, "get", "/api/inventory/orders/%s" % order["id"], key).json()["late"] is True
    assert admin_rows("select count(*) from inventory_po_alert where order_id = %s", (order["id"],))[0][0] == 1


# ------------------------------------------------------------------------------------------------ the workbook

def test_the_workbook_downloads_for_a_viewer_with_live_formulas_and_the_plans_figures(client, seeded):
    from openpyxl import load_workbook
    r = call(client, "get", "/api/inventory/workbook", seeded.keys["a_viewer"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert r.headers["content-disposition"] == 'attachment; filename="inventory-plan-2026-11-24.xlsx"'
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Inputs", "Plan", "Classes", "Scenario"]
    plan = wb["Plan"]
    skus = {plan["A%d" % r_].value: r_ for r_ in range(2, plan.max_row + 1)}
    assert {"TEE-1", "MUG-1", "HAT-1"} <= set(skus)                                           # the untracked gift card is left out
    assert plan["N%d" % skus["TEE-1"]].value.startswith("=IF(") and plan["G%d" % skus["TEE-1"]].value == pytest.approx(2.0)
    assert wb["Inputs"]["A13"].value == "Acme Goods" and wb["Inputs"]["B13"].value == 9


def test_the_workbook_takes_a_promotion_and_refuses_a_bad_one_and_a_tenant_with_no_plan_gets_404(client, seeded):
    from openpyxl import load_workbook
    r = call(client, "get", "/api/inventory/workbook?promo_factor=1.5&promo_from=2026-12-01&promo_to=2026-12-07&service_level=0.9&review_days=21", seeded.keys["a_viewer"])
    inputs = load_workbook(io.BytesIO(r.content))["Inputs"]
    assert (inputs["B4"].value, inputs["B5"].value, inputs["B6"].value) == (0.9, 21, 1.5) and inputs["B7"].value.date().isoformat() == "2026-12-01"
    for bad in ("service_level=1.5", "promo_factor=0", "review_days=-1", "promo_factor=abc"):
        assert call(client, "get", "/api/inventory/workbook?" + bad, seeded.keys["a_viewer"]).status_code == 422, bad
    assert call(client, "get", "/api/inventory/workbook", seeded.keys["b_viewer"]).status_code == 404
