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
    assert got[0].key.startswith("a very long query") and len(got[0].shown_key) == KEY_WIDTH and got[0].shown_key.endswith("...")
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
