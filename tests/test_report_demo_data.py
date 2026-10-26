"""The invented ad and search figures the sample deck is built from: they have the holes they say they have, they are valid for the
connectors that will read them, and the same seed always draws the same month.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.report import demo_data as d  # noqa: E402


def days_between(start: date, end: date):
    while start < end:
        yield start
        start += timedelta(days=1)


def test_the_ad_days_are_every_day_except_the_quiet_and_the_missing_ones():
    days = d.demo_paid_days()
    expected = set(days_between(d.START, d.END)) - d.PAID_QUIET_DAYS - d.PAID_MISSING_DAYS
    assert set(days) == expected and len(days) == 61 - 3
    assert d.PAID_QUIET_DAYS.isdisjoint(d.PAID_MISSING_DAYS)


def test_the_ad_sync_windows_leave_exactly_the_failed_window_and_cover_every_other_day():
    covered = set()
    for start, end in d.PAID_SYNC_WINDOWS:
        covered |= set(days_between(start, end))
    failed = set(days_between(d.PAID_FAILED_WINDOW.window_start, d.PAID_FAILED_WINDOW.window_end))
    assert d.PAID_FAILED_WINDOW.status == "failed" and failed == d.PAID_MISSING_DAYS
    assert covered | failed == set(days_between(d.START, d.END)) and not covered & failed


def test_every_ad_row_is_one_the_meta_connector_would_accept_and_the_purchase_holes_are_where_they_say():
    for day, row in d.demo_paid_days().items():
        assert Decimal(row["spend"]) > 0 and row["impressions"] > 0 and 0 <= row["clicks"] <= row["impressions"], day
        assert (row["purchases"] is None) == (day in d.PAID_NO_PURCHASE_DAYS) == (row["purchase_value"] is None), day
        if row["purchases"] is not None:
            assert row["purchases"] >= 0 and Decimal(row["purchase_value"]) >= 0
    assert d.PAID_NO_PURCHASE_DAYS <= set(d.demo_paid_days())
    assert d.PAID_CURRENCY == "EUR", "the ad account is not in the store's dollars, on purpose"


def test_october_ads_run_a_little_higher_than_september_as_the_sample_says():
    days = d.demo_paid_days()
    mean = lambda month: sum(Decimal(r["spend"]) for k, r in days.items() if k.month == month) / sum(1 for k in days if k.month == month)  # noqa: E731
    assert mean(10) > mean(9)


def test_the_search_days_are_every_day_except_the_quiet_and_the_missing_ones_and_have_top_rows_for_each():
    days, queries, pages = d.demo_search_days()
    expected = set(days_between(d.START, d.END)) - d.SEARCH_QUIET_DAYS - d.SEARCH_MISSING_DAYS
    assert set(days) == expected == set(queries) == set(pages)
    covered = set()
    for start, end in d.SEARCH_SYNC_WINDOWS:
        covered |= set(days_between(start, end))
    failed = set(days_between(d.SEARCH_FAILED_WINDOW.window_start, d.SEARCH_FAILED_WINDOW.window_end))
    assert failed == d.SEARCH_MISSING_DAYS and d.SEARCH_FAILED_WINDOW.status == "failed" and not covered & failed
    assert covered | failed == set(days_between(d.START, d.END))


def test_every_search_row_is_one_the_search_console_connector_would_accept():
    days, queries, pages = d.demo_search_days()
    for day, (clicks, impressions, position) in days.items():
        assert 0 <= clicks <= impressions and 1.0 <= position <= 12.0, day
        for rows, keys in ((queries[day], d._QUERIES), (pages[day], d._PAGES)):
            assert [r[0] for r in rows] == list(keys) and len({r[0] for r in rows}) == len(rows), "one row for each key, no key twice"
            assert all(0 <= r[1] <= r[2] and r[3] > 0 for r in rows), day
            assert len(rows) > 10 or rows is pages[day], "more queries than the ten the connector keeps, so the cut is exercised"


def test_a_search_day_has_no_more_top_clicks_than_the_day_had_clicks():
    days, queries, pages = d.demo_search_days()
    for day, (clicks, _, _) in days.items():
        assert sum(r[1] for r in queries[day]) <= clicks and sum(r[1] for r in pages[day]) <= clicks, day


def test_october_search_clicks_and_position_improve_as_the_sample_says():
    days, _, _ = d.demo_search_days()
    clicks = lambda month: sum(v[0] for k, v in days.items() if k.month == month) / sum(1 for k in days if k.month == month)  # noqa: E731
    position = lambda month: sum(v[2] for k, v in days.items() if k.month == month) / sum(1 for k in days if k.month == month)  # noqa: E731
    assert clicks(10) > clicks(9) and position(10) < position(9)


def test_the_same_seed_draws_the_same_month_and_another_seed_draws_another():
    assert d.demo_paid_days() == d.demo_paid_days(d.SEED) and d.demo_paid_days() != d.demo_paid_days(d.SEED + 1)
    assert d.demo_search_days() == d.demo_search_days(d.SEED) and d.demo_search_days() != d.demo_search_days(d.SEED + 1)
