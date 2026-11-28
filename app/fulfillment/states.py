"""The order state machine, as a table, and the one function that decides what to do with a partner's status event.

STATES. created (ours, not yet sent), submitted (the partner has it), accepted, packed, shipped (carrier and tracking number required), delivered, and the two ends that are not delivery:
rejected (the partner refused it) and cancelled. delivered, rejected and cancelled end the order.

THE TABLE is TRANSITIONS below and the rows of `fulfillment_transition` in migration 024; a test fails if they differ, and the database refuses any other move with a trigger, so the rule holds
for this code, for a hand written UPDATE and for any other writer alike.

WHAT TO DO WITH AN EVENT (`decide`). The partner may send events twice, late or out of order, so a status event meets the order's CURRENT status and is one of three things:
  apply   the move is in the table: do it.
  ignore  the event says nothing new or says something older (a duplicate of the current status, an `accepted` after `shipped`, a late `submitted` after `cancelled`): record that it was seen
          and change nothing, so the state never moves backwards. Answered 200, so the partner stops sending it.
  refuse  the event is not legal NOW (a `shipped` before `packed`, a `cancelled` after `delivered`, a `shipped` for a cancelled order): change nothing, audit it, answer 409. A `shipped` that
          overtook its `packed` is therefore refused, the partner retries it, and it is applied once `packed` has arrived: nothing is skipped and nothing is lost.
"""
from __future__ import annotations

STATES = ("created", "submitted", "accepted", "packed", "shipped", "delivered", "rejected", "cancelled")
#: statuses a partner may report (it never reports `created`)
PARTNER_STATUSES = ("submitted", "accepted", "packed", "shipped", "delivered", "rejected", "cancelled")
TERMINAL = ("delivered", "rejected", "cancelled")

TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"submitted", "cancelled"}),
    "submitted": frozenset({"accepted", "rejected", "cancelled"}),
    "accepted": frozenset({"packed", "cancelled"}),
    "packed": frozenset({"shipped"}),
    "shipped": frozenset({"delivered"}),
}

#: how far along the delivery road each status is; rejected and cancelled are off the road
_RANK = {"created": 0, "submitted": 1, "accepted": 2, "packed": 3, "shipped": 4, "delivered": 5}


def can_move(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def decide(current: str, target: str) -> str:
    """apply, ignore or refuse, for a partner event naming `target` against an order that is `current`. Unknown statuses are refused, never applied."""
    if current not in STATES or target not in PARTNER_STATUSES:
        return "refuse"
    if target == current:
        return "ignore"
    if can_move(current, target):
        return "apply"
    if current in ("rejected", "cancelled"):
        return "ignore" if target in ("submitted", "accepted") else "refuse"
    if target in _RANK and _RANK[target] < _RANK[current]:
        return "ignore"
    return "refuse"
