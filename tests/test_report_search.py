"""The search half of the monthly report, against figures worked out by hand (tests/sources_fixtures.py).

What is pinned here: the sums, the click through rate from the sums, the average position weighted by impressions
(and not an average of daily averages), the top queries and pages added up over the month from each day's top
rows, the days a month has no data for, the provisional days by arithmetic, and the refusals.
"""
from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.report.monthly import ReportError, build_report  # noqa: E402
from app.report.search import KEY_WIDTH, LAG_DAYS, TOP_SHOWN, attach_search, search_claim_title, search_month, top_rows  # noqa: E402
from sources_fixtures import (  # noqa: E402
    AS_OF,
    COVERED,
    SEARCH_COVERED,
    both_months,
    search_both,
    search_day,
    top,
    top_october,
)
from test_report_paid import half_up  # noqa: E402


def store():
    return build_report(both_months(), 2026, 10, label="Store", covered=COVERED, as_of=AS_OF)


def search(rows=None, tops=None, covered=SEARCH_COVERED, as_of=AS_OF):
    return attach_search(store(), search_both() if rows is None else rows, top_october() if tops is None else tops,
                         label="Search", covered=covered, as_of=as_of).search


def shown(report, key):
    row = report.kpi(key)
    return row.current_display, row.previous_display, row.change_display, row.pct_display


# ---------------------------------------------------------------- the figures

def test_the_kpi_table_matches_the_hand_computed_figures():
    s = search()
    assert shown(s, "search_clicks") == ("20", "30", "-10", "-33.3%")
    assert shown(s, "search_impressions") == ("200", "300", "-100", "-33.3%")
    assert shown(s, "ctr") == ("10.00%", "10.00%", "0.00 pts", "0.0%")
    assert shown(s, "position") == ("6.0", "6.5", "-0.5", "-7.7%")
    assert [k.key for k in s.kpis] == ["search_clicks", "search_impressions", "ctr", "position"]
    assert s.kpi("ctr").unchanged, "both months are exactly 10 percent"


def test_position_is_weighted_by_impressions_not_an_average_of_daily_averages():
    """A day of 10 impressions at position 1 and a day of 10,000 at position 10: the month is 9.99, not 5.5."""
    rows = search_day(date(2026, 10, 1), 1, 10, 1000) + search_day(date(2026, 10, 2), 100, 10000, 10000)
    assert search(rows).kpi("position").current_display == "10.0"
    assert search(rows).current.position_exact == Decimal(1000 * 10 + 10000 * 10000) / 10010 / 1000


def test_the_click_through_rate_comes_from_the_sums_not_from_an_average_of_daily_rates():
    rows = search_day(date(2026, 10, 1), 1, 1000, 5000) + search_day(date(2026, 10, 2), 90, 100, 5000)
    assert search(rows).kpi("ctr").current_display == "8.27%"


@pytest.mark.parametrize("rows,key,expected", [
    (search_day(date(2026, 10, 1), 0, 0, 0), "ctr", "n/a"),                     # no impressions: a rate of nothing
    (search_day(date(2026, 10, 1), 0, 0, 0), "position", "n/a"),                # no weight: a position of nothing
    (search_day(date(2026, 10, 1), 0, 10, 4000), "ctr", "0.00%"),               # impressions and no clicks is a real zero rate
    (search_day(date(2026, 10, 1), 0, 10, 4000), "search_clicks", "0"),
])
def test_a_zero_denominator_is_not_available_and_a_real_zero_is_a_zero(rows, key, expected):
    assert search(rows).kpi(key).current_display == expected


def test_a_month_with_neither_rows_nor_a_run_has_no_figures_and_is_not_a_zero():
    s = search([], [], covered=[])
    assert not s.current.has_baseline and s.previous is None and s.top_queries == [] and s.provisional == []
    assert all(k.current is None and k.current_display == "n/a" for k in s.kpis)
    assert "not available" in search_claim_title(s)


def test_attaching_search_leaves_every_store_figure_exactly_as_it_was():
    plain = store()
    attached = attach_search(store(), search_both(), top_october(), covered=SEARCH_COVERED, as_of=AS_OF)
    assert [(k.key, k.current_display, k.change_display, k.notes) for k in plain.kpis] == \
           [(k.key, k.current_display, k.change_display, k.notes) for k in attached.kpis]


def test_a_partial_month_gives_a_total_its_change_per_reported_day_and_a_position_does_not():
    s = search()
    assert s.kpi("search_clicks").per_day is not None and s.kpi("position").per_day is None and s.kpi("ctr").per_day is None
    assert [d.day for d in s.current.missing_days] == [10, 11]
    assert search_claim_title(s) == ("October search clicks total is 33.3% below September, but October is missing 2 days of search data: "
                                     "per reported day it is down 31.0%")


def test_figures_with_a_fractional_or_negative_value_are_refused_as_the_store_s_are():
    bad = search_day(date(2026, 10, 1), 1, 2, 3000)
    bad[0] = dict(bad[0], value_numeric=1.5)
    with pytest.raises(ReportError):
        search(bad)
    with pytest.raises(ReportError):
        search(search_both() + search_day(date(2026, 10, 1), 1, 2, 3000))


# ---------------------------------------------------------------- the top rows

def test_top_queries_and_pages_add_each_days_top_rows_over_the_month_and_rank_by_clicks():
    s = search()
    assert [(r.key, r.clicks, r.impressions, r.ctr, r.position) for r in s.top_queries] == [
        ("shoes", 9, 80, "11.25%", "5.5"),        # position (5.0 * 60 + 7.0 * 20) / 80
        ("socks", 5, 30, "16.67%", "4.0"),
        ("boots", 4, 40, "10.00%", "10.0"),
    ]
    assert [(r.key, r.clicks, r.impressions, r.position) for r in s.top_pages] == [("/a", 14, 140, "6.7"), ("/b", 4, 10, "2.0")]
    assert s.top_row_days == 2


def test_ties_on_clicks_break_by_impressions_then_key_so_the_order_is_the_same_every_time():
    d = date(2026, 10, 1)
    rows = [top(d, "query", "b", 5, 50, 1000), top(d, "query", "a", 5, 50, 1000), top(d, "query", "c", 5, 90, 1000),
            top(d, "query", "z", 1, 1, 1000)]
    first = [r.key for r in top_rows(rows, search().current, "query")]
    assert first == ["c", "a", "b", "z"]
    assert [r.key for r in top_rows(list(reversed(rows)), search().current, "query")] == first


def test_only_the_asked_dimension_and_the_counted_days_of_the_month_are_added():
    cur = search().current
    rows = [top(date(2026, 10, 1), "query", "q", 1, 2, 1000), top(date(2026, 10, 1), "page", "/p", 7, 8, 1000),
            top(date(2026, 9, 30), "query", "q", 100, 200, 1000),        # the month before
            top(date(2026, 11, 1), "query", "q", 100, 200, 1000)]        # the month after
    assert [(r.key, r.clicks) for r in top_rows(rows, cur, "query")] == [("q", 1)]
    assert [(r.key, r.clicks) for r in top_rows(rows, cur, "page")] == [("/p", 7)]


def test_a_month_in_progress_leaves_out_top_rows_of_days_not_yet_counted():
    s = search(as_of=date(2026, 10, 2), tops=[top(date(2026, 10, 1), "query", "q", 1, 2, 1000), top(date(2026, 10, 2), "query", "later", 9, 9, 1000)],
               covered=[(date(2026, 9, 1), date(2026, 10, 2))])
    assert [r.key for r in s.top_queries] == ["q"]


def test_the_list_is_cut_to_five_and_a_long_key_is_cut_to_fit_without_changing_the_figures():
    d = date(2026, 10, 1)
    rows = [top(d, "query", "q%d" % i, 10 - i, 100, 1000) for i in range(8)]
    rows.append(top(d, "query", "a very long query that people really did type into a search box one day", 20, 100, 1000))
    rows.append(top(d, "query", "line\nbreak   and  spaces", 15, 100, 1000))
    got = top_rows(rows, search().current, "query")
    assert TOP_SHOWN == 5 and len(got) == 5
    assert got[0].key.startswith("a very long query") and got[0].shown_key == "a very long...rch box one day" and len(got[0].shown_key) <= KEY_WIDTH
    assert got[0].clicks == 20 and got[1].shown_key == "line break and spaces" and "\n" not in got[1].shown_key


def test_a_key_with_every_odd_character_survives_in_the_key_and_a_cut_never_changes_it():
    d = date(2026, 10, 1)
    odd = ["café ☕", "<script>", "a/b?c=d&e", "日本語の語" * 10]
    got = top_rows([top(d, "query", k, 1, 2, 1000) for k in odd], search().current, "query")
    assert {r.key for r in got} == set(odd) and all(len(r.shown_key) <= KEY_WIDTH for r in got)


def test_no_top_rows_is_an_empty_list_not_an_error():
    assert search(tops=[]).top_queries == [] and search(tops=[]).top_row_days == 0


# ---------------------------------------------------------------- provisional days

def test_the_last_days_before_the_report_date_are_provisional_by_arithmetic():
    assert LAG_DAYS == 3
    assert [d.day for d in search(as_of=date(2026, 11, 2)).provisional] == [30, 31]
    assert [d.day for d in search(as_of=date(2026, 11, 3)).provisional] == [31]
    assert search(as_of=date(2026, 11, 4)).provisional == []
    assert search().provisional == []


def test_a_missing_day_is_never_provisional():
    s = search(as_of=date(2026, 10, 12))
    assert [d.day for d in s.provisional] == [9]
    assert not set(s.provisional) & set(s.current.missing_days)


# ---------------------------------------------------------------- the same figures from an independent oracle

@pytest.mark.parametrize("seed", range(60))
def test_over_random_months_every_figure_equals_an_independent_exact_oracle(seed):
    rng = random.Random(seed)
    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(31)]
    chosen = rng.sample(days, rng.randint(1, 12))
    rows, clk, imp, weighted = [], 0, 0, 0
    for d in chosen:
        impressions = rng.randint(0, 4000)
        clicks = rng.randint(0, impressions)
        position = rng.randint(0, 60000)
        rows += search_day(d, clicks, impressions, position)
        clk += clicks
        imp += impressions
        weighted += position * impressions
    s = search(rows, [], covered=[(date(2026, 10, 1), date(2026, 11, 1))])
    assert (s.current.totals["search_clicks"], s.current.totals["search_impressions"]) == (clk, imp)
    assert s.kpi("ctr").current == (None if imp == 0 else half_up(Fraction(clk * 100, imp), 2))
    expected_position = None if imp == 0 else half_up(Fraction(weighted, imp * 1000), 1)
    assert s.kpi("position").current == expected_position
    assert search_month(rows, 2026, 10, covered=[(date(2026, 10, 1), date(2026, 11, 1))], as_of=AS_OF).position_shown == expected_position


# ---------------------------------------------------------------- lines the derived guards found untested

def test_the_partial_clause_is_the_phrase_the_title_and_the_narrative_share():
    from app.report.search import search_partial_clause
    assert search_partial_clause(search()) == "October is missing 2 days of search data"
    assert search_partial_clause(search(covered=[(date(2026, 9, 1), date(2026, 11, 1))])) == ""


@pytest.mark.parametrize("key,value,signed,expected", [
    ("search_clicks", 1234, False, "1,234"), ("search_clicks", 1, True, "+1"), ("search_clicks", -3, True, "-3"), ("search_clicks", 0, True, "0"),
    ("ctr", Decimal("4.24"), False, "4.24%"), ("ctr", Decimal("0.12"), True, "+0.12 pts"), ("ctr", Decimal("-0.12"), True, "-0.12 pts"),
    ("ctr", Decimal("0.00"), True, "0.00 pts"), ("position", Decimal("8.6"), False, "8.6"), ("position", Decimal("1234.5"), False, "1,234.5"),
    ("position", Decimal("1"), True, "+1"), ("position", Decimal("-0.8"), True, "-0.8"), ("position", None, False, "n/a"),
])
def test_each_search_figure_is_written_the_way_its_row_prints_it(key, value, signed, expected):
    from app.report.search import show_search
    assert show_search(key, value, signed=signed) == expected
    if not signed:
        assert show_search(key, value) == expected


# ---------------------------------------------------------------- the last survivors of the derived guards

def _row(day, metric, value):
    return {"observed_on": day, "metric_key": metric, "value_numeric": value, "currency": None}


def test_a_day_that_carries_only_some_of_the_figures_adds_nothing_for_the_others():
    d1, d2 = date(2026, 10, 1), date(2026, 10, 2)
    rows = search_day(d1, 12, 150, 5000) + [_row(d2, "search_impressions", 50)]          # day 2 has no clicks and no position
    month = search_month(rows, 2026, 10, covered=SEARCH_COVERED, as_of=AS_OF)
    assert month.totals == {"search_clicks": 12, "search_impressions": 200}
    assert month.position_exact == Decimal(5)                                             # day 2 has no position to weigh
    only_position = search_day(d1, 12, 150, 5000) + [_row(d2, "search_position", 9000)]   # a position with no impressions has no weight
    month = search_month(only_position, 2026, 10, covered=SEARCH_COVERED, as_of=AS_OF)
    assert month.position_exact == Decimal(5) and month.totals["search_impressions"] == 150


def test_a_month_with_no_impressions_has_no_average_position():
    month = search_month([_row(date(2026, 10, 1), "search_clicks", 0), _row(date(2026, 10, 1), "search_impressions", 0),
                          _row(date(2026, 10, 1), "search_position", 3000)], 2026, 10, covered=SEARCH_COVERED, as_of=AS_OF)
    assert month.position_exact is None and month.position_shown is None


def test_a_top_row_is_frozen_so_a_figure_shown_cannot_be_changed_after_it_was_computed():
    import dataclasses
    from app.report.search import TopRow
    row = TopRow("a", "a", 1, 2, "50.00%", "1.0")
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.clicks = 5


def test_the_top_rows_are_added_over_the_days_and_their_figures_are_exact():
    stats = search_month([], 2026, 10, as_of=AS_OF)
    d1, d2 = date(2026, 10, 1), date(2026, 10, 2)
    rows = [top(d1, "query", "a", 3, 30, 2000), top(d2, "query", "a", 1, 10, 6000)]
    out = top_rows(rows, stats, "query")
    assert len(out) == 1
    one = out[0]
    assert (one.key, one.clicks, one.impressions, one.ctr, one.position) == ("a", 4, 40, "10.00%", "3.0")


def test_ties_on_clicks_go_to_more_impressions_then_to_the_key_and_never_to_the_position_sum():
    stats = search_month([], 2026, 10, as_of=AS_OF)
    d1 = date(2026, 10, 1)
    rows = [top(d1, "query", "x", 5, 50, 9000), top(d1, "query", "y", 5, 80, 1000), top(d1, "query", "a", 5, 80, 1000),
            top(d1, "query", "z", 6, 10, 1000)]
    assert [r.key for r in top_rows(rows, stats, "query")] == ["z", "a", "y", "x"]
    assert [r.key for r in top_rows(rows, stats, "query", limit=2)] == ["z", "a"]


def test_a_key_with_no_impressions_has_no_rate_and_no_position_and_a_big_position_has_a_thousands_comma():
    stats = search_month([], 2026, 10, as_of=AS_OF)
    d1 = date(2026, 10, 1)
    zero = top_rows([top(d1, "query", "none", 0, 0, 0)], stats, "query")[0]
    assert (zero.ctr, zero.position) == ("n/a", "n/a")
    big = top_rows([top(d1, "query", "deep", 1, 1, 1234500)], stats, "query")[0]
    assert big.position == "1,234.5" and big.ctr == "100.00%"


def test_rows_of_another_dimension_or_outside_the_month_are_not_counted():
    stats = search_month([], 2026, 10, as_of=AS_OF)
    rows = [top(date(2026, 10, 1), "page", "/a", 9, 9, 1000), top(date(2026, 9, 30), "query", "old", 9, 9, 1000),
            top(date(2026, 11, 1), "query", "new", 9, 9, 1000), top(date(2026, 10, 31), "query", "last", 2, 9, 1000),
            top(date(2026, 10, 1), "query", "first", 1, 9, 1000)]
    assert {r.key for r in top_rows(rows, stats, "query")} == {"last", "first"}


def test_a_month_nobody_synced_shows_no_top_row_days_and_no_figures():
    s = search(rows=[], tops=[], covered=())
    assert s.top_row_days == 0 and s.top_queries == [] and s.top_pages == []
    assert s.kpi("search_clicks").current_display == "n/a"


@pytest.mark.parametrize("key,value,signed,expected", [
    ("ctr", Decimal("1234.50"), False, "1,234.50%"), ("ctr", Decimal("1234.50"), True, "+1,234.50 pts"),
    ("position", Decimal("1234.5"), False, "1,234.5"), ("position", Decimal("1234.5"), True, "+1,234.5"),
    ("position", Decimal("-0.5"), True, "-0.5"), ("search_clicks", 1234567, False, "1,234,567"),
])
def test_figures_over_a_thousand_carry_the_comma_in_every_form(key, value, signed, expected):
    from app.report.search import show_search
    assert show_search(key, value, signed=signed) == expected


def test_a_top_key_position_is_rounded_half_up_from_the_exact_weighted_figure_with_nothing_added():
    stats = search_month([], 2026, 10, as_of=AS_OF)
    d1 = date(2026, 10, 1)
    assert top_rows([top(d1, "query", "k", 1, 1, 1449)], stats, "query")[0].position == "1.4"      # 1.449 is below the half
    assert top_rows([top(d1, "query", "k", 1, 1, 1450)], stats, "query")[0].position == "1.5"      # 1.450 rounds up
