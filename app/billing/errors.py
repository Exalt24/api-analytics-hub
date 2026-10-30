"""The ways a billing command is refused. Each is its own type so a caller (and a test) can tell a refusal from a fault."""
from __future__ import annotations


class BillingError(Exception):
    """Base for every refusal in this package."""


class IdempotencyConflict(BillingError):
    """The same idempotency key came back with different content. Refused loudly, nothing written: reusing a key for another request is a bug in
    the caller (or an attack), and answering it with the first result would hide that."""

    def __init__(self, key: str):
        super().__init__("idempotency key %r was already used for a different request" % key)
        self.key = key


class UnbalancedTransaction(BillingError):
    """The lines of a transaction do not add up to zero (caught here for a clear message; the database refuses it as well)."""


class LedgerInvariantViolation(BillingError):
    """The database itself refused a write that the module should never have attempted (an overdraft, an unbalanced transaction). It means a
    lock or a check in this package did not do its job, so it is raised and never turned into a polite answer."""


class NoUsagePrice(BillingError):
    """The tenant has no price for this billable event in this currency."""


class UnknownPlan(BillingError):
    pass


class NoSubscription(BillingError):
    pass


class WebhookRejected(BillingError):
    """A provider event that failed verification. Never says which check failed to the caller."""

    def __init__(self, reason: str):
        super().__init__("webhook rejected")
        self.reason = reason     # for our own log, never for the sender


class WebhookUnroutable(BillingError):
    """A verified event whose tenant cannot be found yet (a refund that arrived before its payment was recorded). Answered with a non-2xx so the
    provider sends it again later."""


class ReconciliationFailed(BillingError):
    def __init__(self, report):
        super().__init__("the books do not agree: %d discrepancy(ies)" % len(report.discrepancies))
        self.report = report
