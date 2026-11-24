#!/usr/bin/env python
# LIFESPAN: permanent
"""Run purchase order tracking and the plan workbook against the LIVE development store and the local Postgres, and write what happened to a dated file.

    python scripts/inventory_orders_run.py --out docs/test-runs/2026-11-24-inventory-orders-and-workbook.txt --workbook docs/inventory-plan-dev-store.xlsx [--browser-dir DIR]

It follows scripts/inventory_live_check.py: it reads the store whose token file SHOPIFY_DEV_TOKEN_FILE names (read only; the products it plans on are the IPT test products that
scripts/inventory_dev_store.py makes and removes), drops and rebuilds the `public` schema of ANALYTICS_TEST_DSN, makes a tenant and keys, syncs, and enters what the store does not hold
(suppliers, their lead times, a promotion): the dev store has none, so they are inputs, and the file says so. Then, on the real plan:

  PART 1  an order from the plan's draft, placed (it lands after the shelf is empty, so the shortage stays and no second order is suggested), its expected date moved (the shortage
          clears), received in parts with an over-receipt refused, a second order that is late and reported once through the outbox, and the workbook recalculated in LibreOffice and
          compared with the Python plan cell by cell, with a change of one unit that must fail the check.
  PART 2  the same on a CONSTRUCTED stock history, because a store set up today has none.
  PART 3  with --browser-dir, the Inventory page driven in Chromium against the real backend: make an order from the draft, place it, receive part of it, meet the refusal, download the
          workbook.

Nothing is written to the shop. The two parts are labelled so the file never says more than was done.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402
import psycopg  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

import inventory_live_check as live  # noqa: E402
from app.changes import deliver_pending  # noqa: E402
from app.connectors.shopify import API_VERSION, ShopifyConnector  # noqa: E402
from app.db import Database  # noqa: E402
from app.inventory import alerts, model, order_store, orders, store, workbook, workbook_check  # noqa: E402
from app.inventory.model import PlanParams  # noqa: E402
from app.inventory.reader import OrderLine, ShopifyInventoryReader  # noqa: E402
from app.inventory.sync import sync_tenant  # noqa: E402
from app.secret_files import DEV_TOKEN  # noqa: E402

say = live.say
DSN = live.DSN
SUPPLIERS = (("Harbor Textiles", 21, 4.0), ("Pacific Caps", 30, 5.0))
ASSIGN = (("IPT-TEE-M", "Harbor Textiles", 24, 6), ("IPT-TEE-S", "Harbor Textiles", 24, 6), ("IPT-TEE-L", "Harbor Textiles", 24, 6),
          ("IPT-SOCK-L", "Harbor Textiles", 48, 12), ("IPT-CAP-R", "Pacific Caps", 60, 12))


async def show(db: Database, tenant: str, sku: str, as_of: date, label: str) -> dict:
    """Plan again and say what the plan reads for one variant."""
    async with db.tenant(tenant) as conn:
        await store.make_plan(conn, tenant, as_of, PlanParams())
        r = await conn.fetchrow("select * from inventory_plan_line where sku = $1", sku)
    cover = "no sales" if r["cover_days"] is None else "%.1f" % r["cover_days"]
    when = "" if r["inbound_expected_on"] is None else ", expected %s%s" % (r["inbound_expected_on"], " (LATE)" if r["inbound_late"] else "")
    say("  %-34s %s: on hand %d, inbound %d (%s%s), cover %s days against a %d day lead time, shortage: %s, needs an order: %s, suggested %d" % (
        label, sku, r["available"], r["incoming"], r["inbound_source"], when, cover, r["lead_time_days"], r["shortage"] or "none", "yes" if r["needs_reorder"] else "no", r["suggested_qty"]))
    return dict(r)


async def attempt(db: Database, tenant: str, fn):
    try:
        async with db.tenant(tenant) as conn:
            return await fn(conn)
    except orders.OrderError as exc:
        return exc


async def workbook_check_on_store(db: Database, tenant: str, today: date, out: Path | None) -> None:
    async with db.tenant(tenant) as conn:
        data = await store.workbook_inputs(conn, today)
        params = PlanParams(service_level=data.service_level, review_days=data.review_days)
        inputs, _ = await store.load_inputs(conn, today, params)
    by_id = {i.key: i for i in inputs}
    items = [by_id[v] for v in data.variant_ids]
    promos = (model.Promo(data.promo_start, data.promo_end, data.promo_factor),)
    xlsx = workbook.build_workbook(data)
    if out:
        out.write_bytes(xlsx)
    mismatches, engine = workbook_check.check_inputs(data, items, params, promos, xlsx=xlsx)
    cells = workbook_check.compared_cells(len(items))
    say("  workbook of the plan of %s: %d variants, %d suppliers, promotion factor %.2f from %s to %s" % (data.as_of, len(items), len(data.suppliers), data.promo_factor, data.promo_start, data.promo_end))
    say("  recalculated in %s: %d computed cells compared with the Python plan, %d differ" % (engine, cells, len(mismatches)))
    for line in mismatches[:10]:
        say("    " + line)
    wb = load_workbook(io.BytesIO(xlsx))
    cell = wb["Inputs"]["B%d" % workbook.SUPPLIER_FIRST_ROW]
    name, was = wb["Inputs"]["A%d" % workbook.SUPPLIER_FIRST_ROW].value, cell.value
    cell.value = was + 1
    changed = io.BytesIO()
    wb.save(changed)
    broken, _ = workbook_check.check_inputs(data, items, params, promos, xlsx=changed.getvalue())
    say("  self-test: %s's lead time changed from %s to %s days by one: %d cell(s) differ, so the check can fail%s" % (name, was, was + 1, len(broken), "" if broken else " (NOT CAUGHT)"))
    for line in broken[:3]:
        say("    " + line)
    calc, _ = workbook_check.recalculate(xlsx)
    s = calc["Scenario"]
    tee = next(n for n, v in enumerate(data.variants) if v.sku == "IPT-TEE-M")
    r = workbook.SCENARIO_FIRST_ROW + tee
    say("  Scenario sheet for IPT-TEE-M, promotion factor %.2f: safety stock %s -> %s, reorder point %s -> %s, order %s -> %s units (before -> after)" % (
        data.promo_factor, s["D%d" % r].value, s["E%d" % r].value, s["F%d" % r].value, s["G%d" % r].value, s["H%d" % r].value, s["I%d" % r].value))
    classes = calc["Classes"]
    say("  Classes sheet: " + "; ".join("%s %s variants, %.0f%% of sales" % (classes["A%d" % n].value, classes["B%d" % n].value, 100 * (classes["D%d" % n].value or 0)) for n in range(4, 8)))


async def part1(db: Database, tenant: str, workbook_out: Path | None) -> date:
    text = DEV_TOKEN.read()
    shop = re.search(r"^SHOP=(.+)$", text, re.M).group(1).strip()
    token = re.search(r"^ACCESS_TOKEN=(.+)$", text, re.M).group(1).strip()
    connector = ShopifyConnector(shop_domain=shop, access_token=token, page_size=100)
    say("PART 1: the development store, read live, then purchase orders and the workbook on its plan")
    say("store: %s, Admin GraphQL %s, read only" % (await connector.verify(), API_VERSION))
    today = datetime.now(timezone.utc).date()
    result = await sync_tenant(db, tenant, ShopifyInventoryReader(connector), today=today)
    say("sync on %s: %d variants read with their stock levels, %d snapshots stored, %d order lines stored from %d orders, %d Shopify calls" % (
        today, result.variants, result.snapshots, result.order_lines, result.orders_read, result.api_calls))
    async with db.tenant(tenant) as conn:
        for name, lead, sd in SUPPLIERS:
            await store.upsert_supplier(conn, tenant, name=name, lead_time_days=lead, lead_time_sd_days=sd, note="entered for the run")
        for ref, sup, moq, pack in ASSIGN:
            await store.assign_supplier(conn, ref, sup, moq=moq, pack_size=pack)
    say("Inputs entered for this run (the store holds no supplier data, so these are figures a person gives, not read): Harbor Textiles lead time 21 days (deviation 4) for the tees and the")
    say("  socks, minimum 24 and packs of 6 (socks 48 and 12); Pacific Caps 30 days (deviation 5), minimum 60, packs of 12. Unit costs are the store's own cost field.")
    say()

    say("1. The plan before any order, and a draft order made from it")
    before = await show(db, tenant, "IPT-TEE-M", today, "no order yet")
    await show(db, tenant, "IPT-SOCK-L", today, "no order yet")
    async with db.tenant(tenant) as conn:
        oid = await order_store.create_from_plan(conn, "Harbor Textiles", "run operator")
        draft = await order_store.get_order(conn, oid, as_of=today)
    say("  draft %s for Harbor Textiles from the plan of %s: %s, total %s %s, status %s" % (
        oid[:8], draft["plan_as_of"], ", ".join("%d x %s at %s" % (l["quantity_ordered"], l["sku"], l["unit_cost_minor"] / 100) for l in draft["lines"]), draft["total_minor"] / 100, draft["currency"], draft["status"]))
    again = await attempt(db, tenant, lambda c: order_store.create_from_plan(c, "Harbor Textiles", "run operator"))
    say("  a second draft for the same supplier is refused: %s" % again)
    await show(db, tenant, "IPT-TEE-M", today, "draft only")
    say("  (a draft is not inbound: the plan is unchanged until the order is placed)")
    say()

    say("2. The order is placed: the expected date is the order date plus the supplier's lead time, and the plan reads what is on the way")
    async with db.tenant(tenant) as conn:
        await order_store.move(conn, oid, "ordered", "run operator", on=today)
        placed = await order_store.get_order(conn, oid, as_of=today)
    say("  placed on %s, lead time %d days, expected %s" % (placed["ordered_on"], placed["lead_time_days"], placed["expected_on"]))
    tee = await show(db, tenant, "IPT-TEE-M", today, "ordered, lands in 21 days")
    await show(db, tenant, "IPT-SOCK-L", today, "ordered, lands in 21 days")
    say("  The tee's shelf lasts %.1f days and the order lands on day 21: it is too late to help, so the shortage stays, but no second order is suggested (the %d units on the way count)." % (tee["cover_days"], tee["incoming"]))
    say()

    say("3. The supplier confirms an expedited shipment: the expected date is edited")
    async with db.tenant(tenant) as conn:
        await order_store.move(conn, oid, "confirmed", "run operator", on=today, expected_on=today + timedelta(days=14), note="supplier confirmed an expedited shipment")
    tee = await show(db, tenant, "IPT-TEE-M", today, "confirmed, lands in 14 days")
    say("  The 24 tees now land on day 14, before the shelf (%.1f days) is empty: the tee shows no shortage (shortage: %s) and the forecast cover is %.1f days." % (
        before["cover_days"], tee["shortage"] or "none", tee["forecast_cover_days"]))
    illegal = await attempt(db, tenant, lambda c: order_store.move(c, oid, "draft", "run operator"))
    say("  an illegal move is refused, loudly: %s" % illegal)
    manual = await attempt(db, tenant, lambda c: order_store.move(c, oid, "received", "run operator"))
    say("  and so is moving to received by hand: %s" % manual)
    say()

    say("4. Receipts: the socks arrive in parts, an over-receipt is refused, the rest is accepted")
    async with db.tenant(tenant) as conn:
        sock = await order_store.find_line(conn, oid, "IPT-SOCK-L")
        teeline = await order_store.find_line(conn, oid, "IPT-TEE-M")
        status = await order_store.receive(conn, oid, [(sock, 20)], "dock", on=today)
    say("  20 of 48 socks received: order status %s" % status)
    over = await attempt(db, tenant, lambda c: order_store.receive(c, oid, [(sock, 40)], "dock"))
    say("  40 more socks would be 60 of 48 ordered, refused: %s" % over)
    async with db.tenant(tenant) as conn:
        await order_store.receive(conn, oid, [(sock, 28), (teeline, 24)], "dock", on=today)
        done = await order_store.get_order(conn, oid, as_of=today)
    say("  the other 28 socks and the 24 tees received: order status %s, open units %d, closed on %s" % (done["status"], done["open_units"], done["closed_on"]))
    await show(db, tenant, "IPT-TEE-M", today, "received (the shop's own stock is unchanged)")
    say("  (the run does not write to the shop, so the shop's stock reading did not change; a real receipt would show in the next sync)")
    say()

    say("5. The workbook with live formulas, recalculated outside the hub")
    async with db.tenant(tenant) as conn:
        await store.add_promo(conn, tenant, ref=None, starts_on=today + timedelta(days=3), ends_on=today + timedelta(days=17), factor=1.5, entered_by="run operator", note="entered for the run")
        await store.make_plan(conn, tenant, today, PlanParams())
    say("  promotion input: the ads team plans a 1.5 factor on every variant from %s to %s (entered for the run, not read from anywhere)" % (today + timedelta(days=3), today + timedelta(days=17)))
    await workbook_check_on_store(db, tenant, today, workbook_out)
    say()

    say("6. A late order is reported once")
    async with db.tenant(tenant) as conn:
        late_id = await order_store.create_from_plan(conn, "Harbor Textiles", "run operator")
        await order_store.move(conn, late_id, "ordered", "run operator", on=today - timedelta(days=30), expected_on=today - timedelta(days=9))
    for attempt_no in (1, 2):
        async with db.tenant(tenant) as conn:
            raised = await order_store.raise_late_alerts(conn, tenant, today)
        say("  late check %d: %d raised, %d already raised" % (attempt_no, raised.new, raised.already_raised))
    async with db.tenant(tenant) as conn:
        await order_store.set_expected(conn, late_id, today - timedelta(days=2), "run operator")
        again = await order_store.raise_late_alerts(conn, tenant, today)
        digest = await order_store.open_late_orders(conn, today)
    say("  the expected date moved to 2 days ago: %d raised, %d already raised (one alert per order)" % (again.new, again.already_raised))
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200)

    with psycopg.connect(DSN, autocommit=True) as pg:
        sink = alerts.webhook_sink("https://hooks.example/inventory", client=httpx.Client(transport=httpx.MockTransport(handler)))
        first = deliver_pending(pg, sink, table=alerts.LATE_OUTBOX_TABLE)
        second = deliver_pending(pg, sink, table=alerts.LATE_OUTBOX_TABLE)
    say("  delivered %d through the outbox to a stand-in webhook (a second pass sends %d):" % (first.delivered, second.delivered))
    for line in sent:
        say("    " + line)
    say("  digest: " + alerts.describe_shortages(digest).replace("\n", " | "))
    say()

    return today


async def constructed_part(db: Database) -> None:
    today = datetime.now(timezone.utc).date()
    window = model.date_range(today - timedelta(days=28), today)
    say("PART 2: the same on a CONSTRUCTED stock history (not the store: a store set up today has no history; these snapshots were written by the same code a daily sync uses)")
    say("A variant sells 2 units on every day it is on the shelf: on the shelf days 1 to 10, none on days 11 to 18, back on days 19 to 28, 10 on the shelf today, supplier lead time 9 days, minimum 50, packs of 12.")
    tenant = live.make_tenant("constructed-orders", "Constructed history")
    sales = [OrderLine("o%d" % d, "l%d" % d, "gid://shopify/ProductVariant/1", None, window[d - 1], 2, 5000, "USD") for d in range(1, 29) if not 11 <= d <= 18]
    async with db.tenant(tenant) as conn:
        ids = await store.upsert_variants(conn, tenant, [live.vs(1, "Constructed Tee", "CON-TEE", 10)])
        for d in range(1, 29):
            level = 5 if d <= 10 else 0 if d <= 18 else 20
            await store.save_snapshots(conn, tenant, window[d - 1], [live.vs(1, "Constructed Tee", "CON-TEE", level)], ids)
        await store.save_snapshots(conn, tenant, today, [live.vs(1, "Constructed Tee", "CON-TEE", 10)], ids)
        await store.replace_order_lines(conn, tenant, today - timedelta(days=120), today, sales, ids)
        await store.upsert_supplier(conn, tenant, name="Constructed Supplier", lead_time_days=9)
        await store.assign_supplier(conn, "CON-TEE", "Constructed Supplier", moq=50, pack_size=12)
    await show(db, tenant, "CON-TEE", today, "no order")
    async with db.tenant(tenant) as conn:
        oid = await order_store.create_from_plan(conn, "Constructed Supplier", "run operator")
        await order_store.move(conn, oid, "ordered", "run operator", on=today, expected_on=today + timedelta(days=8))
    await show(db, tenant, "CON-TEE", today, "60 on the way, lands day 8")
    say("  The shelf (5.0 days) is empty on day 5 and the order lands on day 8: the shortage is real, the order is too late, and no second order is suggested.")
    async with db.tenant(tenant) as conn:
        await order_store.set_expected(conn, oid, today + timedelta(days=3), "run operator")
    await show(db, tenant, "CON-TEE", today, "60 on the way, lands day 3")
    say("  Landing on day 3 the order arrives while the shelf still has stock: no shortage, no second order, and the forecast cover is the shelf plus the 60 at 2 a day.")
    async with db.tenant(tenant) as conn:
        await order_store.move(conn, oid, "cancelled", "run operator")
    await show(db, tenant, "CON-TEE", today, "the order is cancelled")
    say("  A cancelled order stops counting: the shortage and the suggested order are back.")
    say()


async def part3(db: Database, tenant: str, keys: dict[str, str], browser_dir: Path) -> None:
    """The Inventory page in Chromium against the real backend and the same database, as a login role that is not a superuser. Two runs: a look at the open orders (one is late), then,
    once that order's goods have arrived and the plan is made again, the order flow itself: make an order from the draft, place it, receive part of it, meet the refusal, download."""
    say("PART 3: the Inventory page driven in a real browser against the real backend (built dashboard, Chromium, an admin key; the workbook comes from the running API)")
    login = "inventory_run_login"
    with psycopg.connect(DSN, autocommit=True) as pg:
        pg.execute("drop role if exists %s" % login)
        pg.execute("create role %s login password '%s' in role app_request, sync_worker" % (login, login))
    app_dsn = "postgresql://%s:%s@%s" % (login, login, DSN.split("@", 1)[1])
    env = {**os.environ, "DATABASE_URL": app_dsn, "DASHBOARD_ORIGIN": "http://127.0.0.1:3100", "PYTHONIOENCODING": "utf-8"}
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.api.main:app", "--port", "8010"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def node(extra: dict[str, str]) -> None:
        run = subprocess.run(["node", "scripts/inventory-e2e.mjs", "http://127.0.0.1:3100", keys["admin"], str(browser_dir)], cwd=ROOT / "web", capture_output=True, text=True,
                             encoding="utf-8", timeout=300, env={**env, "INVENTORY_E2E_SERVE": "1", "INVENTORY_API_BASE": "http://127.0.0.1:8010", **extra})
        for line in (run.stdout + run.stderr).splitlines():
            say(line)
        say("exit code %d" % run.returncode)

    try:
        up = False
        for _ in range(100):
            time.sleep(0.3)
            try:
                up = httpx.get("http://127.0.0.1:8010/health", timeout=2).status_code == 200
            except httpx.HTTPError:
                up = False
            if up:
                break
        if not up:
            say("FAIL   the backend did not come up")
            return
        browser_dir.mkdir(parents=True, exist_ok=True)
        say("a look at the open orders: INVENTORY_E2E_SERVE=1 INVENTORY_E2E_LOOK=1 node web/scripts/inventory-e2e.mjs http://127.0.0.1:3100 <admin key> <folder>   (backend: uvicorn app.api.main:app on 8010)")
        today = datetime.now(timezone.utc).date()
        async with db.tenant(tenant) as conn:
            await store.make_plan(conn, tenant, today, PlanParams())            # the page reads the stored plan, so make it again with the open order in it
        node({"INVENTORY_E2E_LOOK": "1"})
        async with db.tenant(tenant) as conn:
            late = await conn.fetchrow("select id from inventory_purchase_order where status = 'ordered' order by expected_on limit 1")
            lines = await conn.fetch("select id, quantity_ordered from inventory_purchase_order_line where order_id = $1", late["id"])
            await order_store.receive(conn, str(late["id"]), [(l["id"], l["quantity_ordered"]) for l in lines], "dock", on=today)
            await store.make_plan(conn, tenant, today, PlanParams())
        say("the late order's goods arrived and were received in full; the plan was made again")
        say("the order flow: INVENTORY_E2E_SERVE=1 INVENTORY_E2E_ORDERS=1 node web/scripts/inventory-e2e.mjs http://127.0.0.1:3100 <admin key> <folder>")
        node({"INVENTORY_E2E_ORDERS": "1"})
    finally:
        api.terminate()
        try:
            api.wait(timeout=15)
        except subprocess.TimeoutExpired:
            api.kill()
    say()


async def main_async(args) -> None:
    live.build_schema()
    tenant = live.make_tenant("dev-store-orders", "Dev store orders")
    keys = live.make_keys(tenant)
    db = Database(DSN, max_size=4)
    await db.connect()
    try:
        await part1(db, tenant, args.workbook)
        await constructed_part(db)
        if args.browser_dir:
            # the page run goes last and on the dev store's tenant: it makes one more order and receives part of it
            await part3(db, tenant, keys, args.browser_dir)
    finally:
        await db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workbook", type=Path, default=None, help="also keep the workbook of the plan here")
    ap.add_argument("--browser-dir", type=Path, default=None, help="drive the page in Chromium and keep its screenshots and downloads here")
    args = ap.parse_args()
    started = datetime.now(timezone.utc)
    say("Inventory purchase orders and workbook run, %s UTC. Local: %s, Python %s, Postgres from the compose db service (the request role, forced row level security)." % (
        started.strftime("%Y-%m-%d %H:%M"), platform.platform(), platform.python_version()))
    say("Spreadsheet engine for the recalculation: %s" % (workbook_check.engine_name() or "none installed"))
    say()
    asyncio.run(main_async(args))
    args.out.write_text("\n".join(live.OUT) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
