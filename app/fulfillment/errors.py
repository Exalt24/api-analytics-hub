"""The ways a fulfilment command is refused. Each is its own type so a caller (and a test) can tell a refusal from a fault."""
from __future__ import annotations


class FulfillmentError(Exception):
    """Base for every refusal in this package."""


class InvalidOrder(FulfillmentError):
    """An order that cannot be stored (no such clinic for this caller)."""


class OrderNotFound(FulfillmentError):
    """No such order for this caller: another tenant's or another clinic's order answers exactly like one that does not exist."""


class OrderStateError(FulfillmentError):
    """The move is not legal from the order's status (cancelling a shipped order, submitting a cancelled one, requeueing an order that is not dead)."""


class PartnerRejected(FulfillmentError):
    """The partner answered with a permanent no (a 4xx that saying again will not change). Never retried; the order goes to the dead letter."""

    def __init__(self, status: int, code: str, attempts: int):
        super().__init__("the partner refused the order with HTTP %d (%s)" % (status, code))
        self.status, self.code, self.attempts = status, code, attempts


class PartnerUnavailable(FulfillmentError):
    """The bounded retries ran out without a usable answer (429, 5xx, a timeout, a dropped connection). `status` is the last HTTP status, or None when nothing answered."""

    def __init__(self, attempts: int, status: int | None):
        super().__init__("the partner did not answer usably after %d attempt(s)" % attempts)
        self.attempts, self.status = attempts, status


class EventRejected(FulfillmentError):
    """A verified partner event that is not a valid status event. The reason is for our own log and is never sent back to the caller."""

    def __init__(self, reason: str):
        super().__init__("event rejected")
        self.reason = reason


class EventUnroutable(FulfillmentError):
    """A verified, valid event for an order reference that is not ours."""
