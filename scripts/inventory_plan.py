#!/usr/bin/env python
# LIFESPAN: permanent
"""Inventory planning from the command line: read the shop's stock, set suppliers and promotions, work out the plan, raise and deliver alerts.

    DATABASE_URL=postgresql://... INVENTORY_TENANT=<tenant uuid> python scripts/inventory_plan.py <command>

    sync [--days N]                         read every variant's stock and the last N days of orders (default 120) from Shopify, store today's snapshots
    supplier --name N --lead-days D [--lead-sd S] [--note T]      add or change a supplier and its lead time (a figure a person enters)
    assign --variant SKU_OR_ID --supplier N [--moq M] [--pack P] [--cost AMOUNT --currency USD]   name a variant's supplier, minimum order and pack size
    promo --from D --to D --factor F --by WHO [--variant SKU_OR_ID] [--note T]   a dated promotion factor (no --variant means every variant)
    plan [--as-of D] [--json]               work the plan out from what is stored, keep it, print it
    alerts [--as-of D] [--deliver] [--slack-env VAR]   raise each shortage once from the stored plan; --deliver pushes the pending ones to the webhook named by the variable
    digest [--as-of D] [--state FILE] [--repeat-seconds N]   the shortages and late orders still open, worded once and again only when the set changes or the repeat window passes (app.health.decide)

    order-create --supplier N [--by WHO]            a draft purchase order for a supplier from the stored plan (its lines, quantities and costs)
    orders [--open] [--status S]                    the purchase orders with their lines, what is still to arrive and whether each is late
    order-move --order ID --to STATUS [--on D] [--expected D] [--by WHO]   place (ordered), confirm, ship or cancel an order; placing sets the expected date from the lead time
    order-expect --order ID --date D [--by WHO]     change the expected arrival date of an open order
    order-receive --order ID --line SKU_OR_ID --qty N [--allow-over] [--on D] [--by WHO]   record units received on a line (more than ordered needs --allow-over)
    workbook --out FILE [--as-of D] [--promo-factor F --promo-from D --promo-to D] [--service-level S] [--review-days N]   the stored plan as an Excel workbook with live formulas

SHOPIFY. `sync` reads the development store whose token file SHOPIFY_DEV_TOKEN_FILE names (or .secrets/shopify_dev_token.txt): SHOP=<store>.myshopify.com and ACCESS_TOKEN=<token>.
It only reads. The Slack style webhook URL is a secret read from the environment and never printed.

Exit 0: done. Exit 2: refused or failed, with the reason on stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import psycopg  # noqa: E402

from app.changes import deliver_pending  # noqa: E402
from app.connectors.base import ConnectorError  # noqa: E402
from app.connectors.shopify import ShopifyConnector  # noqa: E402
from app.db import Database, UnsafeDatabaseRole  # noqa: E402
from app.health import decide, load_state, save_state  # noqa: E402
from app.inventory import alerts, order_store, orders, store, workbook  # noqa: E402
from app.inventory.model import PlanParams  # noqa: E402
from app.inventory.reader import ShopifyInventoryReader  # noqa: E402
from app.inventory.sync import ORDER_WINDOW_DAYS, sync_tenant  # noqa: E402
from app.secret_files import DEV_TOKEN  # noqa: E402


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD") from None


def _today() -> date:
    return datetime.now(timezone.utc).date()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", default=os.environ.get("INVENTORY_TENANT"), help="tenant uuid (or INVENTORY_TENANT)")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--days", type=int, default=ORDER_WINDOW_DAYS)
    s = sub.add_parser("supplier")
    s.add_argument("--name", required=True)
    s.add_argument("--lead-days", type=int, required=True)
    s.add_argument("--lead-sd", type=float, default=0.0)
    s.add_argument("--note")
    s = sub.add_parser("assign")
    s.add_argument("--variant", required=True)
    s.add_argument("--supplier", required=True)
    s.add_argument("--moq", type=int)
    s.add_argument("--pack", type=int)
    s.add_argument("--cost", help="unit cost as a decimal amount, such as 12.50")
    s.add_argument("--currency")
    s = sub.add_parser("promo")
    s.add_argument("--from", dest="starts", type=_date, required=True)
    s.add_argument("--to", dest="ends", type=_date, required=True)
    s.add_argument("--factor", type=float, required=True)
    s.add_argument("--by", required=True)
    s.add_argument("--variant")
    s.add_argument("--note")
    s = sub.add_parser("plan")
    s.add_argument("--as-of", type=_date, default=None)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("order-create")
    s.add_argument("--supplier", required=True)
    s.add_argument("--by", default="command line")
    s = sub.add_parser("orders")
    s.add_argument("--open", action="store_true")
    s.add_argument("--status", choices=orders.STATUSES)
    s = sub.add_parser("order-move")
    s.add_argument("--order", required=True)
    s.add_argument("--to", required=True, choices=orders.STATUSES)
    s.add_argument("--on", type=_date)
    s.add_argument("--expected", type=_date)
    s.add_argument("--by", default="command line")
    s = sub.add_parser("order-expect")
    s.add_argument("--order", required=True)
    s.add_argument("--date", type=_date, required=True)
    s.add_argument("--by", default="command line")
    s = sub.add_parser("order-receive")
    s.add_argument("--order", required=True)
    s.add_argument("--line", required=True, help="a line id or its SKU")
    s.add_argument("--qty", type=int, required=True)
    s.add_argument("--allow-over", action="store_true")
    s.add_argument("--on", type=_date)
    s.add_argument("--by", default="command line")
    s = sub.add_parser("workbook")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--as-of", type=_date, default=None)
    s.add_argument("--promo-factor", type=float)
    s.add_argument("--promo-from", type=_date)
    s.add_argument("--promo-to", type=_date)
    s.add_argument("--service-level", type=float)
    s.add_argument("--review-days", type=int)
    s = sub.add_parser("alerts")
    s.add_argument("--as-of", type=_date, default=None)
    s.add_argument("--deliver", action="store_true")
    s.add_argument("--slack-env", default="SLACK_WEBHOOK_URL")
    s = sub.add_parser("digest")
    s.add_argument("--as-of", type=_date, default=None)
    s.add_argument("--state")
    s.add_argument("--repeat-seconds", type=int, default=6 * 3600)
    return p


def shopify_reader() -> ShopifyInventoryReader:
    text = DEV_TOKEN.read()
    shop, token = re.search(r"^SHOP=(.+)$", text, re.M), re.search(r"^ACCESS_TOKEN=(.+)$", text, re.M)
    if not shop or not token:
        raise SystemExit("the Shopify token file needs a SHOP= line and an ACCESS_TOKEN= line")
    return ShopifyInventoryReader(ShopifyConnector(shop_domain=shop.group(1).strip(), access_token=token.group(1).strip(), page_size=100))


def minor_from_amount(text: str, currency: str) -> int:
    exponent = 0 if currency.upper() in ("JPY", "KRW", "VND", "CLP") else 2
    return int((Decimal(text) * (10 ** exponent)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def table(rows) -> str:
    head = ("product / variant", "sku", "avail", "vel/day", "cover", "lead", "ROP", "SS", "order", "class", "label")
    out = [head]
    for r in rows:
        name = r["product_title"] + ("" if r["variant_title"] in ("", "Default Title") else " - " + r["variant_title"])
        out.append((name[:42], r["sku"] or "-", str(r["available"]), "%.2f" % r["velocity"], "-" if r["cover_days"] is None else "%.1f" % r["cover_days"],
                    "-" if r["lead_time_days"] is None else str(r["lead_time_days"]), "-" if r["reorder_point"] is None else str(r["reorder_point"]),
                    "-" if r["safety_stock"] is None else str(r["safety_stock"]), str(r["suggested_qty"]), r["abc_class"], r["label"]))
    widths = [max(len(row[i]) for row in out) for i in range(len(head))]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in out)


async def run(args, db: Database) -> int:
    tenant = args.tenant
    if args.command == "sync":
        result = await sync_tenant(db, tenant, shopify_reader(), today=_today(), order_days=args.days)
        print("variants %d, snapshots written %d for %s, order lines stored %d (from %d orders), Shopify calls %d"
              % (result.variants, result.snapshots, result.snapshot_day, result.order_lines, result.orders_read, result.api_calls))
        return 0
    if args.command == "supplier":
        async with db.tenant(tenant) as conn:
            sid = await store.upsert_supplier(conn, tenant, name=args.name, lead_time_days=args.lead_days, lead_time_sd_days=args.lead_sd, note=args.note)
        print("supplier %s (%s): lead time %d days, deviation %.1f" % (args.name, sid[:8], args.lead_days, args.lead_sd))
        return 0
    if args.command == "assign":
        if (args.cost is None) != (args.currency is None):
            raise SystemExit("give --cost and --currency together")
        cost = minor_from_amount(args.cost, args.currency) if args.cost else None
        async with db.tenant(tenant) as conn:
            vid = await store.assign_supplier(conn, args.variant, args.supplier, moq=args.moq, pack_size=args.pack, unit_cost_minor=cost, currency=args.currency)
        print("variant %s now ordered from %s" % (vid[:8], args.supplier))
        return 0
    if args.command == "promo":
        async with db.tenant(tenant) as conn:
            pid = await store.add_promo(conn, tenant, ref=args.variant, starts_on=args.starts, ends_on=args.ends, factor=args.factor, entered_by=args.by, note=args.note)
        print("promotion %s: factor %.2f from %s to %s for %s" % (pid[:8], args.factor, args.starts, args.ends, args.variant or "every variant"))
        return 0
    if args.command in ORDER_COMMANDS:
        return await run_orders(args, db)
    as_of = args.as_of or _today()
    if args.command == "workbook":
        async with db.tenant(tenant) as conn:
            data = await store.workbook_inputs(conn, args.as_of, service_level=args.service_level, review_days=args.review_days, promo_factor=args.promo_factor,
                                               promo_from=args.promo_from, promo_to=args.promo_to)
        args.out.write_bytes(workbook.build_workbook(data))
        print("wrote %s: plan of %s, %d variants, %d suppliers, promotion factor %.2f from %s to %s (formulas on Plan, Classes and Scenario)" % (
            args.out, data.as_of, len(data.variants), len(data.suppliers), data.promo_factor, data.promo_start, data.promo_end))
        return 0
    if args.command == "plan":
        async with db.tenant(tenant) as conn:
            await store.make_plan(conn, tenant, as_of, PlanParams())
            rows = await store.plan_lines(conn, as_of)
        print(json.dumps([{k: (v.isoformat() if isinstance(v, date) else str(v) if k.endswith("id") else v) for k, v in dict(r).items()} for r in rows], indent=1) if args.json else table(rows))
        return 0
    async with db.tenant(tenant) as conn:
        lines = await store.plan_lines(conn, as_of)
        if not lines:
            raise SystemExit("no stored plan for %s: run `plan` first" % as_of)
        if args.command == "alerts":
            raised = await alerts.raise_alerts(conn, tenant, as_of, lines, await store.snapshot_series(conn, as_of))
            print("alerts raised %d, already raised %d" % (raised.new, raised.already_raised))
            late = await order_store.raise_late_alerts(conn, tenant, as_of)
            print("late orders raised %d, already raised %d" % (late.new, late.already_raised))
    if args.command == "alerts" and args.deliver:
        url = os.environ.get(args.slack_env)
        if not url:
            raise SystemExit("%s is not set, so nothing was delivered" % args.slack_env)
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as pg:
            sink = alerts.webhook_sink(url)
            for label, table in (("shortages", alerts.OUTBOX_TABLE), ("late orders", alerts.LATE_OUTBOX_TABLE)):
                result = deliver_pending(pg, sink, table=table)
                print("%s delivered %d, retrying %d, throttled %d, dead %d" % (label, result.delivered, result.retrying, result.throttled, result.dead))
    if args.command == "digest":
        shortages = alerts.open_shortages(lines)
        async with db.tenant(tenant) as conn:
            shortages = shortages + await order_store.open_late_orders(conn, as_of)
        state = load_state(args.state) if args.state else {}
        message, new_state = decide(state, shortages, time.time(), args.repeat_seconds, describer=alerts.describe_shortages)
        print(message if message else "(quiet: nothing changed inside the repeat window)")
        if args.state:
            save_state(args.state, new_state)
        return 1 if shortages else 0
    return 0


ORDER_COMMANDS = ("order-create", "orders", "order-move", "order-expect", "order-receive")


def order_table(rows) -> str:
    head = ("order", "supplier", "status", "ordered", "expected", "open units", "late", "lines")
    out = [head]
    for o in rows:
        out.append((o["id"][:8], o["supplier"], o["status"], str(o["ordered_on"] or "-"), str(o["expected_on"] or "-"), str(o["open_units"]), "LATE" if o["late"] else "-",
                    ", ".join("%s %d/%d" % (l["sku"] or l["description"], l["quantity_received"], l["quantity_ordered"]) for l in o["lines"])))
    widths = [max(len(row[i]) for row in out) for i in range(len(head))]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in out)


async def run_orders(args, db: Database) -> int:
    """The purchase order commands. An order is named by its full id or the first eight characters `orders` prints."""
    tenant, today = args.tenant, _today()
    if args.command == "order-create":
        async with db.tenant(tenant) as conn:
            oid = await order_store.create_from_plan(conn, args.supplier, args.by)
            o = await order_store.get_order(conn, oid, as_of=today)
        print("draft %s for %s from the plan of %s: %s" % (oid, args.supplier, o["plan_as_of"], ", ".join("%d x %s" % (l["quantity_ordered"], l["sku"] or l["description"]) for l in o["lines"])))
        return 0
    async with db.tenant(tenant) as conn:
        if args.command == "orders":
            wanted = list(orders.OPEN_STATUSES) if args.open else [args.status] if args.status else None
            print(order_table(await order_store.list_orders(conn, statuses=wanted, as_of=today)))
            return 0
        oid = await _resolve_order(conn, args.order)
        if args.command == "order-move":
            status = await order_store.move(conn, oid, args.to, args.by, on=args.on, expected_on=args.expected)
            o = await order_store.get_order(conn, oid, as_of=today)
            print("order %s is now %s, expected %s" % (oid[:8], status, o["expected_on"] or "no date"))
        elif args.command == "order-expect":
            print("order %s is now expected %s" % (oid[:8], await order_store.set_expected(conn, oid, args.date, args.by)))
        else:
            line = await order_store.find_line(conn, oid, args.line)
            status = await order_store.receive(conn, oid, [(line, args.qty)], args.by, allow_over=args.allow_over, on=args.on)
            print("received %d on line %d: order %s is now %s" % (args.qty, line, oid[:8], status))
    return 0


async def _resolve_order(conn, ref: str) -> str:
    """A full order id, or the unique prefix of one (the form `orders` prints)."""
    rows = await conn.fetch("select id from inventory_purchase_order where id::text like $1 || '%'", ref.lower())
    if len(rows) != 1:
        raise orders.NotFound("%d orders match %r" % (len(rows), ref))
    return str(rows[0]["id"])


def main() -> int:
    args = parser().parse_args()
    if not args.tenant:
        print("give --tenant or set INVENTORY_TENANT", file=sys.stderr)
        return 2

    async def go() -> int:
        db = Database(max_size=3)
        await db.connect()
        try:
            return await run(args, db)
        finally:
            await db.close()

    try:
        return asyncio.run(go())
    except (UnsafeDatabaseRole, ConnectorError, LookupError, ValueError, httpx.HTTPError, orders.OrderError) as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
