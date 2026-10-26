"""The paid media half of the monthly report, against figures worked out by hand (tests/sources_fixtures.py).

What is pinned here: the sums, the rates computed from the sums (never an average of daily rates), the cost per
click rounded half up, the return on ad spend over the days that have both figures, a purchase figure that exists
only on some days, the ad account's currency kept apart from the store's, the days a month has no data for, the
provisional days by arithmetic, and the refusals (two currencies, a doubled day, a float).
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
from app.report.paid import LAG_DAYS, WINDOW_TEXT, attach_paid, paid_claim_title  # noqa: E402
from app.connectors.meta import ATTRIBUTION_WINDOW  # noqa: E402
from sources_fixtures import (  # noqa: E402
    AS_OF,
    COVERED,
    PAID_COVERED,
    both_months,
    paid_both,
    paid_day,
    paid_october,
    paid_september,
)


def store():
    return build_report(both_months(), 2026, 10, label="Store", covered=COVERED, as_of=AS_OF)


def paid(rows=None, covered=PAID_COVERED, as_of=AS_OF):
    return attach_paid(store(), paid_both() if rows is None else rows, label="Ads", covered=covered, as_of=as_of).paid


def shown(report, key):
    row = report.kpi(key)
    return row.current_display, row.previous_display, row.change_display, row.pct_display


# ---------------------------------------------------------------- the figures

def test_the_kpi_table_matches_the_hand_computed_figures():
    p = paid()
    assert shown(p, "ad_spend") == ("EUR 180.00", "EUR 150.00", "+EUR 30.00", "+20.0%")
    assert shown(p, "ad_impressions") == ("1,600", "1,500", "+100", "+6.7%")
    assert shown(p, "ad_clicks") == ("70", "70", "0", "0.0%")
    assert shown(p, "ctr") == ("4.38%", "4.67%", "-0.29 pts", "-6.3%")           # 4.375 rounds half up; the change is from the exact rates
    assert shown(p, "cpc") == ("EUR 2.57", "EUR 2.14", "+EUR 0.43", "+20.0%")
    assert shown(p, "ad_purchases") == ("6", "7", "-1", "-14.3%")
    assert shown(p, "ad_purchase_value") == ("EUR 540.00", "EUR 500.00", "+EUR 40.00", "+8.0%")
    assert shown(p, "roas") == ("4.50", "3.33", "+1.17", "+35.0%")
    assert [k.key for k in p.kpis] == ["ad_spend", "ad_impressions", "ad_clicks", "ctr", "cpc", "ad_purchases", "ad_purchase_value", "roas"]


def test_a_rate_is_computed_from_the_sums_and_never_as_an_average_of_daily_rates():
    """Day one clicks 1 of 1000 (0.1 percent), day two 90 of 100 (90 percent). The month is 91 of 1100, not 45.05 percent."""
    rows = paid_day(date(2026, 10, 1), 20000, 1000, 1) + paid_day(date(2026, 10, 2), 20000, 100, 90)
    p = paid(rows)
    assert p.kpi("ctr").current_display == "8.27%"
    assert p.kpi("cpc").current_display == "EUR 4.40", "40000 minor units over 91 clicks is 439.56, kept to the minor unit"


def test_the_clicks_are_unchanged_when_the_exact_values_are_equal_and_flat_is_not_the_same_thing():
    p = paid()
    assert p.kpi("ad_clicks").unchanged and not p.kpi("ad_clicks").flat


def test_the_return_on_ad_spend_uses_only_the_days_that_have_both_spend_and_a_purchase_value():
    """October day two has spend 6000 and no purchase value. Dividing 54000 by all 18000 of spend would say 3.00."""
    p = paid()
    assert p.current.joint_days == 1 and p.current.joint_spend == 12000 and p.current.joint_value == 54000
    assert p.kpi("roas").current_display == "4.50"
    more = paid(paid_both() + paid_day(date(2026, 10, 3), 999999, 5, 1))
    assert more.kpi("roas").current_display == "4.50", "a day with spend and no purchase figure must not pull the ratio down"


def test_purchases_cover_only_the_days_that_carry_them_and_the_report_says_how_many():
    p = paid()
    assert p.current.purchase_days == 1 and p.current.value_days == 1 and p.previous.purchase_days == 2
    assert p.kpi("ad_purchases").current_display == "6"


def test_a_month_with_no_purchase_figures_at_all_shows_not_available_never_zero():
    rows = paid_september() + paid_day(date(2026, 10, 1), 12000, 1200, 60) + paid_day(date(2026, 10, 2), 6000, 400, 10)
    p = paid(rows)
    for key in ("ad_purchases", "ad_purchase_value", "roas"):
        row = p.kpi(key)
        assert row.current is None and row.current_display == "n/a" and "not available" in row.notes
        assert row.change is None and row.change_display == "n/a" and row.pct_display == "n/a"
    assert "Purchases" in p.kpi("ad_purchases").label


def test_a_real_zero_purchase_count_is_a_zero_and_a_zero_baseline_has_no_percent():
    rows = paid_day(date(2026, 9, 1), 100, 1000, 10, 0, 0) + paid_day(date(2026, 10, 1), 100, 1000, 10, 4, 400)
    p = paid(rows)
    assert p.previous.purchase_days == 1 and p.kpi("ad_purchases").previous_display == "0"
    row = p.kpi("ad_purchases")
    assert row.pct is None and row.pct_display == "n/a" and "zero baseline" in row.notes and row.change_display == "+4"
    roas = p.kpi("roas")
    assert roas.previous_display == "0.00" and roas.current_display == "4.00" and roas.pct is None and "zero baseline" in roas.notes


@pytest.mark.parametrize("rows,key,expected", [
    (paid_day(date(2026, 10, 1), 500, 0, 0), "ctr", "n/a"),                       # no impressions: a rate of nothing
    (paid_day(date(2026, 10, 1), 500, 100, 0), "cpc", "n/a"),                     # no clicks: a cost per nothing
    (paid_day(date(2026, 10, 1), 0, 100, 5, 1, 0), "roas", "n/a"),                # no spend: a return on nothing
    (paid_day(date(2026, 10, 1), 0, 100, 5), "ad_spend", "EUR 0.00"),
])
def test_a_ratio_with_a_zero_denominator_is_not_available_and_a_real_zero_amount_is_a_zero(rows, key, expected):
    assert paid(rows).kpi(key).current_display == expected


def test_a_money_total_with_no_currency_row_prints_a_plain_zero_not_an_invented_currency():
    p = paid([])
    assert p.current.has_baseline and p.currency is None
    assert p.kpi("ad_spend").current_display == "0" and p.kpi("ad_spend").previous_display == "0"


# ---------------------------------------------------------------- the ad account's money is not the store's

def test_the_ad_currency_is_kept_apart_from_the_stores_and_never_mixed():
    report = attach_paid(store(), paid_both(), covered=PAID_COVERED, as_of=AS_OF)
    assert report.currency == "USD" and report.paid.currency == "EUR"
    assert report.kpi("net_sales").current_display.startswith("$") and report.paid.kpi("ad_spend").current_display.startswith("EUR ")
    assert report.kpi("net_sales").current_display == "$210.00", "the store's figures are untouched"


def test_attaching_ads_leaves_every_store_figure_exactly_as_it_was():
    plain = store()
    attached = attach_paid(store(), paid_both(), covered=PAID_COVERED, as_of=AS_OF)
    assert [(k.key, k.current_display, k.previous_display, k.change_display, k.pct_display, k.notes) for k in plain.kpis] == \
           [(k.key, k.current_display, k.previous_display, k.change_display, k.pct_display, k.notes) for k in attached.kpis]
    assert plain.top_days == attached.top_days and plain.web is None and attached.web is None


def test_two_currencies_inside_one_month_are_refused_not_converted():
    rows = paid_september() + paid_day(date(2026, 10, 1), 100, 10, 1) + paid_day(date(2026, 10, 2), 100, 10, 1, currency="USD")
    with pytest.raises(ReportError) as exc:
        paid(rows)
    assert "more than one currency" in str(exc.value)


def test_a_month_in_one_currency_against_a_month_in_another_cannot_be_compared():
    rows = paid_september() + paid_day(date(2026, 10, 1), 100, 10, 1, currency="GBP")
    with pytest.raises(ReportError) as exc:
        paid(rows)
    assert "cannot be compared" in str(exc.value) and "EUR" in str(exc.value) and "GBP" in str(exc.value)


def test_the_same_day_twice_a_float_and_money_without_a_currency_are_refused():
    with pytest.raises(ReportError):
        paid(paid_october() + paid_day(date(2026, 10, 1), 1, 1, 1))
    bad = paid_october()
    bad[0] = dict(bad[0], value_numeric=12000.5)
    with pytest.raises(ReportError):
        paid(bad)
    nocur = paid_october()
    nocur[0] = dict(nocur[0], currency=None)
    with pytest.raises(ReportError):
        paid(nocur)


# ---------------------------------------------------------------- days

def test_the_days_are_data_quiet_or_missing_for_the_ads_on_their_own_runs():
    p = paid()
    assert [d.day for d in p.current.data_days] == [1, 2] and len(p.current.quiet_days) == 27
    assert [d.day for d in p.current.missing_days] == [10, 11], "no ad run covers the 10th and 11th, so nobody looked"
    narrower = paid(covered=[(date(2026, 9, 1), date(2026, 10, 1))])
    assert len(narrower.current.missing_days) == 29, "with no October run every October day with no row is missing"


def test_a_month_with_neither_rows_nor_a_run_has_no_figures_and_is_not_a_zero():
    p = paid([], covered=[])
    assert not p.current.has_baseline and p.previous is None
    for row in p.kpis:
        assert row.current is None and row.current_display == "n/a" and row.notes[0] == "not available"
    assert p.provisional == [] and "not available" in paid_claim_title(p)


def test_a_partial_month_gives_a_total_its_change_per_reported_day_and_purchases_do_not():
    p = paid()
    spend = p.kpi("ad_spend")
    assert "partial data" in spend.notes and spend.per_day is not None
    assert (spend.per_day.current_display, spend.per_day.previous_display, spend.per_day.pct_display) == ("EUR 6.21", "EUR 5.00", "+24.1%")
    assert p.kpi("ad_purchases").per_day is None and p.kpi("ctr").per_day is None and p.kpi("roas").per_day is None
    assert paid_claim_title(p) == ("October ad spend total is 20.0% above September, but October is missing 2 days of ad data: "
                                   "per reported day it is up 24.1%")


def test_a_complete_month_has_the_plain_headline_and_no_per_day_figures():
    rows = paid_september() + paid_october()
    p = paid(rows, covered=[(date(2026, 9, 1), date(2026, 11, 1))])
    assert p.current.missing_days == [] and p.kpi("ad_spend").per_day is None
    assert paid_claim_title(p) == "Ad spend rose 20.0% from September"


def test_the_first_month_has_no_change_and_says_why():
    p = paid(paid_october(), covered=[(date(2026, 10, 1), date(2026, 11, 1))])
    row = p.kpi("ad_spend")
    assert p.previous is None and row.change is None and "no previous month" in row.notes and row.pct_display == "n/a"
    assert paid_claim_title(p) == "Ad spend came to EUR 180.00 in October"


# ---------------------------------------------------------------- provisional days

def test_the_last_days_before_the_report_date_are_provisional_by_arithmetic():
    assert LAG_DAYS == 7
    assert [d.day for d in paid(as_of=date(2026, 11, 2)).provisional] == [26, 27, 28, 29, 30, 31]
    assert paid(as_of=date(2026, 11, 7)).provisional == [date(2026, 10, 31)]
    assert paid(as_of=date(2026, 11, 8)).provisional == [], "more than a week after the month ended nothing is provisional"
    assert paid().provisional == []


def test_a_month_in_progress_marks_the_days_counted_so_far_and_never_a_missing_day():
    p = paid(as_of=date(2026, 10, 12))
    assert [d.day for d in p.provisional] == [5, 6, 7, 8, 9], "days 10 and 11 are missing, and a missing day is not provisional"
    assert not set(p.provisional) & set(p.current.missing_days)


def test_the_attribution_window_the_slide_names_is_the_one_the_connector_pins():
    assert ATTRIBUTION_WINDOW == "7d_click" and WINDOW_TEXT == ATTRIBUTION_WINDOW.replace("d_", " day ")


# ---------------------------------------------------------------- the same figures from an independent oracle

def half_up(value: Fraction, places: int) -> Decimal:
    scaled = value * 10 ** places
    n = scaled.numerator // scaled.denominator
    rem = scaled - n
    if rem >= Fraction(1, 2):
        n += 1
    return Decimal(n) / (10 ** places)


@pytest.mark.parametrize("seed", range(60))
def test_over_random_months_every_figure_equals_an_independent_exact_oracle(seed):
    rng = random.Random(seed)
    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(31)]
    chosen = rng.sample(days, rng.randint(1, 12))
    rows, truth = [], {"spend": 0, "imp": 0, "clk": 0, "pur": 0, "val": 0, "jspend": 0, "jval": 0, "pdays": 0}
    for d in chosen:
        spend, imp = rng.randint(0, 90000), rng.randint(0, 5000)
        clk = rng.randint(0, imp)
        pur = rng.choice([None, rng.randint(0, 40)])
        val = rng.choice([None, rng.randint(0, 900000)]) if pur is not None else None
        rows += paid_day(d, spend, imp, clk, pur, val)
        truth["spend"] += spend
        truth["imp"] += imp
        truth["clk"] += clk
        if pur is not None:
            truth["pur"] += pur
            truth["pdays"] += 1
        if val is not None:
            truth["val"] += val
        if val is not None or pur == 0:
            # a day with no purchases has a value of 0 though Meta leaves it out; a count above 0 with no value row stays out
            truth["jspend"] += spend
            truth["jval"] += val or 0
    p = paid(rows, covered=[(date(2026, 10, 1), date(2026, 11, 1))])
    cur = p.current
    assert (cur.totals["ad_spend"], cur.totals["ad_impressions"], cur.totals["ad_clicks"]) == (truth["spend"], truth["imp"], truth["clk"])
    ctr = None if truth["imp"] == 0 else half_up(Fraction(truth["clk"] * 100, truth["imp"]), 2)
    assert p.kpi("ctr").current == ctr
    cpc = None if truth["clk"] == 0 else int(half_up(Fraction(truth["spend"], truth["clk"]), 0))
    assert p.kpi("cpc").current == cpc
    roas = None if truth["jspend"] == 0 else half_up(Fraction(truth["jval"], truth["jspend"]), 2)
    assert p.kpi("roas").current == roas
    assert p.kpi("ad_purchases").current == (truth["pur"] if truth["pdays"] else None)
    assert all(isinstance(k.current, (int, Decimal)) or k.current is None for k in p.kpis)
    assert not [d for d in p.provisional if d in cur.missing_days]


# ---------------------------------------------------------------- lines the derived guards found untested

def test_impressions_and_clicks_are_totals_over_days_so_a_partial_month_gives_them_a_per_reported_day_change_too():
    p = paid()
    imp, clk = p.kpi("ad_impressions").per_day, p.kpi("ad_clicks").per_day
    assert (imp.current_display, imp.previous_display, imp.pct_display) == ("55.17", "50.00", "+10.3%")
    assert (clk.current_display, clk.previous_display, clk.pct_display) == ("2.41", "2.33", "+3.4%")
    from app.report.paid import PAID_TOTAL_KEYS
    assert PAID_TOTAL_KEYS == {"ad_spend", "ad_impressions", "ad_clicks"}


def test_the_partial_clause_is_the_phrase_the_title_and_the_narrative_share():
    from app.report.paid import paid_partial_clause
    assert paid_partial_clause(paid()) == "October is missing 2 days of ad data"
    complete = paid(paid_september() + paid_october(), covered=[(date(2026, 9, 1), date(2026, 11, 1))])
    assert paid_partial_clause(complete) == ""
    in_progress = paid(as_of=date(2026, 10, 12), covered=[(date(2026, 9, 1), date(2026, 10, 12))])
    assert paid_partial_clause(in_progress) == "only 11 of 31 days of October are counted"


@pytest.mark.parametrize("key,value,currency,signed,expected", [
    ("ad_spend", 1234, "EUR", False, "EUR 12.34"), ("ad_spend", 1234, "EUR", True, "+EUR 12.34"), ("ad_spend", -1234, "EUR", True, "-EUR 12.34"),
    ("ad_spend", 0, "EUR", True, "EUR 0.00"), ("ad_spend", 0, None, False, "0"), ("ad_spend", 5, None, False, "n/a"), ("cpc", 257, "USD", False, "$2.57"),
    ("ctr", Decimal("1.5"), None, False, "1.5%"), ("ctr", Decimal("0.29"), None, True, "+0.29 pts"), ("ctr", Decimal("-0.29"), None, True, "-0.29 pts"),
    ("ctr", Decimal("0.00"), None, True, "0.00 pts"), ("roas", Decimal("1234.5"), None, False, "1,234.5"), ("roas", Decimal("1"), None, True, "+1"),
    ("roas", Decimal("0"), None, True, "0"), ("ad_clicks", 1234567, None, False, "1,234,567"), ("ad_clicks", 1, None, True, "+1"),
    ("ad_clicks", -3, None, True, "-3"), ("ad_clicks", 0, None, True, "0"), ("ad_spend", None, "EUR", False, "n/a"),
    ("ctr", Decimal("1234.50"), None, False, "1,234.50%"), ("ctr", Decimal("1234.50"), None, True, "+1,234.50 pts"),
])
def test_each_figure_is_written_the_way_its_row_prints_it_by_default_and_signed(key, value, currency, signed, expected):
    from app.report.paid import show_paid
    assert show_paid(key, value, currency, signed=signed) == expected
    if not signed:
        assert show_paid(key, value, currency) == expected, "unsigned is the default"


def _complete(sept_clicks, oct_clicks, sept_spend=1000, oct_spend=1000):
    rows = paid_day(date(2026, 9, 1), sept_spend, 5000, sept_clicks) + paid_day(date(2026, 10, 1), oct_spend, 5000, oct_clicks)
    return paid(rows, covered=[(date(2026, 9, 1), date(2026, 11, 1))])


@pytest.mark.parametrize("sept,octo,key,title", [
    (50, 50, "ad_clicks", "Clicks did not change from September"),
    (100000, 100001, "ad_clicks", "Clicks stayed essentially flat (+1) from September"),
    (0, 50, "ad_clicks", "Clicks came to 50 in October, up 50 from September"),
    (80, 40, "ad_clicks", "Clicks fell 50.0% from September"),
    (80, 120, "ad_clicks", "Clicks rose 50.0% from September"),
])
def test_the_headline_of_a_complete_month_for_each_way_a_figure_can_move(sept, octo, key, title):
    assert paid_claim_title(_complete(sept, octo), key) == title


def test_a_previous_month_with_missing_days_is_named_in_the_clause_beside_this_months():
    from app.report.paid import paid_partial_clause
    p = paid(covered=[(date(2026, 9, 1), date(2026, 9, 28)), (date(2026, 10, 1), date(2026, 10, 10)), (date(2026, 10, 12), date(2026, 11, 1))])
    assert paid_partial_clause(p) == "October is missing 2 days of ad data and September is missing 3 days of ad data"
    assert paid_claim_title(p, "ad_spend") == ("October ad spend total is 20.0% above September, but October is missing 2 days of ad data "
                                                "and September is missing 3 days of ad data: per reported day it is up 11.7%")


def test_a_figure_the_month_before_did_not_report_says_there_is_no_previous_value_for_it():
    rows = (paid_day(date(2026, 9, 1), 1000, 5000, 50) + paid_day(date(2026, 10, 1), 1000, 5000, 50, 4, 8000))
    p = paid(rows, covered=[(date(2026, 9, 1), date(2026, 11, 1))])
    purchases = p.kpi("ad_purchases")
    assert purchases.previous is None and purchases.change is None and "no previous value" in purchases.notes and purchases.change_display == "n/a"
    assert p.kpi("ad_clicks").notes == ()


# ---------------------------------------------------------------- shared headline wording, with stand-in sources

def _stub(*, previous, pct, change, per_day=None, missing=(), unchanged=False, flat=False):
    from types import SimpleNamespace
    row = SimpleNamespace(key="clicks", label="Clicks", current=5, current_display="5", change=change, per_day=per_day, pct=pct,
                          pct_display=("+" if pct is not None and pct > 0 else "") + str(pct), unchanged=unchanged, flat=flat)
    cur = SimpleNamespace(short="Oct", missing_days=list(missing), complete=True, days_expected=31, days_in_month=31)
    prev = None if previous is None else SimpleNamespace(short="Sep", missing_days=[])
    return SimpleNamespace(current=cur, previous=prev, kpi=lambda key: row)


def test_a_headline_with_no_earlier_month_or_no_change_figure_only_states_the_value():
    from app.report.sources import claim_title
    keys = frozenset({"clicks"})
    no_prev = claim_title(_stub(previous=None, pct=Decimal(5), change=3), "clicks", "ad", keys, "n/a")
    assert no_prev == "Clicks came to 5 in Oct"
    no_change = claim_title(_stub(previous=True, pct=None, change=None), "clicks", "ad", keys, "n/a")
    assert no_change == "Clicks came to 5 in Oct"


def test_a_small_rise_is_called_a_rise_and_a_small_fall_a_fall():
    from app.report.sources import claim_title
    keys = frozenset()
    assert claim_title(_stub(previous=True, pct=Decimal("0.5"), change=1), "clicks", "ad", keys, "n/a") == "Clicks rose 0.5 from Sep"
    assert claim_title(_stub(previous=True, pct=Decimal("-0.5"), change=-1), "clicks", "ad", keys, "n/a") == "Clicks fell 0.5 from Sep"


def test_the_total_and_per_day_headline_needs_an_earlier_month_a_caveat_a_total_key_and_both_figures():
    from app.report.sources import states_total_with_per_day
    keys = frozenset({"clicks"})
    full = dict(previous=True, pct=Decimal(5), change=3, per_day=Decimal(2), missing=[date(2026, 10, 3)])
    assert states_total_with_per_day(_stub(**full), "clicks", "ad", keys)
    assert not states_total_with_per_day(_stub(**{**full, "previous": None}), "clicks", "ad", keys)
    assert not states_total_with_per_day(_stub(**{**full, "missing": []}), "clicks", "ad", keys)
    assert not states_total_with_per_day(_stub(**full), "clicks", "ad", frozenset())
    assert not states_total_with_per_day(_stub(**{**full, "per_day": None}), "clicks", "ad", keys)
    assert not states_total_with_per_day(_stub(**{**full, "pct": None}), "clicks", "ad", keys)


@pytest.mark.parametrize("web,paid,count,length,expected", [
    (None, None, 1, 5, (18, 8)), (1, None, 1, 5, (18, 8)), (1, None, 9, 5, (15, 5)),          # a store alone or with a website keeps its sizes
    (None, 1, 3, 389, (18, 4)), (None, 1, 8, 98, (16, 4)), (None, 1, 1, 1855, (14, 4)),
    (None, 1, 2, 1126, (13, 4)), (None, 1, 3, 805, (12, 4)), (None, 1, 2, 1597, (12, 3)),      # the largest size whose estimated wrap fits, else the smallest
])
def test_the_narrative_font_is_the_largest_that_fits_the_estimated_wrap(web, paid, count, length, expected):
    """Cases picked at the edges of each size's limit, so a changed constant in the estimate moves one of them."""
    from types import SimpleNamespace
    from app.report.deck import _insights_size
    report = SimpleNamespace(web=web, paid=paid, search=None)
    assert _insights_size(report, ["x" * length] * count) == expected


def test_a_long_list_of_provisional_dates_is_cut_with_others_never_with_a_count_the_figure_check_would_refuse():
    """The lag keeps real lists short, so this uses stand-ins for a source with twelve provisional days."""
    from types import SimpleNamespace
    from app.report.insights import _add_paid_facts, _add_search_facts
    days = [date(2026, 10, n) for n in range(1, 13)]
    cur = SimpleNamespace(data_days=[], quiet_days=[], missing_days=[])
    source = dict(current=cur, previous=None, provisional=days, kpis=[], currency="EUR")
    for add, attr, key in ((_add_paid_facts, "paid", "provisional ad dates"), (_add_search_facts, "search", "provisional search dates")):
        facts: dict = {}
        add(facts, SimpleNamespace(**{attr: SimpleNamespace(**source)}))
        assert facts[key].endswith("Oct 10 and others") and "more" not in facts[key], (attr, facts[key])
