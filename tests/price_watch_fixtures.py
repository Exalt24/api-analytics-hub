"""Shared setup for the price watch database tests: a fresh schema, two tenants, a small catalogue and a Database that runs as the request role.

Like the other database suites this DROPS and recreates the `public` schema of the database it points at (ANALYTICS_TEST_DSN, default the
compose Postgres on 5433), so aim it at a scratch database. It applies 001, 002 and 011 only: price watch needs the tenant table, the
request role and its own tables, and not pgvector.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")
DAY = date(2026, 10, 30)

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
pytest.importorskip("asyncpg")


def reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not reachable(), reason="Postgres not reachable, run docker compose up -d")


def run(coro):
    return asyncio.run(coro)


def reset_schema(extra: tuple[str, ...] = ()) -> None:
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade; create schema public")
        for name in ("001_init.sql", "002_control_plane.sql", "011_price_watch.sql", *extra):
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))


def add_tenant(slug: str) -> uuid.UUID:
    tid = uuid.uuid4()
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select set_config('app.tenant_id', %s, false)", (str(tid),))
        cur.execute("insert into tenant (id, slug, name) values (%s, %s, %s)", (tid, slug, slug))
    return tid


async def seed_catalogue(db, tenant: uuid.UUID) -> dict[str, str]:
    """Three products (two wired to Shopify and Zoho, one to Shopify only) and the rate table. Returns sku -> id."""
    from app.price_watch import store
    ids = {}
    async with db.tenant(str(tenant)) as conn:
        ids["KTB-750"] = await store.add_product(
            conn, str(tenant), sku="KTB-750", title="Kestrel Insulated Bottle 750ml", brand="Kestrel", gtin="9300000001016", price_minor=4995,
            currency="AUD", floor_minor=3800, weight_grams=480, shopify_product_gid="gid://shopify/Product/1001",
            shopify_variant_gid="gid://shopify/ProductVariant/2001", zoho_item_id="4815000000010001", zoho_item_name="Kestrel Insulated Bottle 750ml")
        ids["LMP-210"] = await store.add_product(
            conn, str(tenant), sku="LMP-210", title="Lumen Pocket Lamp 210 lumen", brand="Lumen", price_minor=3450, currency="AUD", floor_minor=2200,
            weight_grams=150, shopify_product_gid="gid://shopify/Product/1002", shopify_variant_gid="gid://shopify/ProductVariant/2002")
        ids["HMJ-NVY-M"] = await store.add_product(
            conn, str(tenant), sku="HMJ-NVY-M", title="Harbour Merino Jumper Navy M", brand="Harbour", price_minor=7900, currency="AUD", floor_minor=5500,
            weight_grams=600, shopify_product_gid="gid://shopify/Product/1004", shopify_variant_gid="gid://shopify/ProductVariant/2004",
            zoho_item_id="4815000000010004", zoho_item_name="Harbour Merino Jumper Navy M")
        for lo, hi, cost in ((0, 500, 850), (500, 2000, 1200)):
            await store.add_rate(conn, str(tenant), zone="domestic", weight_from_g=lo, weight_to_g=hi, cost_minor=cost, currency="AUD")
    return ids


async def seed_listing_with_scan(db, tenant: uuid.UUID, product_id: str, *, competitor: str, url: str, price_minor: int | None, shipping_minor: int | None,
                                 currency: str = "AUD", availability: str = "in_stock", day: date = DAY, status: str = "matched") -> str:
    """A matched listing with one observation, written directly (no browser)."""
    from app.price_watch import store
    async with db.tenant(str(tenant)) as conn:
        lid = await store.add_listing(conn, str(tenant), competitor=competitor, url=url)
        await conn.execute("update competitor_listing set match_status = $2, catalogue_product_id = $3::uuid, match_method = 'gtin', match_score = 1 where id = $1::uuid",
                           lid, status, product_id)
        await conn.execute(
            """insert into competitor_price_observation (tenant_id, listing_id, observed_on, price_minor, currency, availability, shipping_minor)
               values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7)""", str(tenant), lid, day, price_minor, currency if price_minor is not None else None, availability, shipping_minor)
    return lid
