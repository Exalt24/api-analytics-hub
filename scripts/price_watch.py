#!/usr/bin/env python
# LIFESPAN: permanent
"""Competitor price watch from the command line: scan, report, review, approve, sync.

    DATABASE_URL=postgresql://... PRICE_WATCH_TENANT=<tenant uuid> python scripts/price_watch.py <command>

    scan                          read every watched competitor page in Chromium, store the prices, match the pages to the catalogue
    report [--date D] [--out-dir DIR]   build the day's report (stored, and written as .md and .csv), queue proposals for the lower prices
    proposals [--state S]         list the review queue
    approve --id ID --actor WHO   a person approves a proposal (the first 8 characters of its id are enough)
    reject  --id ID --actor WHO [--note TEXT]
    listings [--status S]         the watched pages and how each matched (needs_review are the ones left for a person)
    confirm-match --listing ID --product SKU --actor WHO     settle a match that was left for a person (a listing id prefix, a SKU or a product id)
    sync [--id ID] [--apply] [--fakes]   send approved proposals to Shopify and Zoho Inventory. A dry run unless --apply.
    demo-server [--port 8791]     serve the invented competitor shop that seed_price_watch_demo.py points at

SYNC AND THE VENDORS. `--fakes` talks to in-process fake Shopify and Zoho servers (nothing real is contacted); without it, --apply uses
SHOPIFY_SHOP_DOMAIN and SHOPIFY_ADMIN_TOKEN, and ZOHO_REGION, ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN and ZOHO_ORGANIZATION_ID from the
environment. That live path follows the vendors' official pages and has never been run against a real account.

Exit 0: done. Exit 2: refused or failed, with the reason on stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
import httpx  # noqa: E402

from app.db import Database, UnsafeDatabaseRole  # noqa: E402
from app.price_watch import proposals, store  # noqa: E402
from app.price_watch.adapters import ShopifyPriceAdapter, ZohoInventoryAdapter, ZohoTokenSource  # noqa: E402
from app.price_watch.fake_site import FakeSite  # noqa: E402
from app.price_watch.fakes import FakeShopify, FakeZoho  # noqa: E402
from app.price_watch.money import format_minor  # noqa: E402
from app.price_watch.scraper import BrowserUnavailable, PageScraper  # noqa: E402
from app.price_watch.sync import NoSyncTarget, NotApproved, StaleProposal, sync_proposal  # noqa: E402


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", default=os.environ.get("PRICE_WATCH_TENANT"), help="tenant uuid (or PRICE_WATCH_TENANT)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("scan")
    r = sub.add_parser("report")
    r.add_argument("--date", type=_date, default=None)
    r.add_argument("--out-dir", default=None)
    r.add_argument("--no-proposals", action="store_true")
    r.add_argument("--undercut-minor", type=int, default=0, help="go this many minor units below the competitor's landed price (default 0: match it)")
    ls = sub.add_parser("proposals")
    ls.add_argument("--state", choices=sorted(proposals.TRANSITIONS), default=None)
    for name in ("approve", "reject"):
        d = sub.add_parser(name)
        d.add_argument("--id", required=True)
        d.add_argument("--actor", required=True)
        d.add_argument("--note", default=None)
    lg = sub.add_parser("listings")
    lg.add_argument("--status", choices=["unmatched", "matched", "confirmed", "needs_review", "no_match"], default=None)
    c = sub.add_parser("confirm-match")
    c.add_argument("--listing", required=True)
    c.add_argument("--product", required=True)
    c.add_argument("--actor", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--id", default=None, help="one proposal; default every approved or failed one")
    s.add_argument("--apply", action="store_true", help="really send; without it nothing is sent and nothing changes")
    s.add_argument("--fakes", action="store_true", help="use the in-process fake Shopify and Zoho servers")
    s.add_argument("--actor", default="price-watch sync")
    d = sub.add_parser("demo-server")
    d.add_argument("--port", type=int, default=8791)
    return p


async def resolve_id(db: Database, tenant: str, prefix: str) -> str:
    async with db.tenant(tenant) as conn:
        rows = await conn.fetch("select id from price_change_proposal where id::text like $1 || '%'", prefix.lower())
    if len(rows) != 1:
        raise LookupError("%d proposals start with %r" % (len(rows), prefix))
    return str(rows[0]["id"])


async def resolve_listing_and_product(db: Database, tenant: str, listing: str, product: str) -> tuple[str, str]:
    async with db.tenant(tenant) as conn:
        listings = await conn.fetch("select id from competitor_listing where id::text like $1 || '%'", listing.lower())
        products = await conn.fetch("select id from catalogue_product where sku = $1 or id::text = $1", product)
    if len(listings) != 1:
        raise LookupError("%d listings start with %r" % (len(listings), listing))
    if len(products) != 1:
        raise LookupError("no catalogue product with the SKU or id %r" % product)
    return str(listings[0]["id"]), str(products[0]["id"])


def build_adapters(args: argparse.Namespace) -> tuple[dict, list[httpx.AsyncClient], str]:
    clients: list[httpx.AsyncClient] = []
    use_fakes = args.fakes or (not args.apply and not os.environ.get("SHOPIFY_SHOP_DOMAIN"))
    if use_fakes:
        shop, zoho = FakeShopify(), FakeZoho()
        sc, zc = shop.client(), zoho.client()
        clients += [sc, zc]
        tokens = ZohoTokenSource(zc, "https://" + zoho.accounts_host, zoho.client_id, zoho.client_secret, zoho.refresh_token)
        adapters = {"shopify": ShopifyPriceAdapter(sc, shop.shop_domain, shop.token),
                    "zoho": ZohoInventoryAdapter(zc, tokens, zoho.org, "https://" + zoho.api_host)}
        return adapters, clients, "FAKE vendors (in process, nothing real is contacted)"
    client = httpx.AsyncClient(timeout=20)
    clients.append(client)
    return ({"shopify": ShopifyPriceAdapter.from_env(client), "zoho": ZohoInventoryAdapter.from_env(client)}, clients,
            "LIVE vendors from the environment (this path has never been run against a real account)")


async def run(args: argparse.Namespace) -> int:
    if args.command == "demo-server":
        with FakeSite(args.port) as site:
            print("invented competitor shop on %s (pages at /p/<name>); Ctrl-C to stop" % site.base)
            try:
                while True:
                    await asyncio.sleep(3600)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
        return 0
    if not args.tenant:
        print("no tenant: pass --tenant or set PRICE_WATCH_TENANT", file=sys.stderr)
        return 2
    try:
        db = Database(max_size=2)
    except RuntimeError as exc:
        print("cannot start: %s" % exc, file=sys.stderr)
        return 2
    try:
        await db.connect()
    except UnsafeDatabaseRole as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, asyncpg.PostgresError) as exc:
        print("cannot reach the database: %s" % exc, file=sys.stderr)
        return 2
    try:
        return await dispatch(db, args)
    except (proposals.InvalidTransition, proposals.ProposalNotFound, NotApproved, StaleProposal, NoSyncTarget, LookupError, ValueError, BrowserUnavailable) as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    finally:
        await db.close()


async def dispatch(db: Database, args: argparse.Namespace) -> int:
    tenant = args.tenant
    if args.command == "scan":
        async with PageScraper() as scraper:
            results = await store.scan_tenant(db, tenant, scraper, date.today())
        for r in results:
            price = "%s %s" % (r["currency"], format_minor(r["price_minor"], r["currency"])) if r["price_minor"] is not None else "no price"
            print("%-26s %-48s %-14s match: %s (%s)" % (r["competitor"], r["url"], price, r["match"]["status"], r["match"]["reason"]))
            for problem in r["problems"]:
                print("    note:", problem)
        return 0
    if args.command == "report":
        day = args.date or date.today()
        async with db.tenant(tenant) as conn:
            report, created = await store.make_report(conn, tenant, day, undercut_minor=args.undercut_minor, create_proposals=not args.no_proposals)
            row = await conn.fetchrow("select markdown, csv from price_watch_report where report_date = $1", day)
        print(row["markdown"])
        print("%d new proposal(s) queued" % len(created))
        if args.out_dir:
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / ("price-report-%s.md" % day)).write_text(row["markdown"], encoding="utf-8")
            (out / ("price-report-%s.csv" % day)).write_text(row["csv"], encoding="utf-8", newline="")
            print("wrote", out / ("price-report-%s.md" % day), "and the .csv")
        return 0
    if args.command == "proposals":
        async with db.tenant(tenant) as conn:
            rows = await proposals.list_proposals(conn, args.state)
        for r in rows:
            cur = r["currency"].strip()
            print("%s  %-9s %-10s %s -> %s  (%s landed %s, %s)  shopify:%s zoho:%s" % (
                str(r["id"])[:8], r["state"], r["sku"], format_minor(r["current_price_minor"], cur), format_minor(r["proposed_price_minor"], cur),
                r["competitor"], format_minor(r["competitor_landed_minor"], cur), r["shipping_source"].upper() if r["shipping_source"] == "estimated" else r["shipping_source"],
                r["shopify_status"], r["zoho_status"]))
        if not rows:
            print("no proposals")
        return 0
    if args.command in ("approve", "reject"):
        pid = await resolve_id(db, tenant, args.id)
        fn = proposals.approve if args.command == "approve" else proposals.reject
        async with db.tenant(tenant) as conn:
            row = await fn(conn, tenant, pid, args.actor, args.note)
        print("%s is now %s" % (pid, row["state"]))
        return 0
    if args.command == "listings":
        async with db.tenant(tenant) as conn:
            rows = await conn.fetch(
                """select l.id, l.match_status, l.competitor, l.url, l.match_reason, p.sku from competitor_listing l
                     left join catalogue_product p on p.id = l.catalogue_product_id
                    where ($1::text is null or l.match_status = $1) order by l.competitor, l.url""", args.status)
        for r in rows:
            print("%s  %-12s %-26s %s  %s%s" % (str(r["id"])[:8], r["match_status"], r["competitor"], r["url"], ("-> %s  " % r["sku"]) if r["sku"] else "", r["match_reason"] or ""))
        if not rows:
            print("no listings")
        return 0
    if args.command == "confirm-match":
        listing_id, product_id = await resolve_listing_and_product(db, tenant, args.listing, args.product)
        async with db.tenant(tenant) as conn:
            await store.confirm_match(conn, tenant, listing_id, product_id, args.actor)
        print("listing confirmed")
        return 0
    if args.command == "sync":
        adapters, clients, banner = build_adapters(args)
        print(banner)
        try:
            if args.id:
                ids = [await resolve_id(db, tenant, args.id)]
            else:
                async with db.tenant(tenant) as conn:
                    ids = [str(r["id"]) for r in await proposals.list_proposals(conn) if r["state"] in ("approved", "failed")]
            failed = 0
            for pid in ids:
                out = await sync_proposal(db, tenant, pid, adapters, apply=args.apply, actor=args.actor)
                mode = "dry run" if out.dry_run else "applied" if out.state == "applied" else "FAILED"
                print("%s  %s  %s  shopify:%s zoho:%s" % (pid[:8], out.state, mode, out.sides["shopify"], out.sides["zoho"]))
                for step in out.plan:
                    print("    would send:", step)
                for side, msg in out.messages.items():
                    print("    %s: %s" % (side, msg))
                failed += out.state == "failed" and not out.dry_run
            if not ids:
                print("nothing approved to sync")
            return 2 if failed else 0
        finally:
            for c in clients:
                await c.aclose()
    return 2


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
