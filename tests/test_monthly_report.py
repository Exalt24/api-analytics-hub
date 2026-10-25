"""The report's arithmetic and its day accounting, from hand-made rows.

No database, no model, no slide library. The expectations are written out by hand in
report_fixtures.py (a pencil can check them), not copied from the code under test. The
weight of the file is on the days: an empty month, a missing day, a gap in the middle, the
first month with nothing before it, a zero baseline and the edges of a month, because those
are where a monthly total is quietly wrong.
"""
from __future__ import annotations

import random
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.report.monthly import (
    ReportError,
    build_report,
    daily_series,
    days_text,
    format_money,
    month_bounds,
    month_stats,
    pct_change,
    previous_month,
)
from report_fixtures import (
    AS_OF,
    COVERED,
    OCTOBER_COVERED,
    SEPTEMBER_COVERED,
    both_months,
    day_rows,
    october_rows,
    september_rows,
)


def report(rows=None, covered=COVERED, as_of=AS_OF):
    return build_report(both_months() if rows is None else rows, 2026, 10, label="Demo", covered=covered, as_of=as_of)


# ------------------------------------------------------------ the KPI table

def test_the_kpi_table_matches_the_hand_computed_figures():
    r = report()
    shown = {k.key: (k.current_display, k.previous_display, k.change_display, k.pct_display) for k in r.kpis}
    assert shown == {
        "orders": ("20", "15", "+5", "+33.3%"),
        "gross_sales": ("USD 240.00", "USD 150.00", "+USD 90.00", "+60.0%"),
        "refunds": ("USD 30.00", "USD 10.00", "+USD 20.00", "+200.0%"),
        "net_sales": ("USD 210.00", "USD 140.00", "+USD 70.00", "+50.0%"),
        "avg_order_value": ("USD 12.00", "USD 10.00", "+USD 2.00", "+20.0%"),
        "buyers_per_day": ("8.0", "6.5", "+1.5", "+23.1%"),
    }


def test_the_integer_values_behind_the_display_are_exact():
    r = report()
    assert r.current.totals == {"orders": 20, "gross_sales": 24000, "refunds": 3000, "net_sales": 21000}
    assert r.current.avg_order_value == 1200
    assert r.previous.totals["net_sales"] == 14000


def test_best_days_are_ordered_by_net_sales_then_date():
    assert report().top_days == [(date(2026, 10, 1), 15000), (date(2026, 10, 2), 6000)]
    tie = day_rows(date(2026, 10, 3), 1, 5000, 0) + day_rows(date(2026, 10, 2), 1, 5000, 0)
    r = build_report(tie, 2026, 10, covered=OCTOBER_COVERED, as_of=AS_OF)
    assert [d for d, _ in r.top_days] == [date(2026, 10, 2), date(2026, 10, 3)]


def test_a_total_is_the_plain_sum_of_the_rows_over_random_months():
    rng = random.Random(7)
    for _ in range(25):
        rows, expected = [], 0
        for d in range(1, 32):
            if rng.random() < 0.3:
                continue
            gross, refunds = rng.randint(0, 90000), rng.randint(0, 3000)
            rows += day_rows(date(2026, 10, d), rng.randint(1, 40), gross, refunds)
            expected += gross - refunds
        if not rows:
            continue
        r = build_report(rows, 2026, 10, covered=OCTOBER_COVERED + [(date(2026, 10, 10), date(2026, 10, 12))], as_of=AS_OF)
        assert r.current.totals["net_sales"] == expected


# ------------------------------------------------------------ the days

def test_a_covered_day_without_rows_is_quiet_and_an_uncovered_one_is_missing():
    s = month_stats(october_rows(), 2026, 10, covered=OCTOBER_COVERED, as_of=AS_OF)
    assert s.data_days == [date(2026, 10, 1), date(2026, 10, 2)]
    assert s.missing_days == [date(2026, 10, 10), date(2026, 10, 11)]
    assert len(s.quiet_days) == 31 - 2 - 2
    assert not s.fully_covered


def test_a_gap_in_the_middle_marks_the_changes_partial_and_the_sums_use_the_days_present():
    r = report()
    assert {k.note for k in r.kpis} == {"partial data"}
    assert r.current.totals["orders"] == 20


def test_a_failed_run_is_not_coverage_so_its_days_stay_missing():
    # the caller passes only succeeded and partial runs; a window that was never passed in covers nothing
    s = month_stats(october_rows(), 2026, 10, covered=[(date(2026, 10, 1), date(2026, 10, 10))], as_of=AS_OF)
    assert date(2026, 10, 20) in s.missing_days


def test_a_window_end_is_exclusive():
    s = month_stats(october_rows(), 2026, 10, covered=[(date(2026, 10, 1), date(2026, 10, 3))], as_of=AS_OF)
    assert date(2026, 10, 3) in s.missing_days and date(2026, 10, 2) in s.data_days


def test_a_month_with_nothing_stored_and_no_sync_refuses_to_report():
    with pytest.raises(ReportError, match="nothing to report"):
        build_report([], 2026, 10, covered=[], as_of=AS_OF)


def test_a_month_with_a_sync_that_found_no_orders_reports_zeros_not_an_error():
    r = build_report([], 2026, 10, covered=OCTOBER_COVERED, as_of=AS_OF)
    assert r.current.totals["orders"] == 0
    assert r.kpi("orders").current_display == "0"
    assert r.kpi("avg_order_value").current_display == "n/a"
    assert r.kpi("avg_order_value").note == "not available"


def test_the_first_month_has_no_previous_and_says_so():
    r = build_report(october_rows(), 2026, 10, covered=OCTOBER_COVERED, as_of=AS_OF)
    assert r.previous is None
    for k in r.kpis:
        assert (k.previous_display, k.change_display, k.pct_display) == ("n/a", "n/a", "n/a")
    assert r.kpi("orders").note == "no previous month"
    assert r.kpi("orders").current_display == "20"


def test_a_previous_month_that_was_synced_but_empty_is_a_zero_baseline_not_a_missing_month():
    r = build_report(october_rows(), 2026, 10, covered=COVERED, as_of=AS_OF)
    assert r.previous is not None
    k = r.kpi("orders")
    assert (k.previous_display, k.change_display, k.pct_display, k.note) == ("0", "+20", "n/a", "zero baseline")
    assert r.kpi("avg_order_value").previous_display == "n/a"
    assert r.kpi("avg_order_value").note == "no previous value"


def test_a_month_in_progress_expects_only_the_days_that_are_over():
    r = build_report(october_rows(), 2026, 10, covered=OCTOBER_COVERED, as_of=date(2026, 10, 10))
    assert r.current.days_expected == 9 and not r.current.complete
    assert r.current.missing_days == []


def test_rows_dated_today_or_later_are_not_part_of_a_month_in_progress():
    rows = october_rows() + day_rows(date(2026, 10, 10), 99, 99000, 0)
    s = month_stats(rows, 2026, 10, covered=OCTOBER_COVERED, as_of=date(2026, 10, 10))
    assert s.totals["orders"] == 20


def test_a_month_that_has_not_finished_its_first_day_refuses():
    with pytest.raises(ReportError, match="first day"):
        month_stats([], 2026, 10, as_of=date(2026, 10, 1))


# ------------------------------------------------------------ month boundaries

def test_the_last_and_first_day_of_adjacent_months_land_in_their_own_month():
    rows = day_rows(date(2026, 9, 30), 3, 3000, 0) + day_rows(date(2026, 10, 1), 4, 4000, 0) \
        + day_rows(date(2026, 11, 1), 50, 50000, 0)
    r = build_report(rows, 2026, 10, covered=COVERED, as_of=AS_OF)
    assert r.previous.totals["orders"] == 3
    assert r.current.totals["orders"] == 4


def test_january_looks_back_to_december_of_the_year_before():
    assert previous_month(2027, 1) == (2026, 12)
    assert previous_month(2026, 10) == (2026, 9)


def test_month_lengths_including_a_leap_february():
    assert month_bounds(2028, 2) == (date(2028, 2, 1), date(2028, 3, 1))
    assert month_bounds(2027, 2)[1] == date(2027, 3, 1)
    assert month_bounds(2026, 12)[1] == date(2027, 1, 1)
    with pytest.raises(ReportError):
        month_bounds(2026, 13)


# ------------------------------------------------------------ percent and money

@pytest.mark.parametrize("current, previous, expected", [
    (10025, 10000, "0.3"),      # 0.25 rounds half up
    (99996, 100000, "0.0"),     # -0.004 rounds to zero
    (150, 100, "50.0"),
    (50, 100, "-50.0"),
    (-500, -1000, "50.0"),      # the divisor is the absolute value, so improvement stays positive
    (5, 0, None),               # a zero baseline has no percentage
    (0, 0, None),
])
def test_percent_change(current, previous, expected):
    assert pct_change(current, previous) == (None if expected is None else Decimal(expected))


def test_a_change_that_rounds_to_zero_prints_without_a_sign():
    rows = september_rows() + day_rows(date(2026, 10, 1), 15, 14999, 0, 1)
    r = build_report(rows, 2026, 10, covered=COVERED, as_of=AS_OF)
    assert r.kpi("gross_sales").pct_display == "0.0%"


def test_money_is_exact_and_zero_decimal_currencies_are_not_scaled():
    assert format_money(5, "USD") == "USD 0.05"
    assert format_money(123456789, "usd") == "USD 1,234,567.89"
    assert format_money(-1050, "USD") == "-USD 10.50"
    assert format_money(1234, "JPY") == "JPY 1,234"


def test_a_money_row_without_a_currency_is_refused():
    rows = october_rows()
    rows[1] = dict(rows[1], currency=None)
    with pytest.raises(ReportError, match="no currency"):
        build_report(rows, 2026, 10, covered=OCTOBER_COVERED, as_of=AS_OF)


# ------------------------------------------------------------ refusals

def test_two_rows_for_one_day_and_metric_are_refused_not_summed():
    with pytest.raises(ReportError, match="two rows"):
        build_report(october_rows() + day_rows(date(2026, 10, 1), 1, 100, 0), 2026, 10,
                     covered=OCTOBER_COVERED, as_of=AS_OF)


def test_two_currencies_in_a_month_are_refused():
    rows = october_rows() + day_rows(date(2026, 10, 3), 1, 100, 0, currency="EUR")
    with pytest.raises(ReportError, match="more than one currency"):
        build_report(rows, 2026, 10, covered=OCTOBER_COVERED, as_of=AS_OF)


def test_a_currency_change_between_months_is_refused():
    rows = september_rows() + day_rows(date(2026, 10, 1), 1, 100, 0, currency="EUR")
    with pytest.raises(ReportError, match="cannot be compared"):
        build_report(rows, 2026, 10, covered=COVERED, as_of=AS_OF)


def test_a_float_value_is_refused_at_the_boundary():
    rows = october_rows()
    rows[0] = dict(rows[0], value_numeric=12.0)
    with pytest.raises(ReportError, match="not an integer"):
        build_report(rows, 2026, 10, covered=OCTOBER_COVERED, as_of=AS_OF)


# ------------------------------------------------------------ the customers metric

def test_buyers_are_averaged_over_the_days_that_report_them_never_summed():
    assert report().current.buyers_per_active_day == Decimal("8.0")


def test_a_withheld_customers_metric_is_not_available_rather_than_zero():
    rows = [r for r in october_rows() if r["metric_key"] != "customers"]
    r = build_report(rows, 2026, 10, covered=OCTOBER_COVERED, as_of=AS_OF)
    k = r.kpi("buyers_per_day")
    assert (k.current_display, k.note) == ("n/a", "not available")


# ------------------------------------------------------------ chart series and lists

def test_a_daily_series_draws_a_missing_day_as_a_gap_and_a_quiet_day_as_zero():
    s = month_stats(october_rows(), 2026, 10, covered=OCTOBER_COVERED, as_of=AS_OF)
    series = dict(daily_series(s, "net_sales"))
    assert series[date(2026, 10, 1)] == 15000
    assert series[date(2026, 10, 5)] == 0
    assert series[date(2026, 10, 10)] is None and series[date(2026, 10, 11)] is None
    assert len(series) == 31


def test_a_list_of_days_says_how_many_it_cut():
    days = [date(2026, 10, d) for d in range(1, 16)]
    assert days_text(days).endswith("and 5 more")
    assert days_text([]) == "none"
    assert days_text(days[:2]) == "2026-10-01, 2026-10-02"
