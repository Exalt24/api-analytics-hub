"""Database side of price watch: the catalogue, the watched listings, scans, the rate table, and the stored daily report.

Every function takes an asyncpg connection from `Database.tenant(...)` (so row level security scopes it) except `scan_tenant`,
which opens its own short transactions: the browser work in the middle of a scan must not hold a database transaction open.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

import asyncpg

from app.db import Database

from . import proposals
from .matching import CatalogueItem, CompetitorItem, match_listing
from .report import Observation, ProductRow, Report, ReviewItem, build_report, render_csv, render_markdown
from .scraper import PageScraper, ScrapedProduct
from .shipping import RateBand


async def add_product(conn: asyncpg.Connection, tenant_id: str, *, sku: str, title: str, price_minor: int, currency: str, floor_minor: int,
                      gtin: str | None = None, brand: str | None = None, our_shipping_minor: int = 0, weight_grams: int | None = None,
                      shopify_product_gid: str | None = None, shopify_variant_gid: str | None = None,
                      zoho_item_id: str | None = None, zoho_item_name: str | None = None) -> str:
    row = await conn.fetchrow(
        """insert into catalogue_product (tenant_id, sku, gtin, brand, title, price_minor, currency, floor_minor, our_shipping_minor, weight_grams,
                                          shopify_product_gid, shopify_variant_gid, zoho_item_id, zoho_item_name)
           values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
           on conflict (tenant_id, sku) do update set gtin = excluded.gtin, brand = excluded.brand, title = excluded.title,
               price_minor = excluded.price_minor, currency = excluded.currency, floor_minor = excluded.floor_minor,
               our_shipping_minor = excluded.our_shipping_minor, weight_grams = excluded.weight_grams,
               shopify_product_gid = excluded.shopify_product_gid, shopify_variant_gid = excluded.shopify_variant_gid,
               zoho_item_id = excluded.zoho_item_id, zoho_item_name = excluded.zoho_item_name
           returning id""",
        tenant_id, sku, gtin, brand, title, price_minor, currency, floor_minor, our_shipping_minor, weight_grams,
        shopify_product_gid, shopify_variant_gid, zoho_item_id, zoho_item_name)
    return str(row["id"])


async def add_listing(conn: asyncpg.Connection, tenant_id: str, *, competitor: str, url: str, ship_zone: str = "domestic") -> str:
    row = await conn.fetchrow(
        """insert into competitor_listing (tenant_id, competitor, url, ship_zone) values ($1::uuid, $2, $3, $4)
           on conflict (tenant_id, url) do update set competitor = excluded.competitor, ship_zone = excluded.ship_zone returning id""",
        tenant_id, competitor, url, ship_zone)
    return str(row["id"])


async def add_rate(conn: asyncpg.Connection, tenant_id: str, *, zone: str, weight_from_g: int, weight_to_g: int, cost_minor: int, currency: str) -> None:
    await conn.execute(
        """insert into shipping_rate (tenant_id, destination_zone, weight_from_g, weight_to_g, cost_minor, currency)
           values ($1::uuid, $2, $3, $4, $5, $6)
           on conflict (tenant_id, destination_zone, currency, weight_from_g) do update set weight_to_g = excluded.weight_to_g, cost_minor = excluded.cost_minor""",
        tenant_id, zone, weight_from_g, weight_to_g, cost_minor, currency)


async def load_bands(conn: asyncpg.Connection) -> list[RateBand]:
    rows = await conn.fetch("select destination_zone, weight_from_g, weight_to_g, cost_minor, currency from shipping_rate")
    return [RateBand(r["destination_zone"], r["weight_from_g"], r["weight_to_g"], r["cost_minor"], r["currency"].strip()) for r in rows]


async def load_products(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch("select * from catalogue_product order by sku")


# ------------------------------------------------------------------------------------------------------- scanning

async def record_scan(conn: asyncpg.Connection, tenant_id: str, listing_id: str, scraped: ScrapedProduct, day: date) -> dict[str, Any]:
    """Store one scan of one page and re-run the match (a person's confirmed match is left alone). Returns the match outcome."""
    problem = "; ".join(scraped.problems) or None
    await conn.execute(
        """insert into competitor_price_observation
               (tenant_id, listing_id, observed_on, price_minor, original_price_minor, currency, availability, shipping_minor, shipping_text, problem)
           values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10)
           on conflict (listing_id, observed_on) do update set observed_at = now(), price_minor = excluded.price_minor,
               original_price_minor = excluded.original_price_minor, currency = excluded.currency, availability = excluded.availability,
               shipping_minor = excluded.shipping_minor, shipping_text = excluded.shipping_text, problem = excluded.problem""",
        tenant_id, listing_id, day, scraped.price_minor, scraped.original_price_minor, scraped.currency, scraped.availability,
        scraped.shipping_minor, scraped.shipping_text, problem)
    listing = await conn.fetchrow("select match_status from competitor_listing where id = $1::uuid", listing_id)
    await conn.execute(
        "update competitor_listing set seen_title = $2, seen_sku = $3, seen_gtin = $4, last_scanned_at = now() where id = $1::uuid",
        listing_id, scraped.title, scraped.sku, scraped.gtin)
    if listing is not None and listing["match_status"] == "confirmed":
        return {"status": "confirmed", "reason": "confirmed by a person"}
    if not scraped.title and not scraped.sku and not scraped.gtin:
        return {"status": "unchanged", "reason": "page gave nothing to match on"}
    catalogue = [CatalogueItem(str(r["id"]), r["sku"], r["title"], r["gtin"], r["brand"]) for r in await load_products(conn)]
    result = match_listing(CompetitorItem(scraped.title, scraped.sku, scraped.gtin), catalogue)
    candidates = ", ".join("%s %.2f" % (next((c.sku for c in catalogue if c.id == cid), cid), sc) for cid, sc in result.candidates)
    reason = result.reason + (" [candidates: %s]" % candidates if candidates and result.status != "matched" else "")
    await conn.execute(
        """update competitor_listing set match_status = $2, catalogue_product_id = $3::uuid, match_method = $4, match_score = $5, match_reason = $6
            where id = $1::uuid""",
        listing_id, result.status, result.catalogue_id if result.status == "matched" else None, result.method, result.score, reason)
    return {"status": result.status, "reason": reason, "score": result.score}


async def scan_tenant(db: Database, tenant_id: str, scraper: PageScraper, day: date) -> list[dict[str, Any]]:
    """Scrape every watched listing once and store what was seen. A page that fails is recorded as a problem, and the scan goes on."""
    async with db.tenant(tenant_id) as conn:
        listings = await conn.fetch("select id, competitor, url from competitor_listing order by competitor, url")
    expected = await _expected_currency(db, tenant_id)
    results = []
    for l in listings:
        scraped = await scraper.scrape(l["url"], assume_currency=expected)
        async with db.tenant(tenant_id) as conn:
            outcome = await record_scan(conn, tenant_id, str(l["id"]), scraped, day)
        results.append({"competitor": l["competitor"], "url": l["url"], "price_minor": scraped.price_minor, "currency": scraped.currency,
                        "problems": list(scraped.problems), "match": outcome})
    return results


async def _expected_currency(db: Database, tenant_id: str) -> str | None:
    """A bare '$' on a competitor page is read as the currency our own catalogue uses, when the catalogue uses just one."""
    async with db.tenant(tenant_id) as conn:
        rows = await conn.fetch("select distinct currency from catalogue_product")
    return rows[0]["currency"].strip() if len(rows) == 1 else None


async def confirm_match(conn: asyncpg.Connection, tenant_id: str, listing_id: str, product_id: str, actor: str) -> None:
    """A person's decision on a needs_review listing. It is kept across later scans."""
    if not actor.strip():
        raise ValueError("an actor is required")
    updated = await conn.fetchval(
        """update competitor_listing set match_status = 'confirmed', catalogue_product_id = $2::uuid, match_method = 'manual', match_score = 1,
               match_reason = $3 where id = $1::uuid returning id""", listing_id, product_id, "confirmed by %s" % actor.strip())
    if updated is None:
        raise LookupError("no such listing")


# ------------------------------------------------------------------------------------------------------- the report

async def make_report(conn: asyncpg.Connection, tenant_id: str, day: date, *, undercut_minor: int = 0, create_proposals: bool = True) -> tuple[Report, list[str]]:
    """Build the day's report from what the scans stored, keep it (one row for a tenant and day; running it again replaces that row),
    and queue a proposal for each product that has a lower competitor. Returns the report and the new proposal ids."""
    products = [ProductRow(str(r["id"]), r["sku"], r["title"], r["price_minor"], r["currency"].strip(), r["floor_minor"], r["our_shipping_minor"], r["weight_grams"])
                for r in await load_products(conn)]
    obs_rows = await conn.fetch(
        """select l.id as listing_id, l.catalogue_product_id as product_id, l.competitor, l.url, l.ship_zone,
                  o.observed_on, o.price_minor, o.currency, o.availability, o.shipping_minor, o.problem
             from competitor_listing l
             join lateral (select * from competitor_price_observation x where x.listing_id = l.id and x.observed_on <= $1
                            order by x.observed_on desc limit 1) o on true
            where l.match_status in ('matched', 'confirmed') and l.catalogue_product_id is not null""", day)
    observations = [Observation(str(r["listing_id"]), str(r["product_id"]), r["competitor"], r["url"], r["ship_zone"], r["observed_on"], r["price_minor"],
                                r["currency"].strip() if r["currency"] else None, r["availability"], r["shipping_minor"], r["problem"]) for r in obs_rows]
    review = [ReviewItem(r["competitor"], r["url"], r["match_reason"] or "needs review")
              for r in await conn.fetch("select competitor, url, match_reason from competitor_listing where match_status = 'needs_review' order by competitor, url")]
    report = build_report(day, products, observations, await load_bands(conn), review, undercut_minor=undercut_minor)
    summary = report.summary()
    await conn.execute(
        """insert into price_watch_report (tenant_id, report_date, lines, needs_review, summary, markdown, csv)
           values ($1::uuid, $2, $3, $4, $5::jsonb, $6, $7)
           on conflict (tenant_id, report_date) do update set lines = excluded.lines, needs_review = excluded.needs_review,
               summary = excluded.summary, markdown = excluded.markdown, csv = excluded.csv, created_at = now()""",
        tenant_id, day, summary["lower"], summary["needs_review"], json.dumps(summary), render_markdown(report), render_csv(report))
    created: list[str] = []
    if create_proposals:
        for line in report.best_by_product().values():
            pid = await proposals.create_proposal(conn, tenant_id, line, day)
            if pid:
                created.append(pid)
    return report, created
