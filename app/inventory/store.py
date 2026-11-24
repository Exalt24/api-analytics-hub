"""Database side of inventory planning: variants, dated snapshots, order lines, suppliers, promotions and the stored plan.

Every function takes an asyncpg connection from `Database.tenant(...)`, so row level security scopes it and nothing here writes `WHERE tenant_id = ...` for isolation (the tenant id is
passed only to fill the column on an insert, and the policy's WITH CHECK refuses a row for any other tenant). The tables are migrations/020_inventory_planning.sql.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from typing import Sequence

import asyncpg

from . import model
from .model import PlanParams, PlanRow, Promo, VariantInput
from .reader import OrderLine, VariantStock


# ------------------------------------------------------------------------------------------------------- suppliers and variants

async def upsert_supplier(conn: asyncpg.Connection, tenant_id: str, *, name: str, lead_time_days: int, lead_time_sd_days: float = 0.0, note: str | None = None) -> str:
    row = await conn.fetchrow(
        """insert into inventory_supplier (tenant_id, name, lead_time_days, lead_time_sd_days, note) values ($1::uuid, $2, $3, $4, $5)
           on conflict (tenant_id, name) do update set lead_time_days = excluded.lead_time_days, lead_time_sd_days = excluded.lead_time_sd_days, note = excluded.note
           returning id""", tenant_id, name, lead_time_days, lead_time_sd_days, note)
    return str(row["id"])


async def find_variant(conn: asyncpg.Connection, ref: str) -> asyncpg.Record:
    """A variant by SKU or by its Shopify id (the numeric tail or the whole gid). Several matches or none is an error, never a guess."""
    rows = await conn.fetch(
        "select * from inventory_variant where sku = $1 or variant_gid = $1 or variant_gid = 'gid://shopify/ProductVariant/' || $1", ref)
    if not rows:
        raise LookupError("no variant matches %r" % ref)
    if len(rows) > 1:
        raise LookupError("%d variants match %r, name one by its Shopify id" % (len(rows), ref))
    return rows[0]


async def assign_supplier(conn: asyncpg.Connection, ref: str, supplier: str | None, *, moq: int | None = None, pack_size: int | None = None,
                          unit_cost_minor: int | None = None, currency: str | None = None) -> str:
    """Name the supplier of a variant (None clears it) and, when given, its minimum order quantity, pack size and unit cost. Returns the variant id."""
    variant = await find_variant(conn, ref)
    supplier_id = None
    if supplier is not None:
        supplier_id = await conn.fetchval("select id from inventory_supplier where name = $1", supplier)
        if supplier_id is None:
            raise LookupError("no supplier named %r" % supplier)
    await conn.execute(
        """update inventory_variant set supplier_id = $2::uuid, moq = coalesce($3, moq), pack_size = coalesce($4, pack_size),
               unit_cost_minor = coalesce($5, unit_cost_minor), currency = coalesce($6, currency), updated_at = now() where id = $1::uuid""",
        variant["id"], supplier_id, moq, pack_size, unit_cost_minor, currency)
    return str(variant["id"])


async def upsert_variants(conn: asyncpg.Connection, tenant_id: str, variants: Sequence[VariantStock]) -> dict[str, str]:
    """Add or refresh the variants the shop reports. The supplier, minimum order quantity and pack size a person set are never overwritten; a cost the shop reports replaces ours,
    one it does not report leaves ours. Returns variant gid -> variant id."""
    ids: dict[str, str] = {}
    for v in variants:
        row = await conn.fetchrow(
            """insert into inventory_variant (tenant_id, variant_gid, inventory_item_gid, product_gid, product_title, variant_title, sku, tracked, unit_cost_minor, currency)
               values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10)
               on conflict (tenant_id, variant_gid) do update set inventory_item_gid = excluded.inventory_item_gid, product_gid = excluded.product_gid,
                   product_title = excluded.product_title, variant_title = excluded.variant_title, sku = excluded.sku, tracked = excluded.tracked,
                   unit_cost_minor = coalesce(excluded.unit_cost_minor, inventory_variant.unit_cost_minor),
                   currency = case when excluded.unit_cost_minor is null then inventory_variant.currency else excluded.currency end, updated_at = now()
               returning id""",
            tenant_id, v.variant_gid, v.inventory_item_gid, v.product_gid, v.product_title, v.variant_title, v.sku, v.tracked, v.unit_cost_minor, v.currency)
        ids[v.variant_gid] = str(row["id"])
    return ids


# ------------------------------------------------------------------------------------------------------- snapshots and order lines

async def save_snapshots(conn: asyncpg.Connection, tenant_id: str, day: date, variants: Sequence[VariantStock], ids: dict[str, str]) -> int:
    """One row per variant for `day`. A second sync the same day corrects that day's row. Returns how many rows were written."""
    for v in variants:
        levels = [{"location": l.location_gid, "name": l.name, "available": l.available, "on_hand": l.on_hand, "committed": l.committed, "incoming": l.incoming} for l in v.levels]
        await conn.execute(
            """insert into inventory_snapshot (tenant_id, variant_id, observed_on, available, on_hand, committed, incoming, levels)
               values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8::jsonb)
               on conflict (variant_id, observed_on) do update set available = excluded.available, on_hand = excluded.on_hand, committed = excluded.committed,
                   incoming = excluded.incoming, levels = excluded.levels, synced_at = now()""",
            tenant_id, ids[v.variant_gid], day, v.available, v.on_hand, v.committed, v.incoming, json.dumps(levels))
    return len(variants)


async def replace_order_lines(conn: asyncpg.Connection, tenant_id: str, window_start: date, window_end: date, lines: Sequence[OrderLine], ids: dict[str, str]) -> int:
    """Make the stored lines of [window_start, window_end) exactly the lines read: an order cancelled, edited or removed since the last sync leaves no stale line behind."""
    await conn.execute("delete from inventory_order_line where processed_on >= $1 and processed_on < $2", window_start, window_end)
    for ln in lines:
        await conn.execute(
            """insert into inventory_order_line (tenant_id, order_gid, line_gid, variant_id, variant_gid, sku, processed_on, quantity, net_revenue_minor, currency)
               values ($1::uuid, $2, $3, $4::uuid, $5, $6, $7, $8, $9, $10)
               on conflict (tenant_id, line_gid) do update set order_gid = excluded.order_gid, variant_id = excluded.variant_id, variant_gid = excluded.variant_gid,
                   sku = excluded.sku, processed_on = excluded.processed_on, quantity = excluded.quantity, net_revenue_minor = excluded.net_revenue_minor,
                   currency = excluded.currency, synced_at = now()""",
            tenant_id, ln.order_gid, ln.line_gid, ids.get(ln.variant_gid) if ln.variant_gid else None, ln.variant_gid, ln.sku, ln.processed_on, ln.quantity,
            ln.net_revenue_minor, ln.currency)
    return len(lines)


# ------------------------------------------------------------------------------------------------------- promotions

async def add_promo(conn: asyncpg.Connection, tenant_id: str, *, ref: str | None, starts_on: date, ends_on: date, factor: float, entered_by: str, note: str | None = None) -> str:
    """A dated promotion factor for one variant (`ref` a SKU or Shopify id) or for every variant (None). The factor is an input a person enters, never a guess."""
    if not entered_by.strip():
        raise ValueError("a promotion is entered by someone")
    variant_id = str((await find_variant(conn, ref))["id"]) if ref else None
    row = await conn.fetchrow(
        """insert into inventory_promo (tenant_id, variant_id, starts_on, ends_on, factor, note, entered_by) values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7) returning id""",
        tenant_id, variant_id, starts_on, ends_on, factor, note, entered_by.strip())
    return str(row["id"])


# ------------------------------------------------------------------------------------------------------- the plan

async def load_inputs(conn: asyncpg.Connection, as_of: date, params: PlanParams) -> tuple[list[VariantInput], dict[str, asyncpg.Record]]:
    """Everything the plan needs for the tenant the connection is scoped to: each variant with its snapshots, daily sales over the two windows, all-time units and revenue,
    supplier lead time and the promotions that apply. Only complete days before `as_of` count, and the snapshot of `as_of` itself is the stock the plan starts from."""
    window_start = as_of - timedelta(days=params.recent_days + params.prior_days)
    variants = await conn.fetch(
        """select v.*, s.name as supplier_name, s.lead_time_days, s.lead_time_sd_days from inventory_variant v left join inventory_supplier s on s.id = v.supplier_id order by v.product_title, v.variant_title, v.id""")
    snaps: dict[str, dict[date, int]] = defaultdict(dict)
    latest: dict[str, asyncpg.Record] = {}
    for r in await conn.fetch("select variant_id, observed_on, available, incoming from inventory_snapshot where observed_on <= $1 order by observed_on", as_of):
        key = str(r["variant_id"])
        snaps[key][r["observed_on"]] = r["available"]
        latest[key] = r
    sales: dict[str, dict[date, int]] = defaultdict(dict)
    for r in await conn.fetch(
            """select variant_id, processed_on, sum(quantity)::bigint as units from inventory_order_line
                where variant_id is not null and processed_on >= $1 and processed_on < $2 group by variant_id, processed_on""", window_start, as_of):
        sales[str(r["variant_id"])][r["processed_on"]] = int(r["units"])
    totals = {str(r["variant_id"]): r for r in await conn.fetch(
        """select variant_id, sum(quantity)::bigint as units, sum(net_revenue_minor)::bigint as revenue from inventory_order_line
            where variant_id is not null and processed_on < $1 group by variant_id""", as_of)}
    promos: dict[str | None, list[Promo]] = defaultdict(list)
    for r in await conn.fetch("select variant_id, starts_on, ends_on, factor from inventory_promo where ends_on >= $1", as_of):
        promos[str(r["variant_id"]) if r["variant_id"] else None].append(Promo(r["starts_on"], r["ends_on"], r["factor"]))

    inputs: list[VariantInput] = []
    by_key: dict[str, asyncpg.Record] = {}
    for v in variants:
        key = str(v["id"])
        by_key[key] = v
        snap = latest.get(key)
        total = totals.get(key)
        inputs.append(VariantInput(
            key=key, available=int(snap["available"]) if snap else 0, incoming=int(snap["incoming"]) if snap else 0, tracked=v["tracked"], stock_known=snap is not None,
            snapshots=snaps.get(key, {}), sales=sales.get(key, {}), units_all=int(total["units"]) if total else 0, revenue_minor=int(total["revenue"]) if total else 0,
            lead_time_days=v["lead_time_days"], lead_time_sd_days=float(v["lead_time_sd_days"] or 0.0), moq=v["moq"], pack_size=v["pack_size"],
            promos=tuple(promos.get(None, [])) + tuple(promos.get(key, []))))
    return inputs, by_key


async def make_plan(conn: asyncpg.Connection, tenant_id: str, as_of: date, params: PlanParams | None = None) -> tuple[str, list[PlanRow]]:
    """Work the plan out from what is stored, keep it (one run for a tenant and an as-of date; running it again replaces that run) and return its id and rows."""
    params = params or PlanParams()
    inputs, by_key = await load_inputs(conn, as_of, params)
    rows = model.plan_all(inputs, as_of, params)
    items = {i.key: i for i in inputs}
    read_on = {str(r["variant_id"]): r["observed_on"] for r in await conn.fetch(
        "select distinct on (variant_id) variant_id, observed_on from inventory_snapshot where observed_on <= $1 order by variant_id, observed_on desc", as_of)}
    fallback_currency = await _currency(conn)
    await conn.execute("delete from inventory_plan_run where as_of = $1", as_of)
    run_id = await conn.fetchval(
        "insert into inventory_plan_run (tenant_id, as_of, params, variants) values ($1::uuid, $2, $3::jsonb, $4) returning id", tenant_id, as_of, json.dumps(params.as_json()), len(rows))
    for r in rows:
        v = by_key[r.key]
        item = items[r.key]
        await conn.execute(
            """insert into inventory_plan_line (tenant_id, run_id, variant_id, product_title, variant_title, sku, supplier_id, supplier_name, status, available, incoming, velocity,
                   velocity_sd, counted_days, out_days, unknown_days, window_days, units_recent, units_prior, units_all, trend_ratio, revenue_minor, currency, cover_days,
                   forecast_cover_days, stockout_date, promo_factor, lead_time_days, lead_time_sd_days, safety_stock, reorder_point, needs_reorder, suggested_qty,
                   unit_cost_minor, abc_class, label, stock_read_on)
               values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7::uuid, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28,
                       $29, $30, $31, $32, $33, $34, $35, $36, $37)""",
            tenant_id, run_id, r.key, v["product_title"], v["variant_title"], v["sku"], v["supplier_id"], v["supplier_name"], r.status, r.available, r.incoming,
            r.velocity.mean, r.velocity.sd, r.velocity.counted_days, r.velocity.out_days, r.velocity.unknown_days, r.velocity.window_days, r.velocity.units,
            r.prior_velocity.units, item.units_all, r.trend_ratio, item.revenue_minor, v["currency"] or fallback_currency, r.cover_days, r.forecast_cover_days, r.stockout_date,
            r.promo_factor, r.lead_time_days, r.lead_time_sd_days, r.safety_stock, r.reorder_point, r.needs_reorder, r.suggested_qty, v["unit_cost_minor"], r.abc_class, r.label, read_on.get(r.key))
    return str(run_id), rows


async def _currency(conn: asyncpg.Connection) -> str | None:
    """The currency of the order lines (one per tenant: a second would be refused at sync), for a variant that has no cost currency of its own."""
    return await conn.fetchval("select currency from inventory_order_line limit 1")


async def snapshot_series(conn: asyncpg.Connection, as_of: date) -> dict[str, dict[date, int]]:
    """Every variant's available quantity by snapshot day up to `as_of`, for the restock dates the alerts key on."""
    series: dict[str, dict[date, int]] = defaultdict(dict)
    for r in await conn.fetch("select variant_id, observed_on, available from inventory_snapshot where observed_on <= $1 order by observed_on", as_of):
        series[str(r["variant_id"])][r["observed_on"]] = int(r["available"])
    return series


async def plan_lines(conn: asyncpg.Connection, as_of: date | None = None) -> list[asyncpg.Record]:
    """The stored plan lines of a run, in the order a planner reads them: what needs ordering first, then the soonest to run out."""
    if as_of is None:
        as_of = await conn.fetchval("select max(as_of) from inventory_plan_run")
        if as_of is None:
            return []
    return await conn.fetch(
        """select l.*, r.as_of from inventory_plan_line l join inventory_plan_run r on r.id = l.run_id where r.as_of = $1
            order by l.needs_reorder desc, l.cover_days nulls last, l.product_title, l.variant_title""", as_of)
