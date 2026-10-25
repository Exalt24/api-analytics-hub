"""Shared hand-made snapshot rows for the monthly report tests (not a test file).

The numbers are small on purpose so every expectation in the tests can be checked with a
pencil: September holds two days, October holds two days, and October has a two day hole
that no sync run covers.

  September: day 1  orders 10, gross 10000, refunds 1000, buyers 8
             day 2  orders  5, gross  5000, refunds    0, buyers 5
  October:   day 1  orders 12, gross 18000, refunds 3000, buyers 9
             day 2  orders  8, gross  6000, refunds    0, buyers 7
"""
from __future__ import annotations

from datetime import date

AS_OF = date(2026, 11, 15)
SEPTEMBER_COVERED = [(date(2026, 9, 1), date(2026, 10, 1))]
#: October 10 and 11 are not inside any window
OCTOBER_COVERED = [(date(2026, 10, 1), date(2026, 10, 10)), (date(2026, 10, 12), date(2026, 11, 1))]
COVERED = SEPTEMBER_COVERED + OCTOBER_COVERED


def day_rows(day: date, orders: int, gross: int, refunds: int, buyers: int | None = None, currency: str = "USD"):
    rows = [
        {"observed_on": day, "metric_key": "orders", "value_numeric": orders, "currency": None},
        {"observed_on": day, "metric_key": "gross_sales", "value_numeric": gross, "currency": currency},
        {"observed_on": day, "metric_key": "refunds", "value_numeric": refunds, "currency": currency},
        {"observed_on": day, "metric_key": "net_sales", "value_numeric": gross - refunds, "currency": currency},
    ]
    if orders:
        rows.append({"observed_on": day, "metric_key": "avg_order_value", "value_numeric": gross // orders,
                     "currency": currency})
    if buyers is not None:
        rows.append({"observed_on": day, "metric_key": "customers", "value_numeric": buyers, "currency": None})
    return rows


def september_rows():
    return day_rows(date(2026, 9, 1), 10, 10000, 1000, 8) + day_rows(date(2026, 9, 2), 5, 5000, 0, 5)


def october_rows():
    return day_rows(date(2026, 10, 1), 12, 18000, 3000, 9) + day_rows(date(2026, 10, 2), 8, 6000, 0, 7)


def both_months():
    return september_rows() + october_rows()
