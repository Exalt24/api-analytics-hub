"""Purchase order tracking and the plan workbook as HTTP calls: list, make, move, receive and read orders, run the late check, and download the workbook of the stored plan.

WHO MAY DO WHAT, by the key's role (app/api/auth.py): every role reads (`read:inventory`); an operator or admin changes anything (`write:inventory`). The tenant comes from the key and nowhere
else. An order id that belongs to another tenant answers 404 with exactly the body of an id that does not exist, because row level security shows the other tenant's order to no one.

STATUSES. 200 done. 201 an order made. 401 and 403 as everywhere. 404 no such order, line, supplier or stored plan. 409 a refusal of the rules: an illegal status move, a receipt on an
order that is not open, more received than ordered without `allow_over`, nothing to order or a draft already open. 422 a body that cannot be stored (a quantity of zero, an unknown field).
The refusal text is the order rules' own (app/inventory/orders.py, and the database raises the same codes).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import Principal, requires
from app.inventory import alerts, order_store, orders, store, workbook

router = APIRouter()

_STATUS_OF: dict[type[orders.OrderError], int] = {
    orders.IllegalMove: 409, orders.NotAllowedInStatus: 409, orders.OverReceipt: 409, orders.NothingToOrder: 409, orders.BadQuantity: 422, orders.NotFound: 404,
}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def refuse(exc: orders.OrderError) -> HTTPException:
    return HTTPException(status_code=_STATUS_OF.get(type(exc), 400), detail=str(exc))


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _order_id(value: str) -> str:
    """A malformed id is the same 404 as an id nobody has, so the route never feeds the database a string it would answer with a 500."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(status_code=404, detail="no such purchase order") from None


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateOrder(Strict):
    supplier: str = Field(min_length=1, max_length=120)


class MoveBody(Strict):
    to: str = Field(max_length=30)
    on: date | None = None
    expected_on: date | None = None
    note: str | None = Field(default=None, max_length=500)


class ExpectedBody(Strict):
    expected_on: date


class ReceiptLine(Strict):
    line_id: int
    quantity: int


class ReceiptBody(Strict):
    lines: list[ReceiptLine] = Field(min_length=1, max_length=200)
    allow_over: bool = False
    on: date | None = None


def _who(principal: Principal) -> str:
    return "api key %s" % principal.key_id[:8]


@router.get("/api/inventory/orders")
async def list_orders(request: Request, status_filter: str | None = Query(default=None, alias="status"), principal: Principal = Depends(requires("read:inventory"))):
    """The tenant's orders with their lines. `?status=open` lists the placed and unfinished ones; any status name lists that one."""
    wanted = list(orders.OPEN_STATUSES) if status_filter == "open" else [status_filter] if status_filter else None
    if wanted and any(s not in orders.STATUSES for s in wanted):
        raise HTTPException(status_code=422, detail="status must be open or one of %s" % ", ".join(orders.STATUSES))
    async with request.app.state.db.tenant(principal.tenant_id) as conn:
        return {"as_of": _today(), "orders": await order_store.list_orders(conn, statuses=wanted, as_of=_today())}


@router.post("/api/inventory/orders", status_code=status.HTTP_201_CREATED)
async def create_order(body: CreateOrder, request: Request, principal: Principal = Depends(requires("write:inventory"))):
    """A draft order for one supplier from the latest stored plan: the lines, quantities and costs are the plan's."""
    try:
        async with request.app.state.db.tenant(principal.tenant_id) as conn:
            oid = await order_store.create_from_plan(conn, body.supplier, _who(principal))
            return await order_store.get_order(conn, oid, as_of=_today())
    except orders.OrderError as exc:
        raise refuse(exc) from None


@router.get("/api/inventory/orders/{order_id}")
async def get_order(order_id: str, request: Request, principal: Principal = Depends(requires("read:inventory"))):
    try:
        async with request.app.state.db.tenant(principal.tenant_id) as conn:
            return await order_store.get_order(conn, _order_id(order_id), as_of=_today())
    except orders.OrderError as exc:
        raise refuse(exc) from None


@router.post("/api/inventory/orders/{order_id}/status")
async def move_order(order_id: str, body: MoveBody, request: Request, principal: Principal = Depends(requires("write:inventory"))):
    """Move an order to another status. Placing it (`ordered`) sets the expected date from the supplier's lead time unless `expected_on` is given."""
    try:
        async with request.app.state.db.tenant(principal.tenant_id) as conn:
            await order_store.move(conn, _order_id(order_id), body.to, _who(principal), on=body.on, expected_on=body.expected_on, note=body.note)
            return await order_store.get_order(conn, order_id, as_of=_today())
    except orders.OrderError as exc:
        raise refuse(exc) from None


@router.post("/api/inventory/orders/{order_id}/expected")
async def set_expected(order_id: str, body: ExpectedBody, request: Request, principal: Principal = Depends(requires("write:inventory"))):
    try:
        async with request.app.state.db.tenant(principal.tenant_id) as conn:
            await order_store.set_expected(conn, _order_id(order_id), body.expected_on, _who(principal))
            return await order_store.get_order(conn, order_id, as_of=_today())
    except orders.OrderError as exc:
        raise refuse(exc) from None


@router.post("/api/inventory/orders/{order_id}/receipts")
async def receive(order_id: str, body: ReceiptBody, request: Request, principal: Principal = Depends(requires("write:inventory"))):
    """Record units received on one or more lines, all or none. More than ordered is refused (409) unless `allow_over` is true, and then the line is flagged."""
    try:
        async with request.app.state.db.tenant(principal.tenant_id) as conn:
            await order_store.receive(conn, _order_id(order_id), [(l.line_id, l.quantity) for l in body.lines], _who(principal), allow_over=body.allow_over, on=body.on)
            return await order_store.get_order(conn, order_id, as_of=_today())
    except orders.OrderError as exc:
        raise refuse(exc) from None


@router.post("/api/inventory/late-check")
async def late_check(request: Request, principal: Principal = Depends(requires("write:inventory"))):
    """Raise the late alert for each open order past its expected date, once each. Safe to call as often as a scheduler likes: an order already reported is counted, not raised again."""
    async with request.app.state.db.tenant(principal.tenant_id) as conn:
        raised = await order_store.raise_late_alerts(conn, principal.tenant_id, _today())
    return {"as_of": _today(), "raised": raised.new, "already_raised": raised.already_raised, "kind": alerts.LATE_KIND}


@router.get("/api/inventory/workbook")
async def plan_workbook(request: Request, as_of: date | None = None, service_level: float | None = Query(default=None, ge=0.5, lt=1), review_days: int | None = Query(default=None, ge=0, le=365),
                        promo_factor: float | None = Query(default=None, gt=0, le=20), promo_from: date | None = None, promo_to: date | None = None,
                        principal: Principal = Depends(requires("read:inventory"))):
    """The stored plan as an Excel workbook with live formulas. The promotion cells default to the largest promotion entered for all variants; pass `promo_factor`, `promo_from` and
    `promo_to` to start from another."""
    try:
        async with request.app.state.db.tenant(principal.tenant_id) as conn:
            data = await store.workbook_inputs(conn, as_of, service_level=service_level, review_days=review_days, promo_factor=promo_factor, promo_from=promo_from, promo_to=promo_to)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    try:
        content = workbook.build_workbook(data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return Response(content=content, media_type=XLSX, headers={"Content-Disposition": 'attachment; filename="%s"' % workbook.filename(data.as_of)})
