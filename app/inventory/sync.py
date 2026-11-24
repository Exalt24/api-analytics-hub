"""One inventory sync for one tenant: read the shop, then store the day's snapshots and the order lines in one tenant transaction.

The shop is read first and the database is written second, so a slow or failed Shopify call never holds a transaction open, and a failure leaves the tables as they were. History is
accumulated here: each run adds the day's stock snapshot (Shopify serves only the current quantity), so the days a variant was out of stock can be known later only for the days a
sync ran. The order lines of the window are replaced as a set, so a cancelled or edited order does not linger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from app.db import Database

from . import store
from .reader import ShopifyInventoryReader

#: How far back the order lines are re-read on each run. Longer than the two planning windows together (56 days) so a late edit to an older order still lands.
ORDER_WINDOW_DAYS = 120


@dataclass(frozen=True)
class SyncResult:
    variants: int
    snapshots: int
    order_lines: int
    orders_read: int
    api_calls: int
    snapshot_day: date


async def sync_tenant(db: Database, tenant_id: str, reader: ShopifyInventoryReader, *, today: date, order_days: int = ORDER_WINDOW_DAYS) -> SyncResult:
    """Read variants with their stock and the complete days of orders before `today`, and store them for the tenant."""
    variants = await reader.read_variants()
    window_start, window_end = today - timedelta(days=order_days), today
    lines, orders_read = await reader.read_order_lines(window_start, window_end)
    async with db.tenant(tenant_id) as conn:
        ids = await store.upsert_variants(conn, tenant_id, variants)
        snaps = await store.save_snapshots(conn, tenant_id, today, variants, ids)
        stored = await store.replace_order_lines(conn, tenant_id, window_start, window_end, lines, ids)
    return SyncResult(len(variants), snaps, stored, orders_read, reader.api_calls, today)
