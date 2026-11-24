"""The purchase order rules as pure functions: which status moves are legal, what a receipt does to a line and to the order, which orders are open inbound stock, and when an order is late.
No database and no network, so every rule is tested alone and by seeded random worlds (tests/test_inventory_orders_model.py). The same rules are enforced again in the database by
migrations/021_purchase_orders.sql (a trigger holds the transition table, constraints hold the received quantity); tests/test_inventory_orders_db.py checks the two agree move by move.

STATUSES. draft, ordered, confirmed, shipped, partly_received, received, cancelled. A move to partly_received or received is made by recording a receipt, never by hand. A received or
cancelled order never moves again. Nothing is shipped and cancelled: goods in transit are not cancelled here.

INBOUND STOCK. An order counts as inbound while it is open (ordered, confirmed, shipped or partly received), by the quantity of each line still to arrive, never below zero. A draft is not
inbound (nothing was placed) and a cancelled order is not (its remainder will not arrive). The plan reads this instead of Shopify's own `incoming` figure for a variant that has an open
order, and never adds the two, because the same goods can be in both.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Sequence

DRAFT = "draft"
ORDERED = "ordered"
CONFIRMED = "confirmed"
SHIPPED = "shipped"
PARTLY_RECEIVED = "partly_received"
RECEIVED = "received"
CANCELLED = "cancelled"

STATUSES = (DRAFT, ORDERED, CONFIRMED, SHIPPED, PARTLY_RECEIVED, RECEIVED, CANCELLED)
#: Placed and not finished: the statuses whose remaining quantity is inbound stock and whose expected date can be edited.
OPEN_STATUSES = (ORDERED, CONFIRMED, SHIPPED, PARTLY_RECEIVED)
TERMINAL_STATUSES = (RECEIVED, CANCELLED)
#: A person moves an order to none of these: a receipt does.
RECEIPT_ONLY = (PARTLY_RECEIVED, RECEIVED)

#: The legal moves, from -> the statuses it may go to. The database table inventory_po_transition holds the same rows.
TRANSITIONS: Mapping[str, frozenset[str]] = {
    DRAFT: frozenset({ORDERED, CANCELLED}),
    ORDERED: frozenset({CONFIRMED, SHIPPED, PARTLY_RECEIVED, RECEIVED, CANCELLED}),
    CONFIRMED: frozenset({SHIPPED, PARTLY_RECEIVED, RECEIVED, CANCELLED}),
    SHIPPED: frozenset({PARTLY_RECEIVED, RECEIVED}),
    PARTLY_RECEIVED: frozenset({RECEIVED, CANCELLED}),
    RECEIVED: frozenset(),
    CANCELLED: frozenset(),
}


class OrderError(Exception):
    """A refused purchase order operation. `code` is the SQLSTATE the database raises for the same refusal, so both layers name a refusal the same way."""

    code = "PO000"


class IllegalMove(OrderError):
    code = "PO001"


class NotAllowedInStatus(OrderError):
    code = "PO003"


class OverReceipt(OrderError):
    code = "PO004"


class NothingToOrder(OrderError):
    code = "PO005"


class BadQuantity(OrderError):
    code = "PO006"


class NotFound(OrderError):
    code = "PO404"


ERROR_BY_CODE: Mapping[str, type[OrderError]] = {cls.code: cls for cls in (IllegalMove, NotAllowedInStatus, OverReceipt, NothingToOrder, BadQuantity, NotFound)}


def can_move(from_status: str, to_status: str) -> bool:
    return to_status in TRANSITIONS.get(from_status, frozenset())


def check_move(from_status: str, to_status: str) -> None:
    """Raise IllegalMove for a move the table does not allow, and for the two moves only a receipt makes."""
    if from_status not in TRANSITIONS or to_status not in TRANSITIONS:
        raise IllegalMove("unknown status in %r to %r" % (from_status, to_status))
    if to_status in RECEIPT_ONLY:
        raise IllegalMove("record a receipt instead: a receipt moves an order to partly received or received")
    if not can_move(from_status, to_status):
        raise IllegalMove("a purchase order cannot move from %s to %s" % (from_status, to_status))


def next_statuses(status: str) -> list[str]:
    """The moves a person can make from `status`, in status order: the legal ones minus the two a receipt makes."""
    return [s for s in STATUSES if can_move(status, s) and s not in RECEIPT_ONLY]


def open_quantity(ordered: int, received: int) -> int:
    """Units of a line still to arrive. Never negative: an over-receipt leaves nothing open, not a negative amount."""
    return max(ordered - received, 0)


def receive_line(ordered: int, received: int, quantity: int, allow_over: bool = False) -> tuple[int, bool]:
    """The line's received total after `quantity` more arrive, and whether it is now over-received. More than ordered is refused unless `allow_over`."""
    if quantity <= 0:
        raise BadQuantity("a received quantity must be above zero")
    total = received + quantity
    if total > ordered and not allow_over:
        raise OverReceipt("receiving %d would bring the line to %d of %d ordered: say to accept more than was ordered" % (quantity, total, ordered))
    return total, total > ordered


def status_after_receipt(current: str, lines: Sequence[tuple[int, int]]) -> str:
    """The order's status once its lines hold these (ordered, received) pairs: received when every line is in full, otherwise partly received. Only an open order can receive."""
    if current not in OPEN_STATUSES:
        raise NotAllowedInStatus("nothing can be received on a %s order" % current)
    if lines and all(received >= ordered for ordered, received in lines):
        return RECEIVED
    return PARTLY_RECEIVED


def expected_from_lead(ordered_on: date, lead_time_days: int | None) -> date | None:
    """The arrival date an order is first expected on: the day it was placed plus the supplier's lead time then. None when the supplier has no lead time."""
    return ordered_on + timedelta(days=lead_time_days) if lead_time_days is not None else None


def is_late(status: str, expected_on: date | None, open_units: int, as_of: date) -> bool:
    """An open order with something still to arrive whose expected date is before `as_of`. An order with no expected date cannot be late."""
    return status in OPEN_STATUSES and expected_on is not None and expected_on < as_of and open_units > 0


@dataclass(frozen=True)
class Arrival:
    """Units of one variant expected on one date, from one open order. `expected_on` is None when the order has no date."""

    quantity: int
    expected_on: date | None


def arrivals_by_variant(rows: Sequence[tuple[str, str, int, int, date | None]]) -> dict[str, list[Arrival]]:
    """Group the open stock on order by variant. Each row is (variant, order status, quantity ordered, quantity received, expected date). Orders that are not open and lines with nothing
    left to arrive are left out, so a draft, a cancelled order and a received line are never inbound."""
    out: dict[str, list[Arrival]] = {}
    for variant, status, ordered, received, expected_on in rows:
        left = open_quantity(ordered, received)
        if status in OPEN_STATUSES and left > 0:
            out.setdefault(variant, []).append(Arrival(left, expected_on))
    return out
