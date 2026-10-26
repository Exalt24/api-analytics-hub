"""Shared hand-made ad and search rows for the paid media and search report tests (not a test file).

The numbers are small on purpose so every expectation can be checked with a pencil, and they sit on the same
calendar as `report_fixtures.py`: September holds two days, October holds two days, and October has the same
two day hole that no sync run covers (the 10th and 11th).

  Ads (EUR, the store is in USD)
    September: day 1  spend 10000, impressions 1000, clicks 50, purchases 5, purchase value 40000
               day 2  spend  5000, impressions  500, clicks 20, purchases 2, purchase value 10000
    October:   day 1  spend 12000, impressions 1200, clicks 60, purchases 6, purchase value 54000
               day 2  spend  6000, impressions  400, clicks 10, and NO purchase figures at all
  Search
    September: day 1  clicks 10, impressions 100, position 7.500
               day 2  clicks 20, impressions 200, position 6.000
    October:   day 1  clicks 12, impressions 150, position 5.000
               day 2  clicks  8, impressions  50, position 9.000
"""
from __future__ import annotations

from datetime import date

from report_fixtures import AS_OF, COVERED, both_months  # noqa: F401 - re-exported

PAID_COVERED = COVERED
SEARCH_COVERED = COVERED


def paid_day(day: date, spend: int, impressions: int, clicks: int, purchases: int | None = None, value: int | None = None,
             currency: str = "EUR"):
    rows = [
        {"observed_on": day, "metric_key": "ad_spend", "value_numeric": spend, "currency": currency},
        {"observed_on": day, "metric_key": "ad_impressions", "value_numeric": impressions, "currency": None},
        {"observed_on": day, "metric_key": "ad_clicks", "value_numeric": clicks, "currency": None},
    ]
    if purchases is not None:
        rows.append({"observed_on": day, "metric_key": "ad_purchases", "value_numeric": purchases, "currency": None})
    if value is not None:
        rows.append({"observed_on": day, "metric_key": "ad_purchase_value", "value_numeric": value, "currency": currency})
    return rows


def paid_september():
    return paid_day(date(2026, 9, 1), 10000, 1000, 50, 5, 40000) + paid_day(date(2026, 9, 2), 5000, 500, 20, 2, 10000)


def paid_october():
    return paid_day(date(2026, 10, 1), 12000, 1200, 60, 6, 54000) + paid_day(date(2026, 10, 2), 6000, 400, 10)


def paid_both():
    return paid_september() + paid_october()


def search_day(day: date, clicks: int, impressions: int, position_milli: int):
    return [
        {"observed_on": day, "metric_key": "search_clicks", "value_numeric": clicks, "currency": None},
        {"observed_on": day, "metric_key": "search_impressions", "value_numeric": impressions, "currency": None},
        {"observed_on": day, "metric_key": "search_position", "value_numeric": position_milli, "currency": None},
    ]


def search_both():
    return (search_day(date(2026, 9, 1), 10, 100, 7500) + search_day(date(2026, 9, 2), 20, 200, 6000)
            + search_day(date(2026, 10, 1), 12, 150, 5000) + search_day(date(2026, 10, 2), 8, 50, 9000))


def top(day: date, dimension: str, key: str, clicks: int, impressions: int, position_milli: int):
    return {"observed_on": day, "dimension": dimension, "key": key, "clicks": clicks, "impressions": impressions,
            "position_milli": position_milli}


def top_october():
    d1, d2 = date(2026, 10, 1), date(2026, 10, 2)
    return [
        top(d1, "query", "shoes", 6, 60, 5000), top(d1, "query", "boots", 4, 40, 10000), top(d1, "page", "/a", 10, 100, 7000),
        top(d2, "query", "shoes", 3, 20, 7000), top(d2, "query", "socks", 5, 30, 4000), top(d2, "page", "/a", 4, 40, 6000),
        top(d2, "page", "/b", 4, 10, 2000),
    ]
