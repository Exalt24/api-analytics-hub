"""The website half of the monthly report, over hand-made rows, with every expectation checkable with a pencil.

The store's rows are the ones in report_fixtures.py (September holds two days, October two days and a two
day hole, 12 and 8 orders in October, 10 and 5 in September). The website rows below are laid out the same
way and are small on purpose:

  September  day 1  sessions 200  users 150  page views 600  key events 10
             day 2  sessions 100  users  80  page views 250  key events  5
  October    day 1  sessions 250  users 180  page views 700  key events 12
             day 2  sessions 160  users 120  page views 400  key events  8

The website was synced over all of September, and over October except the 5th and 6th, which no run covers.
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.report.monthly import ReportError, build_report  # noqa: E402
from app.report.web import (  # noqa: E402
    WEB_LABELS,
    attach_web,
    build_web,
    joint_days,
    web_claim_title,
    web_month,
    web_partial_clause,
)
from report_fixtures import AS_OF, COVERED, both_months  # noqa: E402


def web_day(day: date, sessions: int, users: int, page_views: int, key_events: int):
    return [{"observed_on": day, "metric_key": k, "value_numeric": v, "currency": None}
            for k, v in (("sessions", sessions), ("users", users), ("page_views", page_views), ("key_events", key_events))]


SEPT = web_day(date(2026, 9, 1), 200, 150, 600, 10) + web_day(date(2026, 9, 2), 100, 80, 250, 5)
OCT = web_day(date(2026, 10, 1), 250, 180, 700, 12) + web_day(date(2026, 10, 2), 160, 120, 400, 8)
WEB_ROWS = SEPT + OCT
WEB_SEPT_COVERED = [(date(2026, 9, 1), date(2026, 10, 1))]
#: October 5 and 6 are not inside any window
WEB_COVERED = WEB_SEPT_COVERED + [(date(2026, 10, 1), date(2026, 10, 5)), (date(2026, 10, 7), date(2026, 11, 1))]


def store_report(as_of=AS_OF):
    return build_report(both_months(), 2026, 10, label="Store", covered=COVERED, as_of=as_of)


def web_report(rows=WEB_ROWS, covered=WEB_COVERED, as_of=AS_OF):
    """Both sources are read as of the same day, as the command does."""
    return build_web(rows, store_report(as_of), label="Site", covered=covered, as_of=as_of)


# ---------------------------------------------------------------- one month

def test_a_month_of_website_rows_is_classified_like_the_store_and_totalled():
    cur = web_month(WEB_ROWS, 2026, 10, covered=WEB_COVERED, as_of=AS_OF)
    assert cur.data_days == [date(2026, 10, 1), date(2026, 10, 2)]
    assert cur.missing_days == [date(2026, 10, 5), date(2026, 10, 6)]
    assert len(cur.quiet_days) == 31 - 2 - 2 and cur.reported_days == 29
    assert cur.totals == {"sessions": 410, "page_views": 1100, "key_events": 20}
    assert cur.rows_used == 8


def test_users_are_a_mean_of_the_days_with_data_and_never_a_sum_over_days():
    cur = web_month(WEB_ROWS, 2026, 10, covered=WEB_COVERED, as_of=AS_OF)
    assert cur.users_per_active_day == Decimal("150.0") and cur.users_exact == Decimal(150)
    assert "users" not in cur.totals, "distinct counts cannot be added across days"
    odd = web_month(web_day(date(2026, 10, 1), 1, 1, 1, 0) + web_day(date(2026, 10, 2), 1, 2, 1, 0)
                    + web_day(date(2026, 10, 3), 1, 2, 1, 0), 2026, 10, covered=WEB_COVERED, as_of=AS_OF)
    assert odd.users_exact == Decimal(5) / 3 and odd.users_per_active_day == Decimal("1.7"), "rounded half up from the exact mean"


def test_rows_of_other_metrics_are_ignored_and_a_second_row_for_a_day_is_refused():
    other = [{"observed_on": date(2026, 10, 1), "metric_key": "orders", "value_numeric": 99, "currency": None}]
    assert web_month(WEB_ROWS + other, 2026, 10, covered=WEB_COVERED, as_of=AS_OF).totals["sessions"] == 410
    with pytest.raises(ReportError, match="two rows"):
        web_month(OCT + web_day(date(2026, 10, 1), 1, 1, 1, 1), 2026, 10, covered=WEB_COVERED, as_of=AS_OF)


# ---------------------------------------------------------------- the KPI table

def test_every_website_figure_and_its_change_on_the_month_before():
    web = web_report()
    rows = {k.key: k for k in web.kpis}
    assert [k.label for k in web.kpis] == [WEB_LABELS[k] for k in ("sessions", "users_per_day", "page_views", "key_events", "order_rate")]
    s = rows["sessions"]
    assert (s.current_display, s.previous_display, s.change_display, s.pct_display) == ("410", "300", "+110", "+36.7%")
    u = rows["users_per_day"]
    assert (u.current_display, u.previous_display, u.change_display, u.pct_display) == ("150.0", "115.0", "+35.0", "+30.4%")
    p = rows["page_views"]
    assert (p.current_display, p.previous_display, p.change_display, p.pct_display) == ("1,100", "850", "+250", "+29.4%")
    k = rows["key_events"]
    assert (k.current_display, k.previous_display, k.change_display, k.pct_display) == ("20", "15", "+5", "+33.3%")


def test_the_order_rate_uses_only_days_both_sources_reported_and_is_not_the_ratio_of_the_months_totals():
    web = web_report()
    # store reported every October day but the 10th and 11th, the website all but the 5th and 6th: 27 joint days
    shop = store_report().current
    assert len(joint_days(shop, web.current)) == 27 and web.joint_current == 27
    rate = web.kpi("order_rate")
    # 20 orders over 410 sessions is 4.878 percent, shown 4.9; September 15 over 300 is 5.0
    assert (rate.current_display, rate.previous_display, rate.change_display, rate.pct_display) == ("4.9%", "5.0%", "-0.1 pts", "-2.4%")
    assert rate.per_day is None, "a rate is not a total, so it has no per reported day figure"


def test_a_total_in_a_partial_month_gets_the_change_per_reported_day_and_a_mean_does_not():
    web = web_report()
    s = web.kpi("sessions")
    assert s.notes == ("partial data",) and web.kpi("users_per_day").per_day is None
    assert (s.per_day.current_display, s.per_day.previous_display, s.per_day.pct_display) == ("14.14", "10.00", "+41.4%")
    assert web.kpi("page_views").per_day is not None and web.kpi("key_events").per_day is not None
    assert web_partial_clause(web) == "October is missing 2 days of website data"


def test_a_complete_month_has_no_partial_note_and_no_per_day_figure():
    full = [(date(2026, 9, 1), date(2026, 10, 1)), (date(2026, 10, 1), date(2026, 11, 1))]
    web = web_report(covered=full)
    assert web.current.missing_days == [] and all(k.notes == () for k in web.kpis[:4])
    assert all(k.per_day is None for k in web.kpis) and web_partial_clause(web) == ""
    assert web_claim_title(web) == "Sessions rose 36.7% from September"


def test_a_month_in_progress_is_partial_and_says_how_many_days_are_counted():
    web = web_report(as_of=date(2026, 10, 20))
    assert web.current.days_expected == 19 and not web.current.complete
    assert "only 19 of 31 days of October are counted" in web_partial_clause(web)
    assert "partial data" in web.kpi("sessions").notes


def test_no_earlier_month_stored_means_no_change_and_says_so():
    web = web_report(rows=OCT, covered=[(date(2026, 10, 1), date(2026, 11, 1))])
    assert web.previous is None
    s = web.kpi("sessions")
    assert (s.previous_display, s.change_display, s.pct_display) == ("n/a", "n/a", "n/a") and "no previous month" in s.notes
    assert web_claim_title(web) == "Sessions came to 410 in October"


def test_a_previous_month_that_was_synced_and_found_nothing_is_a_zero_baseline_not_a_missing_one():
    web = web_report(rows=OCT, covered=WEB_COVERED)         # September covered, no September rows
    s = web.kpi("sessions")
    assert s.previous_display == "0" and s.change_display == "+410" and s.pct_display == "n/a" and "zero baseline" in s.notes
    assert web.kpi("order_rate").previous_display == "n/a", "no sessions last month, so no rate to compare with"
    assert "no previous value" in web.kpi("order_rate").notes


def test_a_website_with_no_rows_and_no_sync_has_no_figures_which_is_not_zero():
    web = web_report(rows=[], covered=[])
    assert web.current.has_baseline is False and web.weeks == []
    assert all(k.current_display == "n/a" and "not available" in k.notes for k in web.kpis)
    assert len(web.current.missing_days) == 31
    assert web_claim_title(web) == "Website figures are not available for October"


def test_the_store_figures_are_untouched_by_attaching_the_website():
    before = store_report()
    snapshot = [(k.key, k.current_display, k.previous_display, k.change_display, k.pct_display, k.notes) for k in before.kpis]
    attach_web(before, WEB_ROWS, label="Site", covered=WEB_COVERED, as_of=AS_OF)
    assert before.web is not None
    assert snapshot == [(k.key, k.current_display, k.previous_display, k.change_display, k.pct_display, k.notes) for k in before.kpis]


# ---------------------------------------------------------------- sessions against orders

def test_the_weeks_count_only_days_both_sources_reported_and_say_how_many():
    weeks = web_report().weeks
    assert [w.label for w in weeks] == ["Oct 1 to 7", "Oct 8 to 14", "Oct 15 to 21", "Oct 22 to 28", "Oct 29 to 31"]
    first = weeks[0]
    # days 1, 2, 3, 4 and 7 are in both: the 5th and 6th have no website data
    assert (first.days, first.sessions, first.orders, first.rate) == ("5 of 7", "410", "20", "4.9%")
    # the store has no data on the 10th and 11th, so 5 of 7, and no sessions at all that week
    assert (weeks[1].days, weeks[1].sessions, weeks[1].orders, weeks[1].rate) == ("5 of 7", "0", "0", "n/a")
    assert weeks[-1].days == "3 of 3"


def test_a_week_with_no_day_in_both_sources_prints_n_a_not_zero():
    # no October rows and no run before the 8th: nothing was reported in the first week
    web = web_report(rows=SEPT, covered=[(date(2026, 9, 1), date(2026, 10, 1)), (date(2026, 10, 8), date(2026, 11, 1))])
    first = web.weeks[0]
    assert (first.days, first.sessions, first.orders, first.rate) == ("0 of 7", "n/a", "n/a", "n/a")


def test_a_month_in_progress_has_only_the_weeks_that_are_over():
    weeks = web_report(as_of=date(2026, 10, 10)).weeks
    assert [w.label for w in weeks] == ["Oct 1 to 7", "Oct 8 to 9"]


def test_the_date_labels_never_print_a_year_or_an_iso_date():
    for w in web_report().weeks:
        assert "2026" not in w.label and "-" not in w.label
