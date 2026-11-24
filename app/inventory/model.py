"""The inventory planning rules as pure functions: which days a variant was in stock, the daily velocity over those days only, safety stock, reorder point, days of cover,
the order quantity, the ABC class and the label. No database and no network, so every rule is tested alone and by property (tests/test_inventory_model.py).

A STOCKOUT MUST NOT FOOL THE FORECAST. A variant that was out of stock for ten days of a twenty eight day window did not sell slowly, it could not sell. So the velocity is the units
sold divided by the days it was on sale, never by the days in the window. A day is on sale (IN) when a unit sold that day (a sale proves stock) or the snapshot taken that day shows
stock; it is OUT when the snapshot shows none and nothing sold. A day with no snapshot and no sale is UNKNOWN, and a stock history only starts the day the first sync ran, so what an
UNKNOWN day counts as is a named policy: `assume_in` counts it as on sale (absence of a record is not a stockout), `exclude` leaves it out. An OUT day is never counted under either.

WHAT IS NOT ESTIMATED. Seasonality is not modelled (a promotion factor a person enters is the only demand adjustment), the daily demand is treated as independent from day to day,
and the lead time is a figure a person enters for the supplier: nothing here is inferred from a shop that does not hold it.
"""
from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction
from statistics import NormalDist
from typing import Mapping, Sequence

from .orders import Arrival

IN_STOCK = "in"
OUT_OF_STOCK = "out"
UNKNOWN = "unknown"

UNKNOWN_POLICIES = ("assume_in", "exclude")
LABELS = ("best_seller", "rising", "slow_mover", "steady")
ABC = ("A", "B", "C")

#: A day further out than this is not forecast: a cover past it is reported as no stockout in sight.
COVER_CAP_DAYS = 365


def ceil_units(value: float) -> int:
    """Round a unit count up, after rounding off the float dust that makes 20.000000000000004 read as 21."""
    return int(math.ceil(round(value, 9)))


def date_range(start: date, end: date) -> list[date]:
    """The days from `start` up to, and not including, `end`."""
    return [start + timedelta(days=i) for i in range((end - start).days)]


# ------------------------------------------------------------------------------------------------ which days were on sale

def stock_states(days: Sequence[date], snapshots: Mapping[date, int], sales: Mapping[date, int]) -> dict[date, str]:
    """IN, OUT or UNKNOWN for each day.

    A sale that day makes it IN whatever the snapshot says (the last unit can sell and leave zero). Otherwise the snapshot taken that day decides. A day with neither is OUT only when the
    nearest earlier snapshot shows no stock and there is no later snapshot, or the later one shows none either; every other such day is UNKNOWN.
    """
    snapshot_days = sorted(snapshots)
    states: dict[date, str] = {}
    for day in days:
        if sales.get(day, 0) > 0:
            states[day] = IN_STOCK
        elif day in snapshots:
            states[day] = IN_STOCK if snapshots[day] > 0 else OUT_OF_STOCK
        else:
            i = bisect_left(snapshot_days, day)           # no snapshot on `day`, so i is both the first later day and one past the last earlier day
            earlier = snapshots[snapshot_days[i - 1]] if i > 0 else None
            later = snapshots[snapshot_days[i]] if i < len(snapshot_days) else None
            out = earlier is not None and earlier <= 0 and (later is None or later <= 0)
            states[day] = OUT_OF_STOCK if out else UNKNOWN
    return states


@dataclass(frozen=True)
class Velocity:
    """Units a day over the days a variant was on sale, with how many days that was and how sure it is."""

    units: int
    counted_days: int
    in_days: int
    out_days: int
    unknown_days: int
    window_days: int
    mean: float
    variance: float

    @property
    def sd(self) -> float:
        return math.sqrt(self.variance)

    @property
    def stderr(self) -> float:
        """The standard error of the mean: how far the velocity could be from the true rate, from the days counted."""
        return math.sqrt(self.variance / self.counted_days) if self.counted_days > 0 else 0.0

    @property
    def coverage(self) -> float:
        """The share of the window whose stock state was observed (a sale or a snapshot), not assumed."""
        return (self.in_days + self.out_days) / self.window_days if self.window_days else 0.0


def stockout_adjusted_velocity(days: Sequence[date], sales: Mapping[date, int], states: Mapping[date, str], unknown_policy: str = "assume_in") -> Velocity:
    """Units sold per day counted only over the days the variant was on sale, with the sample variance of the daily units over those days."""
    if unknown_policy not in UNKNOWN_POLICIES:
        raise ValueError("unknown_policy must be one of %s" % (UNKNOWN_POLICIES,))
    counted: list[int] = []
    in_days = out_days = unknown_days = 0
    for day in days:
        state = states[day]
        if state == IN_STOCK:
            in_days += 1
        elif state == OUT_OF_STOCK:
            out_days += 1
            continue                                       # never counted: the variant could not sell
        else:
            unknown_days += 1
            if unknown_policy == "exclude":
                continue
        counted.append(sales.get(day, 0))
    n = len(counted)
    units = sum(counted)
    mean = units / n if n else 0.0
    variance = sum((x - mean) ** 2 for x in counted) / (n - 1) if n >= 2 else 0.0
    return Velocity(units, n, in_days, out_days, unknown_days, len(days), mean, variance)


# ------------------------------------------------------------------------------------------------ promotions

@dataclass(frozen=True)
class Promo:
    starts_on: date
    ends_on: date
    factor: float


def promo_factor_on(day: date, promos: Sequence[Promo]) -> float:
    """The demand factor of a day: the largest factor among the promotions that cover it (they are not multiplied together), or 1 when none does."""
    covering = [p.factor for p in promos if p.starts_on <= day <= p.ends_on]
    return max(covering) if covering else 1.0


def demand_over(baseline: float, start: date, horizon_days: int, promos: Sequence[Promo]) -> float:
    """The units expected over `horizon_days` from `start`: the baseline velocity times each day's promotion factor."""
    return sum(baseline * promo_factor_on(start + timedelta(days=i), promos) for i in range(max(horizon_days, 0)))


def projected_cover(available: int, baseline: float, start: date, promos: Sequence[Promo], cap: int = COVER_CAP_DAYS, arrivals: Sequence[Arrival] = ()) -> float | None:
    """Days until the stock runs out when each day sells the baseline times its promotion factor and each open order lands on its expected date. 0 when nothing is left, None when it
    lasts past `cap` days or nothing sells. An arrival is added at the start of its day. One with no date, or a date before `start` (an order already late), never lands."""
    if available <= 0:
        return 0.0
    if baseline <= 0:
        return None
    landing: dict[int, int] = {}
    for a in arrivals:
        if a.expected_on is not None:
            offset = (a.expected_on - start).days          # a day before `start` is never reached by the loop below, so an order already late is not assumed to land
            landing[offset] = landing.get(offset, 0) + a.quantity
    remaining = float(available)
    for i in range(cap):
        remaining += landing.get(i, 0)
        daily = baseline * promo_factor_on(start + timedelta(days=i), promos)
        if remaining <= daily:
            return i + remaining / daily
        remaining -= daily
    return None


def days_of_cover(available: int, velocity: float) -> float | None:
    """The stock on hand divided by the daily velocity; None when nothing sells (an unlimited cover is not a number)."""
    if velocity <= 0:
        return None
    return max(available, 0) / velocity


# ------------------------------------------------------------------------------------------------ safety stock and the reorder point

def z_score(service_level: float) -> float:
    """The normal quantile for a cycle service level, the chance of not stocking out between an order and its arrival."""
    if not 0.5 <= service_level < 1:
        raise ValueError("service level must be at least 0.5 and under 1")
    return NormalDist().inv_cdf(service_level)


def safety_stock(daily_mean: float, daily_sd: float, lead_days: float, lead_sd_days: float = 0.0, service_level: float = 0.95) -> float:
    """z * sqrt(L * sd_d^2 + d^2 * sd_L^2): the stock held against demand varying over the lead time and against a late delivery. Never below zero."""
    if min(daily_mean, daily_sd, lead_days, lead_sd_days) < 0:
        raise ValueError("a mean, a deviation and a lead time cannot be negative")
    return z_score(service_level) * math.sqrt(lead_days * daily_sd ** 2 + daily_mean ** 2 * lead_sd_days ** 2)


def reorder_point(lead_demand: float, safety: float) -> float:
    """The stock position at which an order goes out: the units expected to sell during the lead time plus the safety stock."""
    return lead_demand + safety


def round_to_order(quantity: int, moq: int, pack_size: int) -> int:
    """An order quantity a supplier accepts: nothing when none is due, otherwise at least the minimum, in whole packs."""
    if quantity <= 0:
        return 0
    quantity = max(quantity, moq)
    return int(math.ceil(quantity / pack_size)) * pack_size


# ------------------------------------------------------------------------------------------------ classes and labels

def abc_classes(revenue: Mapping[str, int], a_share: str = "0.80", b_share: str = "0.95") -> dict[str, str]:
    """A variant is A while the revenue of the variants ranked above it is under `a_share` of the total, B under `b_share`, and C after that or with no revenue.

    Ranked by revenue, largest first, ties broken by key so the answer does not depend on input order. Every key gets exactly one class (a partition). Exact arithmetic on integers.
    """
    a_cut, b_cut = Fraction(a_share), Fraction(b_share)
    total = sum(revenue.values())
    classes: dict[str, str] = {}
    running = 0
    for key, amount in sorted(revenue.items(), key=lambda kv: (-kv[1], kv[0])):
        if amount <= 0 or total <= 0:
            classes[key] = "C"
            continue
        before = Fraction(running, total)
        classes[key] = "A" if before < a_cut else "B" if before < b_cut else "C"
        running += amount
    return classes


@dataclass(frozen=True)
class PlanParams:
    recent_days: int = 28
    prior_days: int = 28
    service_level: float = 0.95
    #: Days of cover the order buys beyond the lead time, so the order is not placed again the next morning.
    review_days: int = 14
    unknown_policy: str = "assume_in"
    abc_a: str = "0.80"
    abc_b: str = "0.95"
    #: A is a best seller only with at least this many units sold in all the history held.
    best_min_units: int = 10
    #: Rising: the recent velocity is at least this multiple of the prior window's, with at least `rising_min_units` sold recently.
    rising_ratio: float = 1.5
    rising_min_units: int = 5
    #: Slow mover: under this many units a day over the recent window (0.1 is about three in four weeks).
    slow_max_velocity: float = 0.1

    def as_json(self) -> dict[str, object]:
        return dict(self.__dict__)


def label_for(abc: str, units_all: int, recent: Velocity, prior: Velocity, params: PlanParams) -> str:
    """best_seller (class A with enough units sold), rising (recent velocity well above the prior window), slow_mover (under the slow threshold), else steady. First match wins."""
    if abc == "A" and units_all >= params.best_min_units:
        return "best_seller"
    if recent.units >= params.rising_min_units and (prior.mean == 0 or recent.mean >= params.rising_ratio * prior.mean):
        return "rising"
    if recent.mean < params.slow_max_velocity:
        return "slow_mover"
    return "steady"


# ------------------------------------------------------------------------------------------------ one variant, then all of them

@dataclass(frozen=True)
class VariantInput:
    key: str                                       # the variant's id
    available: int
    incoming: int = 0
    tracked: bool = True
    #: False when no snapshot was ever read for the variant: its stock is then not known, and it is not planned.
    stock_known: bool = True
    snapshots: Mapping[date, int] = field(default_factory=dict)
    sales: Mapping[date, int] = field(default_factory=dict)
    units_all: int = 0
    revenue_minor: int = 0
    lead_time_days: int | None = None
    lead_time_sd_days: float = 0.0
    moq: int = 1
    pack_size: int = 1
    promos: Sequence[Promo] = ()
    #: Open purchase orders for the variant (app/inventory/orders.py), one entry for each order's remaining quantity and expected date. When there are any, they are the inbound stock
    #: and `incoming` (Shopify's own figure) is not used: the same goods can be in both, so the two are never added.
    arrivals: Sequence[Arrival] = ()


@dataclass(frozen=True)
class PlanRow:
    key: str
    status: str                                    # ok | no_supplier | untracked | no_stock_reading
    available: int
    incoming: int
    velocity: Velocity
    prior_velocity: Velocity
    trend_ratio: float | None
    cover_days: float | None
    forecast_cover_days: float | None
    stockout_date: date | None
    promo_factor: float
    lead_time_days: int | None
    lead_time_sd_days: float | None
    safety_stock: int | None
    reorder_point: int | None
    needs_reorder: bool
    suggested_qty: int
    abc_class: str
    label: str
    #: The cover the shortage alerts compare with the lead time: the plain cover, or, when open orders land on dated days, the cover those arrivals give.
    alert_cover_days: float | None = None
    #: Where `incoming` came from: orders (the open purchase orders), shopify (its incoming quantity) or none.
    inbound_source: str = "none"
    #: The earliest expected date among the open orders, and whether any of them is already past its date.
    inbound_expected_on: date | None = None
    inbound_late: bool = False


def plan_variant(item: VariantInput, as_of: date, abc: str, params: PlanParams) -> PlanRow:
    """Everything the plan says about one variant, over the complete days before `as_of`."""
    recent_days = date_range(as_of - timedelta(days=params.recent_days), as_of)
    prior_days = date_range(as_of - timedelta(days=params.recent_days + params.prior_days), as_of - timedelta(days=params.recent_days))
    states = stock_states(prior_days + recent_days, item.snapshots, item.sales)
    recent = stockout_adjusted_velocity(recent_days, item.sales, states, params.unknown_policy)
    prior = stockout_adjusted_velocity(prior_days, item.sales, states, params.unknown_policy)
    trend = recent.mean / prior.mean if prior.mean > 0 else None
    baseline = recent.mean
    label = label_for(abc, item.units_all, recent, prior, params)
    cover = days_of_cover(item.available, baseline)
    # Open purchase orders are the inbound stock when there are any; their dated arrivals also lengthen the cover. An order already past its date stays in the stock position (so the plan
    # does not buy the same goods twice, and the late alert is what asks for it to be chased) but is not assumed to land.
    on_time = [a for a in item.arrivals if a.expected_on is not None and a.expected_on >= as_of]
    forecast_cover = projected_cover(item.available, baseline, as_of, item.promos, arrivals=on_time)
    alert_cover = projected_cover(item.available, baseline, as_of, (), arrivals=on_time) if on_time else cover
    stockout = as_of + timedelta(days=int(forecast_cover)) if forecast_cover is not None else None
    if item.arrivals:
        inbound, source = sum(a.quantity for a in item.arrivals), "orders"
    else:
        inbound, source = item.incoming, "shopify" if item.incoming > 0 else "none"
    dated = [a.expected_on for a in item.arrivals if a.expected_on is not None]
    inbound_expected = min(dated) if dated else None
    inbound_late = any(d < as_of for d in dated)

    status = "untracked" if not item.tracked else "no_stock_reading" if not item.stock_known else "no_supplier" if item.lead_time_days is None else "ok"
    lead = item.lead_time_days
    if status != "ok" or lead is None:
        return PlanRow(item.key, status, item.available, inbound, recent, prior, trend, cover, forecast_cover, stockout, 1.0, lead,
                       item.lead_time_sd_days if lead is not None else None, None, None, False, 0, abc, label, alert_cover, source, inbound_expected, inbound_late)

    lead_demand = demand_over(baseline, as_of, lead, item.promos)
    factor = lead_demand / (baseline * lead) if baseline * lead > 0 else 1.0
    ss = ceil_units(safety_stock(baseline * factor, recent.sd * factor, lead, item.lead_time_sd_days, params.service_level))
    rop = ceil_units(reorder_point(lead_demand, ss))
    position = item.available + inbound
    needs = baseline > 0 and position <= rop
    quantity = 0
    if needs:
        target = demand_over(baseline, as_of, lead + params.review_days, item.promos) + ss
        quantity = round_to_order(ceil_units(target - position), item.moq, item.pack_size)
    return PlanRow(item.key, status, item.available, inbound, recent, prior, trend, cover, forecast_cover, stockout, factor, lead,
                   item.lead_time_sd_days, ss, rop, needs, quantity, abc, label, alert_cover, source, inbound_expected, inbound_late)


def plan_all(items: Sequence[VariantInput], as_of: date, params: PlanParams | None = None) -> list[PlanRow]:
    """The plan for every variant: classes are worked out across all of them, then each variant is planned."""
    params = params or PlanParams()
    classes = abc_classes({i.key: i.revenue_minor for i in items}, params.abc_a, params.abc_b)
    return [plan_variant(i, as_of, classes[i.key], params) for i in items]


def last_restock(snapshots: Mapping[date, int]) -> date | None:
    """The date of the latest snapshot whose quantity is higher than the one before it: when stock last arrived. None when no increase was ever seen."""
    latest = None
    previous = None
    for day in sorted(snapshots):
        if previous is not None and snapshots[day] > previous:
            latest = day
        previous = snapshots[day]
    return latest
