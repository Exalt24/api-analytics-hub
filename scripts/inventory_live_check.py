#!/usr/bin/env python
# LIFESPAN: permanent
"""Run the inventory planning loop against the LIVE development store and the local Postgres, and write what happened to a dated file.

    python scripts/inventory_live_check.py --out docs/test-runs/2026-11-24-inventory-planning-dev-store.txt [--keys-out FILE]

It reads the store whose token file SHOPIFY_DEV_TOKEN_FILE names (read only), drops and rebuilds the `public` schema of the database ANALYTICS_TEST_DSN names (default the compose
Postgres on 5433), makes a tenant and an admin and a viewer API key, syncs, enters suppliers and a promotion (inputs a person gives: the store holds none), plans, raises the alerts
twice, delivers them to a stand-in webhook, and then shows the stockout adjustment on a constructed stock history, because a development store that was set up today has no history of
its own. The two parts are labelled in the output so the file never says more than was done. Nothing is written to the shop.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import psycopg  # noqa: E402

from app.api.auth import generate_key, hash_key  # noqa: E402
from app.changes import deliver_pending  # noqa: E402
from app.connectors.shopify import API_VERSION, ShopifyConnector  # noqa: E402
from app.db import Database  # noqa: E402
from app.inventory import alerts, store  # noqa: E402
from app.inventory.model import PlanParams, date_range  # noqa: E402
from app.inventory.reader import LocationLevel, OrderLine, ShopifyInventoryReader, VariantStock  # noqa: E402
from app.inventory.sync import sync_tenant  # noqa: E402
from app.secret_files import DEV_TOKEN  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")
OUT: list[str] = []


def say(text: str = "") -> None:
    print(text)
    OUT.append(text)


def table(rows) -> str:
    head = ("variant", "sku", "avail", "sold/day", "days counted", "cover", "lead", "safety", "ROP", "order", "class", "label")
    body = [head]
    for r in rows:
        name = r["product_title"] + ("" if r["variant_title"] in ("", "Default Title") else " - " + r["variant_title"])
        body.append((name[:34], r["sku"] or "-", str(r["available"]), "%.2f" % r["velocity"], "%d of %d" % (r["counted_days"], r["window_days"]),
                     "-" if r["cover_days"] is None else "%.1f" % r["cover_days"], "-" if r["lead_time_days"] is None else str(r["lead_time_days"]),
                     "-" if r["safety_stock"] is None else str(r["safety_stock"]), "-" if r["reorder_point"] is None else str(r["reorder_point"]),
                     str(r["suggested_qty"]), r["abc_class"], r["label"]))
    widths = [max(len(row[i]) for row in body) for i in range(len(head))]
    return "\n".join("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip() for row in body)


def admin(sql: str, *params):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def build_schema() -> None:
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade; create schema public")
        for name in ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql", "020_inventory_planning.sql", "021_purchase_orders.sql"):
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))


def make_tenant(slug: str, name: str) -> str:
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select gen_random_uuid()")
        tid = cur.fetchone()[0]
        cur.execute("select set_config('app.tenant_id', %s, false)", (str(tid),))
        cur.execute("insert into tenant (id, slug, name) values (%s, %s, %s)", (tid, slug, name))
    return str(tid)


def make_keys(tenant: str) -> dict[str, str]:
    keys = {"admin": generate_key(), "viewer": generate_key()}
    for role, raw in keys.items():
        admin("insert into api_key (tenant_id, key_hash, label, role) values (%s, %s, %s, %s)", tenant, hash_key(raw), "inventory-" + role, role)
    return keys


async def live_part(db: Database, tenant: str) -> None:
    text = DEV_TOKEN.read()
    shop = re.search(r"^SHOP=(.+)$", text, re.M).group(1).strip()
    token = re.search(r"^ACCESS_TOKEN=(.+)$", text, re.M).group(1).strip()
    connector = ShopifyConnector(shop_domain=shop, access_token=token, page_size=100)
    say("PART 1: the development store, read live")
    say("store: %s, Admin GraphQL %s, read only" % (await connector.verify(), API_VERSION))
    today = datetime.now(timezone.utc).date()
    reader = ShopifyInventoryReader(connector)
    result = await sync_tenant(db, tenant, reader, today=today)
    say("sync on %s: %d variants read with their stock levels, %d snapshots stored, %d order lines stored from %d orders, %d Shopify calls"
        % (today, result.variants, result.snapshots, result.order_lines, result.orders_read, result.api_calls))
    async with db.tenant(tenant) as conn:
        say("stored: %d variants, %d snapshots, %d order lines (all under row level security as the request role)" % tuple(
            [await conn.fetchval("select count(*) from %s" % t) for t in ("inventory_variant", "inventory_snapshot", "inventory_order_line")]))
        levels = await conn.fetch(
            "select v.sku, v.product_title, v.variant_title, s.available, s.on_hand, s.incoming, jsonb_array_length(s.levels) as locations from inventory_snapshot s join inventory_variant v on v.id = s.variant_id "
            "where s.observed_on = $1 and v.product_title like 'IPT %' order by v.sku", today)
        say("stock levels read for the test products, by variant:")
        for r in levels:
            say("  %-11s %-18s available %3d on hand %3d incoming %d (%d location%s)" % (r["sku"], r["product_title"], r["available"], r["on_hand"], r["incoming"], r["locations"], "" if r["locations"] == 1 else "s"))
        untracked = await conn.fetchval("select count(*) from inventory_variant where not tracked")
        say("variants whose stock the store does not track: %d (kept, never planned)" % untracked)
    say()
    say("Inputs entered for this run (the store holds no supplier data, so these are figures a person gives, not read):")
    async with db.tenant(tenant) as conn:
        await store.upsert_supplier(conn, tenant, name="Harbor Textiles", lead_time_days=21, lead_time_sd_days=4.0, note="entered for the run")
        await store.upsert_supplier(conn, tenant, name="Pacific Caps", lead_time_days=30, lead_time_sd_days=5.0, note="entered for the run")
        for ref, sup, moq, pack in (("IPT-TEE-M", "Harbor Textiles", 24, 6), ("IPT-TEE-S", "Harbor Textiles", 24, 6), ("IPT-TEE-L", "Harbor Textiles", 24, 6),
                                    ("IPT-SOCK-L", "Harbor Textiles", 48, 12), ("IPT-CAP-R", "Pacific Caps", 60, 12)):
            await store.assign_supplier(conn, ref, sup, moq=moq, pack_size=pack)
    say("  Harbor Textiles: lead time 21 days (deviation 4) for the tees, 21 days for the socks, minimum order 24 (socks 48), packs of 6 (socks 12)")
    say("  Pacific Caps: lead time 30 days (deviation 5), minimum order 60, packs of 12")
    say("  unit costs: read from the store's own cost field (tees 8.00, cap 6.00, socks 3.50)")
    say()

    params = PlanParams()
    async with db.tenant(tenant) as conn:
        await store.make_plan(conn, tenant, today, params)
        rows = await store.plan_lines(conn, today)
    say("Plan for %s (recent window %d days, prior window %d days, service level %.0f percent, review %d days):" % (today, params.recent_days, params.prior_days, params.service_level * 100, params.review_days))
    say(table([r for r in rows if (r["sku"] or "").startswith("IPT-")]))
    others = [r for r in rows if not (r["sku"] or "").startswith("IPT-")]
    sold = ", ".join("%s %d units" % (r["product_title"], r["units_all"]) for r in others if r["units_all"]) or "none"
    say("  and %d other variants of the store (its own sample products, no supplier set, sales: %s)" % (len(others), sold))
    tee = next(r for r in rows if r["sku"] == "IPT-TEE-M")
    say()
    say("Worked on the best seller IPT-TEE-M: %d units in the recent window over %d counted days is %.2f a day (standard deviation %.2f); %d on the shelf is %.1f days of cover against a 21 day lead time;"
        % (tee["units_recent"], tee["counted_days"], tee["velocity"], tee["velocity_sd"], tee["available"], tee["cover_days"]))
    say("  safety stock %d, reorder point %d, suggested order %d (minimum 24, packs of 6)." % (tee["safety_stock"], tee["reorder_point"], tee["suggested_qty"]))
    say("  History note: the store was set up today, so there is one stock snapshot, and the days before it are unknown, counted as on sale (%d of %d days observed). The stockout adjustment is shown in PART 2." % (
        tee["window_days"] - tee["unknown_days"], tee["window_days"]))
    say()

    say("Promotion input: the ads team plans a 1.5 factor on IPT-TEE-M from %s to %s (entered for the run, not read from anywhere)." % (today + timedelta(days=3), today + timedelta(days=17)))
    async with db.tenant(tenant) as conn:
        await store.add_promo(conn, tenant, ref="IPT-TEE-M", starts_on=today + timedelta(days=3), ends_on=today + timedelta(days=17), factor=1.5, entered_by="run operator", note="entered for the run")
        await store.make_plan(conn, tenant, today, params)
        promo_tee = await conn.fetchrow("select * from inventory_plan_line where sku = 'IPT-TEE-M'")
    say("  IPT-TEE-M with the promotion: forecast cover %.1f days (was %.1f at the normal rate), runs out %s, reorder point %d (was %d), suggested order %d (was %d)." % (
        promo_tee["forecast_cover_days"], tee["cover_days"], promo_tee["stockout_date"], promo_tee["reorder_point"], tee["reorder_point"], promo_tee["suggested_qty"], tee["suggested_qty"]))
    say()

    say("Alerts, raised through the UNIQUE key outbox:")
    for attempt in (1, 2):
        async with db.tenant(tenant) as conn:
            lines = await store.plan_lines(conn, today)
            raised = await alerts.raise_alerts(conn, tenant, today, lines, await store.snapshot_series(conn, today))
        say("  run %d: %d raised, %d already raised" % (attempt, raised.new, raised.already_raised))
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200)

    with psycopg.connect(DSN, autocommit=True) as pg:
        delivery = deliver_pending(pg, alerts.webhook_sink("https://hooks.example/inventory", client=httpx.Client(transport=httpx.MockTransport(handler))), table=alerts.OUTBOX_TABLE)
    say("  delivered %d to a stand-in webhook (a second delivery pass sends none):" % delivery.delivered)
    for text in sent:
        say("    " + text)
    say()

    say("Purchase order drafts: the page and the export read the stored plan; see the browser run in README.md and docs/inventory-planning.md.")
    say("  lines that would be ordered now: " + ", ".join("%s x %d (%s)" % (r["sku"], r["suggested_qty"], r["supplier_name"]) for r in await _orderable(db, tenant, today)))
    say()


async def _orderable(db: Database, tenant: str, today):
    async with db.tenant(tenant) as conn:
        return await conn.fetch("select sku, suggested_qty, supplier_name from inventory_plan_line where needs_reorder and suggested_qty > 0 and status = 'ok' order by supplier_name, sku")


def vs(n: int, product: str, sku: str, available: int) -> VariantStock:
    return VariantStock("gid://shopify/ProductVariant/%d" % n, "gid://shopify/InventoryItem/%d" % n, "gid://shopify/Product/%d" % n, product, "Default Title", sku, True, 800, "USD",
                        (LocationLevel("gid://shopify/Location/1", "Main", available, available, 0, 0),))


async def constructed_part(db: Database) -> None:
    today = datetime.now(timezone.utc).date()
    window = date_range(today - timedelta(days=28), today)
    say("PART 2: the stockout adjustment on a CONSTRUCTED stock history (not the store: a store set up today has no history; these snapshots were written by the same code a daily sync uses)")
    say("A variant sells 2 units on every day it is on the shelf. Its stock history: on the shelf days 1 to 10, none on days 11 to 18, back on days 19 to 28.")
    tenant = make_tenant("constructed-history", "Constructed history")
    sales = [OrderLine("o%d" % d, "l%d" % d, "gid://shopify/ProductVariant/1", None, window[d - 1], 2, 5000, "USD") for d in range(1, 29) if not 11 <= d <= 18]
    async with db.tenant(tenant) as conn:
        ids = await store.upsert_variants(conn, tenant, [vs(1, "Constructed Tee", "CON-TEE", 10)])
        for d in range(1, 29):
            level = 5 if d <= 10 else 0 if d <= 18 else 20
            await store.save_snapshots(conn, tenant, window[d - 1], [vs(1, "Constructed Tee", "CON-TEE", level)], ids)
        await store.save_snapshots(conn, tenant, today, [vs(1, "Constructed Tee", "CON-TEE", 10)], ids)
        await store.replace_order_lines(conn, tenant, today - timedelta(days=120), today, sales, ids)
        await store.upsert_supplier(conn, tenant, name="Constructed Supplier", lead_time_days=9)
        await store.assign_supplier(conn, "CON-TEE", "Constructed Supplier", moq=50, pack_size=12)
        await store.make_plan(conn, tenant, today)
        line = await conn.fetchrow("select * from inventory_plan_line where sku = 'CON-TEE'")
    say("  units sold in the 28 day window: %d. Days in the window: 28, out of stock: %d, counted: %d." % (line["units_recent"], line["out_days"], line["counted_days"]))
    say("  stockout adjusted velocity: %.2f a day.  Units over the whole window instead: %.2f a day, which would say demand fell by %.0f percent." % (
        line["velocity"], line["units_recent"] / 28, 100 * (1 - (line["units_recent"] / 28) / line["velocity"])))
    say("  10 on the shelf is %.1f days of cover against a 9 day lead time; reorder point %d, suggested order %d (minimum 50, packs of 12)." % (line["cover_days"], line["reorder_point"], line["suggested_qty"]))
    async with db.tenant(tenant) as conn:
        raised = await alerts.raise_alerts(conn, tenant, today, await store.plan_lines(conn, today), await store.snapshot_series(conn, today))
        kinds = [(r["kind"], r["event_ref"]) for r in await conn.fetch("select kind, event_ref from inventory_alert")]
    say("  alert raised: %s (the stock cycle that began at the restock on %s)." % (kinds[0][0], kinds[0][1]) if raised.new else "  no alert")
    say()


async def main_async(args) -> None:
    build_schema()
    tenant = make_tenant("dev-store-inventory", "Dev store inventory")
    keys = make_keys(tenant)
    if args.keys_out:
        Path(args.keys_out).write_text(json.dumps({"tenant": tenant, **keys}), encoding="utf-8")
    db = Database(DSN, max_size=4)
    await db.connect()
    try:
        await live_part(db, tenant)
        await constructed_part(db)
    finally:
        await db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--keys-out", type=Path, default=None, help="write the tenant id and the two API keys here, for the browser run (the file is a secret)")
    args = ap.parse_args()
    started = datetime.now(timezone.utc)
    say("Inventory planning run, %s UTC. Local: %s, Python %s, Postgres from the compose db service (the request role, forced row level security)." % (
        started.strftime("%Y-%m-%d %H:%M"), platform.platform(), platform.python_version()))
    say()
    asyncio.run(main_async(args))
    args.out.write_text("\n".join(OUT) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
