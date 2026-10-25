"""Invented daily snapshots for the sample report. No store, no customer, no real order.

A fixed seed draws orders per day with a weekend lift and a slightly busier second month,
then builds the same canonical points the Shopify connector would (so the money and
currency rules in `CanonicalPoint` apply). It also lays out the holes a real history has:

  * quiet days, covered by a sync run that found no orders (no rows, not missing)
  * two days no sync covered (no rows, missing), plus a failed run over them, which
    must not count as coverage

`demo_points()` and `DEMO_RUNS` are what `scripts/build_sample_report.py` loads.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

from app.connectors.base import CanonicalPoint

SEED = 2026
START = date(2026, 9, 1)
END = date(2026, 11, 1)  # half-open

QUIET_DAYS = frozenset({date(2026, 9, 7), date(2026, 9, 22), date(2026, 10, 6)})
MISSING_DAYS = frozenset({date(2026, 10, 14), date(2026, 10, 15)})
CURRENCY = "USD"


@dataclass(frozen=True)
class DemoRun:
    window_start: date
    window_end: date
    status: str


DEMO_RUNS = (
    DemoRun(date(2026, 9, 1), date(2026, 10, 14), "succeeded"),
    DemoRun(date(2026, 10, 14), date(2026, 10, 16), "failed"),
    DemoRun(date(2026, 10, 16), date(2026, 11, 1), "succeeded"),
)


def demo_points(seed: int = SEED) -> list[CanonicalPoint]:
    rng = random.Random(seed)
    points: list[CanonicalPoint] = []
    day = START
    while day < END:
        lift = 1.0 + (0.08 if day.month == 10 else 0.0)
        weekend = day.weekday() >= 4
        orders = int(rng.randint(14, 22) * (1.35 if weekend else 1.0) * lift)
        if day in MISSING_DAYS or day in QUIET_DAYS:
            day += timedelta(days=1)
            continue
        gross = refunds = 0
        for _ in range(orders):
            price = rng.randint(2400, 11800)
            gross += price
            if rng.random() < 0.06:
                refunds += price if rng.random() < 0.5 else price // 2
        buyers = orders - rng.randint(0, orders // 4)
        net = gross - refunds
        points += [
            CanonicalPoint("orders", day, orders),
            CanonicalPoint("gross_sales", day, gross, CURRENCY),
            CanonicalPoint("refunds", day, refunds, CURRENCY),
            CanonicalPoint("net_sales", day, net, CURRENCY),
            CanonicalPoint("avg_order_value", day, gross // orders, CURRENCY),
            CanonicalPoint("customers", day, buyers),
        ]
        day += timedelta(days=1)
    return points
