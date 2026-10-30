"""Proration for a plan change in the middle of a billing period, in integer minor units with one written rounding rule.

THE RULE.
  remaining  = whole seconds from the change to the end of the period, clamped to 0..the whole period (a change before the period began counts as the
               whole period, one after it ended as none).
  credit     = the unused part of the OLD plan:  old_price * remaining / period,  rounded HALF UP to a whole minor unit.
  charge     = the rest of the period on the NEW plan:  new_price * remaining / period,  rounded HALF UP.
  net        = charge - credit. Each line is rounded on its own and the net is the difference of the rounded lines, never rounded separately, so the
               lines a customer sees always add up to the amount they are asked to pay. Positive net is an upgrade to bill, negative a credit to give.
Half up on a non-negative number n / d is (2n + d) // (2d), done in integers: no float, no Decimal, no rounding mode to misread.

Stripe's page on prorations (docs/vendor-contract/stripe/billing-subscriptions-prorations.txt) gives one worked example, and this reproduces it: "If a
customer upgrades from a 10 USD monthly plan to a 20 USD option halfway through the billing period, the customer is billed an additional 5 USD:",
and its table rows "Unused time on original 10 USD plan (credit)" at -5 USD and "Remaining time on new 20 USD plan (debit)" at +10 USD, total +5 USD. The rounding rule above is this
module's own: the saved page does not state how Stripe rounds, and nothing here claims to match a live invoice to the cent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Proration:
    credit_minor: int          # the unused part of the old plan, as a positive number
    charge_minor: int          # the rest of the period on the new plan
    net_minor: int             # charge - credit
    remaining_seconds: int
    period_seconds: int


def half_up(numerator: int, denominator: int) -> int:
    """numerator / denominator rounded half up, for a non-negative numerator and a positive denominator, in integers."""
    if numerator < 0 or denominator <= 0:
        raise ValueError("half_up takes a non-negative numerator and a positive denominator")
    return (2 * numerator + denominator) // (2 * denominator)


def prorate(old_price_minor: int, new_price_minor: int, period_start: datetime, period_end: datetime, change_at: datetime) -> Proration:
    if old_price_minor < 0 or new_price_minor < 0:
        raise ValueError("prices are not negative")
    for name, moment in (("period_start", period_start), ("period_end", period_end), ("change_at", change_at)):
        if moment.tzinfo is None:
            raise ValueError("%s must carry a time zone" % name)
    period = int((period_end - period_start).total_seconds())
    if period <= 0:
        raise ValueError("the period must end after it starts")
    remaining = min(max(int((period_end - change_at).total_seconds()), 0), period)
    credit = half_up(old_price_minor * remaining, period)
    charge = half_up(new_price_minor * remaining, period)
    return Proration(credit, charge, charge - credit, remaining, period)
