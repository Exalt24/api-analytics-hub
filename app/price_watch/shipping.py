"""Shipping cost for a competitor's offer: the page's own figure when it states one, otherwise our estimate, and never mixed up.

The competitor's figure and ours are different kinds of fact. A stated cost is something the competitor published. An estimate
is what OUR rate table says a parcel of this weight to this zone costs, which is a guess about what they will charge. So every
result carries a `source` ('stated' or 'estimated' or 'unknown') and a `label` that the report prints beside the number; an
estimate is labelled ESTIMATED wherever it appears, and a proposal made on one says so.

The rate table is the tenant's own (table shipping_rate): destination zone and weight band to a cost in one currency. The band
is `weight_from_g <= weight < weight_to_g`. A product with no weight, a zone with no band for that weight, a band in another
currency, or two bands that both fit are all UNESTIMATABLE, and the report lists that product as excluded rather than
inventing a figure.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

STATED = "stated"
ESTIMATED = "estimated"
UNKNOWN = "unknown"
LABELS = {STATED: "stated by competitor", ESTIMATED: "ESTIMATED", UNKNOWN: "unknown"}


@dataclass(frozen=True)
class RateBand:
    zone: str
    weight_from_g: int
    weight_to_g: int
    cost_minor: int
    currency: str


@dataclass(frozen=True)
class ShippingCost:
    source: str                 # stated, estimated or unknown
    cost_minor: int | None
    currency: str | None
    reason: str = ""

    @property
    def label(self) -> str:
        return LABELS[self.source]


def estimate_shipping(weight_g: int | None, zone: str, currency: str, bands: Sequence[RateBand]) -> ShippingCost:
    if not weight_g or weight_g <= 0:
        return ShippingCost(UNKNOWN, None, None, "no product weight, so no band can be chosen")
    fits = [b for b in bands if b.zone == zone and b.currency == currency and b.weight_from_g <= weight_g < b.weight_to_g]
    if not fits:
        in_zone = [b for b in bands if b.zone == zone]
        why = "no %s band for %d g in zone %r" % (currency, weight_g, zone) if in_zone else "no rate table for zone %r" % zone
        return ShippingCost(UNKNOWN, None, None, why)
    if len(fits) > 1:
        return ShippingCost(UNKNOWN, None, None, "%d bands overlap at %d g in zone %r" % (len(fits), weight_g, zone))
    return ShippingCost(ESTIMATED, fits[0].cost_minor, currency, "band %d to %d g in zone %r" % (fits[0].weight_from_g, fits[0].weight_to_g, zone))


def resolve_shipping(stated_minor: int | None, currency: str | None, weight_g: int | None, zone: str,
                     bands: Sequence[RateBand]) -> ShippingCost:
    """The page's own figure if it gave one (0 is a figure: free shipping), else the estimate, else unknown."""
    if stated_minor is not None and currency:
        return ShippingCost(STATED, stated_minor, currency, "stated on the page")
    if not currency:
        return ShippingCost(UNKNOWN, None, None, "the price has no currency, so a rate cannot be chosen")
    return estimate_shipping(weight_g, zone, currency, bands)
