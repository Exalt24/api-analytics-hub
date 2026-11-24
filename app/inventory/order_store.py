"""Database side of purchase order tracking: make an order from the plan's draft for a supplier, move it through its statuses, change its expected date, record what arrived, read
orders back, and raise the late alert once.

The rules live in the database (migrations/021_purchase_orders.sql): a trigger holds the legal moves, constraints hold the received quantity, and four SQL functions are the
operations, so this module, the web page and a hand written statement are held to the same rules. A refusal comes back as the exception in app/inventory/orders.py that carries the
same code. Every function takes an asyncpg connection from `Database.tenant(...)`, so row level security scopes it and the tenant id is never a parameter here.

THE LATE ALERT. An open order whose expected date is before the plan date, with something still to arrive, is late. `raise_late_alerts` inserts one `inventory_po_alert` for it behind
UNIQUE (tenant, order, kind) with `on conflict do nothing`, and an outbox row for the new ones, the pattern of the shortage alerts. Running it daily, twice a day or after a restart
cannot raise a second alert for the same order, and changing the expected date afterwards does not raise another. An order that was reported late and then arrived is not reported again.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Sequence

import asyncpg

from . import alerts, orders
from .alerts import OpenShortage, Raised


async def _call(conn: asyncpg.Connection, sql: str, *args: Any) -> asyncpg.Record:
    """Run one of the database's operations, and turn its refusal into the matching OrderError (the database's code is the same as the exception's)."""
    try:
        row = await conn.fetchrow(sql, *args)
    except asyncpg.PostgresError as exc:
        cls = orders.ERROR_BY_CODE.get(getattr(exc, "sqlstate", None) or "")
        if cls is not None:
            raise cls(exc.message) from None
        raise
    assert row is not None
    return row


async def create_from_plan(conn: asyncpg.Connection, supplier: str, by: str) -> str:
    """A draft order for `supplier` from the latest stored plan, with the plan's lines, quantities and costs. Returns the order id."""
    row = await _call(conn, "select (inventory_po_create_from_plan($1, $2)).id as id", supplier, by)
    return str(row["id"])


async def move(conn: asyncpg.Connection, order_id: str, to_status: str, by: str, *, on: date | None = None, expected_on: date | None = None, note: str | None = None) -> str:
    """Move an order to another status. `on` is the date it happened (today when none), `expected_on` a new expected arrival date (placing an order sets one from the lead time).
    Returns the new status. A move the table does not allow, or one to partly received or received, raises IllegalMove."""
    row = await _call(conn, "select (inventory_po_move($1::uuid, $2, $3, $4, $5, $6)).status as status", order_id, to_status, by, on, expected_on, note)
    return row["status"]


async def set_expected(conn: asyncpg.Connection, order_id: str, expected_on: date, by: str) -> date:
    """Change the expected arrival date of an open order. Refused on a draft, a received order and a cancelled one."""
    row = await _call(conn, "select (inventory_po_set_expected($1::uuid, $2, $3)).expected_on as expected_on", order_id, expected_on, by)
    return row["expected_on"]


async def receive(conn: asyncpg.Connection, order_id: str, receipts: Sequence[tuple[int, int]], by: str, *, allow_over: bool = False, on: date | None = None) -> str:
    """Record units received: `receipts` is (line id, quantity) pairs, applied together (one that is refused leaves the others unrecorded when the caller's transaction rolls back).
    Returns the order's status afterwards: received when every line is in full, otherwise partly received."""
    if not receipts:
        raise orders.BadQuantity("name at least one line and a quantity")
    status = ""
    for line_id, quantity in receipts:
        row = await _call(conn, "select (inventory_po_receive($1::uuid, $2, $3, $4, $5, $6)).status as status", order_id, line_id, quantity, allow_over, by, on)
        status = row["status"]
    return status


async def find_line(conn: asyncpg.Connection, order_id: str, ref: str) -> int:
    """A line of an order by its id or its SKU. None or several matches is an error, never a guess."""
    rows = await conn.fetch("select id from inventory_purchase_order_line where order_id = $1::uuid and (id::text = $2 or sku = $2)", order_id, ref)
    if not rows:
        raise orders.NotFound("no line %r on this order" % ref)
    if len(rows) > 1:
        raise orders.NotFound("%d lines match %r, name one by its id" % (len(rows), ref))
    return int(rows[0]["id"])


def _view(o: asyncpg.Record, lines: Sequence[asyncpg.Record], as_of: date | None) -> dict[str, Any]:
    line_views = [{"id": int(l["id"]), "variant_id": str(l["variant_id"]) if l["variant_id"] else None, "sku": l["sku"], "description": l["description"],
                   "quantity_ordered": l["quantity_ordered"], "quantity_received": l["quantity_received"], "quantity_open": orders.open_quantity(l["quantity_ordered"], l["quantity_received"]),
                   "over_received": l["over_received"], "unit_cost_minor": l["unit_cost_minor"], "currency": l["currency"].strip() if l["currency"] else None} for l in lines]
    open_units = sum(l["quantity_open"] for l in line_views)
    costed = [l for l in line_views if l["unit_cost_minor"] is not None]
    return {
        "id": str(o["id"]), "supplier": o["supplier_name"], "status": o["status"], "currency": o["currency"].strip() if o["currency"] else None,
        "lead_time_days": o["lead_time_days"], "plan_as_of": o["plan_as_of"], "ordered_on": o["ordered_on"], "expected_on": o["expected_on"], "confirmed_on": o["confirmed_on"],
        "shipped_on": o["shipped_on"], "closed_on": o["closed_on"], "note": o["note"], "created_by": o["created_by"], "created_at": o["created_at"],
        "lines": line_views, "open_units": open_units,
        "total_minor": sum(l["unit_cost_minor"] * l["quantity_ordered"] for l in costed) if costed else None,
        "lines_without_cost": len(line_views) - len(costed),
        "late": bool(as_of and orders.is_late(o["status"], o["expected_on"], open_units, as_of)),
        "next_statuses": orders.next_statuses(o["status"]),
        "can_receive": o["status"] in orders.OPEN_STATUSES,
    }


async def list_orders(conn: asyncpg.Connection, *, statuses: Sequence[str] | None = None, as_of: date | None = None) -> list[dict[str, Any]]:
    """The tenant's orders, newest first, each with its lines, its open units, whether it is late on `as_of`, and the moves a person can make from its status."""
    if statuses:
        rows = await conn.fetch("select * from inventory_purchase_order where status = any($1::text[]) order by created_at desc, id", list(statuses))
    else:
        rows = await conn.fetch("select * from inventory_purchase_order order by created_at desc, id")
    lines = await conn.fetch("select * from inventory_purchase_order_line where order_id = any($1::uuid[]) order by order_id, sku, id", [r["id"] for r in rows])
    by_order: dict[Any, list[asyncpg.Record]] = {}
    for l in lines:
        by_order.setdefault(l["order_id"], []).append(l)
    return [_view(o, by_order.get(o["id"], []), as_of) for o in rows]


async def get_order(conn: asyncpg.Connection, order_id: str, *, as_of: date | None = None) -> dict[str, Any]:
    o = await conn.fetchrow("select * from inventory_purchase_order where id = $1::uuid", order_id)
    if o is None:
        raise orders.NotFound("no such purchase order")
    lines = await conn.fetch("select * from inventory_purchase_order_line where order_id = $1::uuid order by sku, id", order_id)
    view = _view(o, lines, as_of)
    view["events"] = [{"kind": e["kind"], "from_status": e["from_status"], "to_status": e["to_status"], "detail": json.loads(e["detail"]) if isinstance(e["detail"], str) else e["detail"],
                       "by": e["by_whom"], "at": e["created_at"]} for e in await conn.fetch("select * from inventory_po_event where order_id = $1::uuid order by id", order_id)]
    return view


# ------------------------------------------------------------------------------------------------------- the late alert

_LATE_ROWS = """select o.id, o.supplier_name, o.status, o.ordered_on, o.expected_on,
                       sum(greatest(l.quantity_ordered - l.quantity_received, 0))::int as open_units,
                       jsonb_agg(jsonb_build_object('sku', l.sku, 'description', l.description, 'open', greatest(l.quantity_ordered - l.quantity_received, 0))
                                 order by l.sku, l.id) filter (where l.quantity_ordered > l.quantity_received) as open_lines
                  from inventory_purchase_order o join inventory_purchase_order_line l on l.order_id = o.id
                 where o.status = any($1::text[]) and o.expected_on is not null and o.expected_on < $2
                 group by o.id having sum(greatest(l.quantity_ordered - l.quantity_received, 0)) > 0
                 order by o.expected_on, o.id"""


async def raise_late_alerts(conn: asyncpg.Connection, tenant_id: str, as_of: date) -> Raised:
    """Raise the late orders of `as_of`, once each. Each new alert gets its outbox row with the payload and the idempotency key the sink will see. Runs in the caller's tenant transaction."""
    new = seen = 0
    for r in await conn.fetch(_LATE_ROWS, list(orders.OPEN_STATUSES), as_of):
        if not orders.is_late(r["status"], r["expected_on"], r["open_units"], as_of):
            continue
        detail = {"order_id": str(r["id"]), "supplier": r["supplier_name"], "status": r["status"], "ordered_on": r["ordered_on"].isoformat() if r["ordered_on"] else None,
                  "expected_on": r["expected_on"].isoformat(), "days_late": (as_of - r["expected_on"]).days, "open_units": r["open_units"],
                  "lines": json.loads(r["open_lines"]) if isinstance(r["open_lines"], str) else r["open_lines"]}
        alert_id = await conn.fetchval(
            """insert into inventory_po_alert (tenant_id, order_id, kind, as_of, detail) values ($1::uuid, $2::uuid, $3, $4, $5::jsonb)
               on conflict on constraint inventory_po_alert_natural_key do nothing returning id""", tenant_id, r["id"], alerts.LATE_KIND, as_of, json.dumps(detail))
        if alert_id is None:
            seen += 1
            continue
        new += 1
        key = "%s:%s:%s" % (tenant_id, r["id"], alerts.LATE_KIND)
        payload = {"alert_id": alert_id, "tenant_id": tenant_id, "kind": alerts.LATE_KIND, "as_of": as_of.isoformat(), "external_key": key, **detail}
        await conn.execute("insert into inventory_po_alert_outbox (event_id, tenant_id, idempotency_key, payload) values ($1, $2::uuid, $3, $4::jsonb)", alert_id, tenant_id, key, json.dumps(payload))
    return Raised(new, seen)


async def open_late_orders(conn: asyncpg.Connection, as_of: date) -> list[OpenShortage]:
    """The late orders still open on `as_of`, shaped for the digest (`app.health.decide` keys a problem by `.key` and alerts again only when the set changes)."""
    out: list[OpenShortage] = []
    for r in await conn.fetch(_LATE_ROWS, list(orders.OPEN_STATUSES), as_of):
        payload = {"supplier": r["supplier_name"], "status": r["status"], "expected_on": r["expected_on"].isoformat(), "days_late": (as_of - r["expected_on"]).days,
                   "open_units": r["open_units"], "kind": alerts.LATE_KIND}
        out.append(OpenShortage(str(r["id"]), alerts.LATE_KIND, "Order to %s" % r["supplier_name"], alerts.describe_late_order(payload)))
    return out
