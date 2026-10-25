"""The report's arithmetic and its day accounting, from hand-made rows.

No database, no model, no slide library. The expectations are written out by hand in
report_fixtures.py (a pencil can check them), not copied from the code under test. The
weight of the file is on the days: an empty month, a missing day, a gap in the middle, the
first month with nothing before it, a zero baseline and the edges of a month, because those
are where a monthly total is quietly wrong. The wording helpers that put a caveat into a
headline are tested here too, since a fall stated without its caveat is a numbers error.
"""
from __future__ import annotations

import random
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.report import monthly
from app.report.monthly import (
    ReportError,
    build_report,
    claim_title,
    daily_series,
    days_text,
    fmt_day,
    format_money,
    insights_title,
    largest_move,
    month_bounds,
    month_stats,
    move_phrase,
    partial_clause,
    pct_change,
    plural,
    previous_month,
    utc_today,
)
from report_fixtures import (
    AS_OF,
    COVERED,
    OCTOBER_COVERED,
    both_months,
    day_rows,
    october_rows,
    september_rows,
)

FULL = [(date(2026, 9, 1), date(2026, 10, 1)), (date(2026, 10, 1), date(2026, 11, 1))]


def report(rows=None, covered=COVERED, as_of=AS_OF):
    return build_report(both_months() if rows is None else rows, 2026, 10, label="Demo", covered=covered, as_of=as_of)


# ------------------------------------------------------------ the KPI table

def test_the_kpi_table_matches_the_hand_computed_figures():
    r = report()
    shown = {k.key: (k.current_display, k.previous_display, k.change_display, k.pct_display) for k in r.kpis}
    assert shown == {
        "orders": ("20", "15", "+5", "+33.3%"),
        "gross_sales": ("$240.00", "$150.00", "+$90.00", "+60.0%"),
        "refunds": ("$30.00", "$10.00", "+$20.00", "+200.0%"),
        "net_sales": ("$210.00", "$140.00", "+$70.00", "+50.0%"),
        "avg_order_value": ("$12.00", "$10.00", "+$2.00", "+20.0%"),
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


def test_the_buyers_percent_comes_from_the_unrounded_means():
    """Three October days of 9, 8 and 8 buyers average 8.333; against 6.5 that is +28.2%.
    From the rounded 8.3 it would read +27.7%. The displayed values and the change keep the
    rounded figures, so a reader can subtract what is on the slide."""
    rows = september_rows() + day_rows(date(2026, 10, 1), 1, 100, 0, 9) + day_rows(date(2026, 10, 2), 1, 100, 0, 8) \
        + day_rows(date(2026, 10, 3), 1, 100, 0, 8)
    k = build_report(rows, 2026, 10, covered=FULL, as_of=AS_OF).kpi("buyers_per_day")
    assert (k.current_display, k.previous_display, k.change_display, k.pct_display) == ("8.3", "6.5", "+1.8", "+28.2%")


# ------------------------------------------------------------ the days

def test_a_covered_day_without_rows_is_quiet_and_an_uncovered_one_is_missing():
    s = month_stats(october_rows(), 2026, 10, covered=OCTOBER_COVERED, as_of=AS_OF)
    assert s.data_days == [date(2026, 10, 1), date(2026, 10, 2)]
    assert s.missing_days == [date(2026, 10, 10), date(2026, 10, 11)]
    assert len(s.quiet_days) == 31 - 2 - 2
    assert not s.fully_covered


def test_days_with_orders_no_orders_and_no_data_add_up_to_the_month():
    s = month_stats(october_rows(), 2026, 10, covered=OCTOBER_COVERED, as_of=AS_OF)
    assert len(s.data_days) + len(s.quiet_days) + len(s.missing_days) == s.days_in_month
    assert s.reported_days == len(s.data_days) + len(s.quiet_days) == 29


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


def test_the_first_month_has_no_previous_and_says_so_and_keeps_the_partial_note():
    r = build_report(october_rows(), 2026, 10, covered=OCTOBER_COVERED, as_of=AS_OF)
    assert r.previous is None
    for k in r.kpis:
        assert (k.previous_display, k.change_display, k.pct_display) == ("n/a", "n/a", "n/a")
    assert r.kpi("orders").notes == ("no previous month", "partial data")
    assert r.kpi("orders").current_display == "20"


def test_a_previous_month_that_was_synced_but_empty_is_a_zero_baseline_not_a_missing_month():
    r = build_report(october_rows(), 2026, 10, covered=COVERED, as_of=AS_OF)
    assert r.previous is not None
    k = r.kpi("orders")
    assert (k.previous_display, k.change_display, k.pct_display) == ("0", "+20", "n/a")
    assert k.notes == ("zero baseline", "partial data")
    assert r.kpi("avg_order_value").previous_display == "n/a"
    assert r.kpi("avg_order_value").notes == ("no previous value", "partial data")


def test_a_baseline_note_never_hides_the_partial_data_note():
    """The October holes make every October figure partial, whichever note comes first."""
    for rows, covered in ((october_rows(), OCTOBER_COVERED), (october_rows(), COVERED), (both_months(), COVERED)):
        r = build_report(rows, 2026, 10, covered=covered, as_of=AS_OF)
        for k in r.kpis:
            if k.current is not None:
                assert "partial data" in k.notes, (k.key, k.notes)


def test_a_month_in_progress_expects_only_the_days_that_are_over():
    r = build_report(october_rows(), 2026, 10, covered=OCTOBER_COVERED, as_of=date(2026, 10, 10))
    assert r.current.days_expected == 9 and not r.current.complete
    assert r.current.missing_days == []
    assert "partial data" in r.kpi("orders").notes


def test_rows_dated_today_or_later_are_not_part_of_a_month_in_progress():
    rows = october_rows() + day_rows(date(2026, 10, 10), 99, 99000, 0)
    s = month_stats(rows, 2026, 10, covered=OCTOBER_COVERED, as_of=date(2026, 10, 10))
    assert s.totals["orders"] == 20


def test_a_month_that_has_not_finished_its_first_day_refuses():
    with pytest.raises(ReportError, match="first day"):
        month_stats([], 2026, 10, as_of=date(2026, 10, 1))


def test_today_defaults_to_the_utc_date_because_the_connector_buckets_by_utc(monkeypatch):
    """A fake clock far ahead makes October a finished month only if the default reads it."""
    monkeypatch.setattr(monthly, "utc_today", lambda: date(2099, 1, 1))
    assert month_stats(october_rows(), 2026, 10, covered=OCTOBER_COVERED).complete


def test_utc_today_reads_the_utc_clock_not_the_local_one(monkeypatch):
    class FakeClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 2, 0, 30, tzinfo=timezone.utc) if tz is timezone.utc else datetime(2026, 11, 1, 19, 30)

    monkeypatch.setattr(monthly, "datetime", FakeClock)
    assert utc_today() == date(2026, 11, 2)


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


def test_money_is_exact_dollars_get_a_sign_and_other_currencies_keep_their_code():
    assert format_money(5, "USD") == "$0.05"
    assert format_money(123456789, "usd") == "$1,234,567.89"
    assert format_money(-1050, "USD") == "-$10.50"
    assert format_money(1234, "JPY") == "JPY 1,234"
    assert format_money(500, "EUR") == "EUR 5.00"


def test_a_money_row_without_a_currency_is_refused():
    rows = october_rows()
    rows[1] = dict(rows[1], currency=None)
    with pytest.raises(ReportError, match="no currency"):
        build_report(rows, 2026, 10, covered=OCTOBER_COVERED, as_of=AS_OF)


# ------------------------------------------------------------ the wording of a change

def flat_report(net_after: int):
    """October's single day nets `net_after` against September's 14000."""
    rows = september_rows() + day_rows(date(2026, 10, 1), 15, net_after + 1000, 1000, 8)
    return report(rows)


def test_a_change_that_rounds_to_zero_is_flat_and_says_by_how_much_not_unchanged():
    r = flat_report(14004)
    k = r.kpi("net_sales")
    assert (k.pct_display, k.change_display, k.flat, k.unchanged) == ("0.0%", "+$0.04", True, False)
    assert move_phrase(k) == "essentially flat (+$0.04)"
    assert claim_title(r, "net_sales") == "Net sales stayed essentially flat (+$0.04) from September (October is missing 2 days)"


def test_only_equal_values_are_called_unchanged():
    r = flat_report(14000)
    k = r.kpi("net_sales")
    assert (k.unchanged, k.flat) == (True, False)
    assert move_phrase(k) == "unchanged"
    assert claim_title(r, "net_sales") == "Net sales did not change from September (October is missing 2 days)"


def test_a_rise_a_fall_and_a_zero_base_have_their_own_phrases():
    r = report()
    assert move_phrase(r.kpi("net_sales")) == "up 50.0%"
    fall = report(september_rows() + day_rows(date(2026, 10, 1), 3, 2000, 100, 2))
    assert move_phrase(fall.kpi("net_sales")) == "down 86.4%"
    assert move_phrase(build_report(october_rows(), 2026, 10, covered=COVERED, as_of=AS_OF).kpi("orders")) == "up 20"


# ------------------------------------------------------------ titles carry the caveat

def test_a_title_that_states_a_change_names_the_missing_days_in_the_same_sentence():
    assert claim_title(report(), "net_sales") == "Net sales rose 50.0% from September (October is missing 2 days)"


def test_a_complete_month_has_no_caveat_in_its_title():
    r = report(covered=FULL)
    assert partial_clause(r) == ""
    assert claim_title(r, "net_sales") == "Net sales rose 50.0% from September"


def test_a_month_in_progress_and_a_previous_month_with_a_hole_are_named():
    mid = build_report(both_months(), 2026, 10, covered=FULL, as_of=date(2026, 10, 10))
    assert partial_clause(mid) == "only 9 of 31 days of October are counted"
    hole = report(covered=[(date(2026, 9, 1), date(2026, 9, 9)), (date(2026, 9, 10), date(2026, 11, 1))])
    assert partial_clause(hole) == "September is missing 1 day"


def test_a_title_without_a_previous_month_says_so_and_keeps_the_caveat():
    r = build_report(october_rows(), 2026, 10, covered=OCTOBER_COVERED, as_of=AS_OF)
    assert claim_title(r, "net_sales") == "Net sales came to $210.00 in October (October is missing 2 days)"


def test_the_biggest_mover_is_the_largest_percent_and_gives_the_insights_title():
    r = report()
    assert largest_move(r).key == "refunds"
    assert insights_title(r) == "Refunds moved most, up 200.0% from September (October is missing 2 days)"


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


def test_dates_read_like_a_person_writes_them_and_a_list_says_how_many_it_cut():
    days = [date(2026, 10, d) for d in range(1, 16)]
    assert days_text(days).endswith("and 5 more")
    assert days_text([]) == "none"
    assert days_text(days[:2]) == "Oct 1, Oct 2"
    assert fmt_day(date(2026, 3, 9)) == "Mar 9"


def test_plurals():
    assert plural(1, "day") == "1 day" and plural(2, "day") == "2 days" and plural(0, "day") == "0 days"
