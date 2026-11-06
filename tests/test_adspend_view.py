"""The combined ad spend view: cost per conversion by platform and by week, with booked jobs beside it.

Examples pin the arithmetic and the wording. A property test then runs the rules over many seeded random worlds, because the view carries state across
days, weeks, connections and currencies and the bugs live where those cross: a world is a random set of ad connections (one to three of Google Ads, Meta and
TikTok, two of the same platform possible, each in its own currency), random days with spend and clicks and impressions and sometimes a conversion figure and
sometimes none, decoy rows outside the window and for other metrics, a random window that starts and ends on any weekday, and a shop's work orders.

The rules (independent of the implementation): spend is the sum of the in-window spend rows; the weeks partition the window exactly and their spends add up to
it; a week's conversion numerator and joint spend add up to the window's; cost per conversion is the joint spend over the conversions and absent when there are
none; a total is one row per currency and its spend is the sum of the platforms in that currency, never across currencies; rows outside the window and of other
metrics never move a figure; booked jobs are absent, not zero, when no day was synced.

Each rule has a mutation control: the same simulator run against a deliberately broken copy must go red.
"""
from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.report import adspend  # noqa: E402
from app.report.adspend import (  # noqa: E402
    AD_PLATFORMS, CONVERSIONS, MAX_DAYS, booked_jobs, build, combine, conversions_of, money_of, render_text, summarise, to_dict, week_starts)
from app.report.monthly import ReportError  # noqa: E402

START, END = date(2026, 10, 1), date(2026, 10, 8)


def row(day, key, value, currency=None):
    return {"observed_on": day, "metric_key": key, "value_numeric": value, "currency": currency}


def spend_rows(day, spend, clicks, impressions, conv=None, key="ad_conversions", currency="USD"):
    out = [row(day, "ad_spend", spend, currency), row(day, "ad_clicks", clicks), row(day, "ad_impressions", impressions)]
    if conv is not None:
        out.append(row(day, key, conv))
    return out


# ---------------------------------------------------------------- examples

def test_cost_per_conversion_is_the_spend_over_the_conversions_in_whole_conversions():
    rows = spend_rows(date(2026, 10, 1), 10000, 50, 1000, conv=2500) + spend_rows(date(2026, 10, 2), 5000, 20, 800, conv=500)   # 3.0 conversions
    p = summarise("google_ads", "Google Ads", rows, START, END)
    assert p.spend == 15000 and p.clicks == 70 and p.impressions == 1800 and p.conversions == Decimal(3) and p.joint_spend == 15000
    assert p.cost_per_conversion == Decimal(5000) and money_of(p.cost_per_conversion, "USD") == "$50.00"
    assert p.cost_per_click == Decimal(15000) / 70 and p.conversion_days == 2 and p.days == 2 and p.currency == "USD"


def test_meta_conversions_are_purchases_in_whole_numbers_and_tiktok_and_google_are_thousandths():
    assert CONVERSIONS["meta"][:2] == ("ad_purchases", 1) and CONVERSIONS["google_ads"][:2] == ("ad_conversions", 1000) == CONVERSIONS["tiktok"][:2]
    meta = summarise("meta", "Meta Ads", spend_rows(date(2026, 10, 1), 9000, 10, 100, conv=3, key="ad_purchases"), START, END)
    assert meta.conversions == Decimal(3) and meta.cost_per_conversion == Decimal(3000)
    tiktok = summarise("tiktok", "TikTok", spend_rows(date(2026, 10, 1), 9000, 10, 100, conv=3000), START, END)
    assert tiktok.conversions == Decimal(3) and tiktok.cost_per_conversion == Decimal(3000)


def test_a_day_with_spend_and_no_conversion_figure_does_not_pull_the_cost_per_conversion_up():
    rows = spend_rows(date(2026, 10, 1), 10000, 5, 100, conv=1000) + spend_rows(date(2026, 10, 2), 90000, 5, 100)
    p = summarise("google_ads", "G", rows, START, END)
    assert p.spend == 100000 and p.joint_spend == 10000 and p.conversions == Decimal(1) and p.cost_per_conversion == Decimal(10000)
    assert (p.conversion_days, p.days) == (1, 2)


def test_a_day_with_a_conversion_figure_and_no_spend_row_is_not_counted():
    rows = spend_rows(date(2026, 10, 1), 10000, 5, 100, conv=1000) + [row(date(2026, 10, 2), "ad_conversions", 5000)]
    p = summarise("google_ads", "G", rows, START, END)
    assert p.conversions == Decimal(1) and p.days == 1


def test_no_conversions_means_no_cost_per_conversion_not_zero_and_not_infinity():
    zero = summarise("google_ads", "G", spend_rows(date(2026, 10, 1), 10000, 5, 100, conv=0), START, END)
    assert zero.conversions == Decimal(0) and zero.cost_per_conversion is None and money_of(None, "USD") == "n/a"
    none = summarise("google_ads", "G", spend_rows(date(2026, 10, 1), 10000, 5, 100), START, END)
    assert none.conversions is None and none.cost_per_conversion is None and conversions_of(None) == "n/a"
    quiet = summarise("google_ads", "G", [], START, END)
    assert quiet.currency is None and quiet.spend == 0 and quiet.days == 0 and quiet.cost_per_click is None


def test_two_currencies_are_two_total_rows_and_are_never_added():
    usd = summarise("google_ads", "G", spend_rows(date(2026, 10, 1), 10000, 5, 100, conv=1000), START, END)
    eur = summarise("meta", "M", spend_rows(date(2026, 10, 1), 2000, 5, 100, conv=2, key="ad_purchases", currency="EUR"), START, END)
    usd2 = summarise("tiktok", "T", spend_rows(date(2026, 10, 1), 5000, 5, 100, conv=1000), START, END)
    totals = combine([usd, eur, usd2])
    assert [(t.currency, t.spend) for t in totals] == [("EUR", 2000), ("USD", 15000)]
    assert totals[1].conversions == Decimal(2) and totals[1].blended_cost_per_conversion == Decimal(7500) and totals[0].blended_cost_per_conversion == Decimal(1000)


def test_one_connection_in_two_currencies_is_refused_and_so_is_a_row_without_a_currency_or_a_non_integer():
    mixed = spend_rows(date(2026, 10, 1), 1, 1, 1) + spend_rows(date(2026, 10, 2), 1, 1, 1, currency="EUR")
    with pytest.raises(ReportError, match="one ad account is one currency"):
        summarise("google_ads", "G", mixed, START, END)
    with pytest.raises(ReportError, match="no currency"):
        summarise("google_ads", "G", [row(date(2026, 10, 1), "ad_spend", 5)], START, END)
    with pytest.raises(ReportError, match="not an integer"):
        summarise("google_ads", "G", [row(date(2026, 10, 1), "ad_clicks", 1.5)], START, END)
    with pytest.raises(ReportError, match="two ad_spend rows"):
        summarise("google_ads", "G", spend_rows(date(2026, 10, 1), 1, 1, 1) * 2, START, END)
    with pytest.raises(ReportError, match="not an ad platform"):
        summarise("square", "S", [], START, END)


def test_booked_jobs_are_the_work_orders_and_absent_not_zero_when_no_day_was_synced():
    assert booked_jobs([row(date(2026, 10, 1), "work_orders", 3), row(date(2026, 10, 2), "work_orders", 0)], START, END) == 3
    assert booked_jobs([], START, END) is None and booked_jobs([row(date(2026, 10, 1), "invoices", 9)], START, END) is None
    assert booked_jobs([row(date(2026, 9, 30), "work_orders", 3)], START, END) is None
    with pytest.raises(ReportError, match="two work_orders"):
        booked_jobs([row(date(2026, 10, 1), "work_orders", 1)] * 2, START, END)


def test_weeks_start_on_monday_and_the_first_and_last_are_cut_to_the_window():
    assert week_starts(date(2026, 10, 7), date(2026, 10, 20)) == [date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 19)]
    view = build({("google_ads", "G"): []}, date(2026, 10, 7), date(2026, 10, 20))
    assert [(w.start, w.end) for w in view.weeks] == [(date(2026, 10, 7), date(2026, 10, 12)), (date(2026, 10, 12), date(2026, 10, 19)),
                                                      (date(2026, 10, 19), date(2026, 10, 20))]


def test_the_window_must_be_at_least_one_day_and_at_most_a_year():
    with pytest.raises(ReportError):
        build({}, START, START)
    with pytest.raises(ReportError, match="at most"):
        build({}, START, START + timedelta(days=MAX_DAYS + 1))
    assert build({}, START, START + timedelta(days=MAX_DAYS)).platforms == []


def test_the_text_table_names_each_platform_the_totals_the_weeks_and_the_footnotes():
    rows = {("google_ads", "Fort Myers Google"): spend_rows(date(2026, 10, 1), 10000, 50, 1000, conv=2000) + spend_rows(date(2026, 10, 7), 4000, 5, 100, conv=1000),
            ("meta", "Fort Myers Meta"): spend_rows(date(2026, 10, 2), 3000, 9, 300, conv=3, key="ad_purchases", currency="USD")}
    bookings = [row(date(2026, 10, 1), "work_orders", 2), row(date(2026, 10, 7), "work_orders", 1)]
    text = render_text(build(rows, START, END, bookings=bookings))
    for fragment in ("Ad spend Oct 1 to Oct 7", "Fort Myers Google", "Fort Myers Meta", "$140.00", "Total in USD: $170.00 across Fort Myers Google, Fort Myers Meta",
                     "Booked jobs (Shopmonkey work orders): 3", "By week", "Oct 1 to Oct 4", "Oct 5 to Oct 7",
                     "Google Ads: conversion actions the account counts", "Meta Ads: purchases, 7 day click", "never converted or added across currencies"):
        assert fragment in text, fragment
    assert "$" in text and "—" not in text and "–" not in text


def test_the_json_form_has_minor_units_with_the_currency_beside_them_and_ratios_as_strings():
    rows = {("tiktok", "T"): spend_rows(date(2026, 10, 1), 10000, 50, 1000, conv=3000)}
    body = to_dict(build(rows, START, END, bookings=[row(date(2026, 10, 1), "work_orders", 4)]))
    p = body["platforms"][0]
    assert (p["platform"], p["currency"], p["spend_minor"], p["conversions"], p["cost_per_conversion_minor"]) == ("tiktok", "USD", 10000, "3.0000", "3333.3333")
    assert body["booked_jobs"] == 4 and body["totals"][0] == {"currency": "USD", "platforms": ["T"], "spend_minor": 10000, "conversions": "3.0000",
                                                             "blended_cost_per_conversion_minor": "3333.3333"}
    assert body["weeks"][0]["booked_jobs"] == 4 and body["start"] == "2026-10-01" and body["end"] == "2026-10-08"


def test_conversion_wording_shows_a_fraction_only_when_there_is_one():
    assert [conversions_of(Decimal(v)) for v in ("3", "3.0", "2.5", "1234", "0.04", "0")] == ["3", "3", "2.5", "1,234", "0.0", "0"]
    assert money_of(Decimal("3333.3333"), "USD") == "$33.33" and money_of(Decimal("3333.5"), "USD") == "$33.34" and money_of(Decimal(5), None) == "n/a"


# ---------------------------------------------------------------- the property test

def make_world(seed: int):
    rng = random.Random(seed)
    start = date(2026, 9, 1) + timedelta(days=rng.randint(0, 40))
    end = start + timedelta(days=rng.randint(1, 45))
    connections: dict[tuple[str, str], list[dict]] = {}
    for n in range(rng.randint(1, 4)):
        platform = rng.choice(AD_PLATFORMS)
        label = "%s #%d" % (platform, n)
        currency = rng.choice(["USD", "USD", "EUR", "JPY", "CAD"])
        conv_key = CONVERSIONS[platform][0]
        rows = []
        day = start - timedelta(days=rng.randint(0, 6))
        while day < end + timedelta(days=rng.randint(0, 6)):
            if rng.random() < 0.85:
                rows += [row(day, "ad_spend", rng.randint(0, 500_000), currency), row(day, "ad_clicks", rng.randint(0, 900)), row(day, "ad_impressions", rng.randint(0, 90_000))]
                if rng.random() < 0.7:
                    rows.append(row(day, conv_key, rng.choice([0, rng.randint(1, 40) * (1000 if conv_key == "ad_conversions" else 1), rng.randint(1, 9999)])))
            if rng.random() < 0.15:
                rows.append(row(day, "ad_conversions" if conv_key == "ad_purchases" else "ad_purchases", rng.randint(0, 50)))     # a decoy metric of the wrong platform
            if rng.random() < 0.1:
                rows.append(row(day, "payments", rng.randint(0, 9)))                                                                # a decoy metric of another source
            day += timedelta(days=1)
        connections[(platform, label)] = rows
    bookings = []
    if rng.random() < 0.8:
        day = start - timedelta(days=3)
        while day < end + timedelta(days=3):
            if rng.random() < 0.7:
                bookings.append(row(day, "work_orders", rng.randint(0, 6)))
            day += timedelta(days=1)
    return start, end, connections, bookings


def reference(platform, rows, lo, hi):
    """spend, conv numerator in native units, joint spend, days, conversion days: an independent walk over the rows."""
    conv_key, divisor, _ = CONVERSIONS[platform]
    spend_by_day = {r["observed_on"]: r["value_numeric"] for r in rows if r["metric_key"] == "ad_spend" and lo <= r["observed_on"] < hi}
    conv_by_day = {r["observed_on"]: r["value_numeric"] for r in rows if r["metric_key"] == conv_key and lo <= r["observed_on"] < hi}
    joint_days = [d for d in spend_by_day if d in conv_by_day]
    return (sum(spend_by_day.values()), sum(conv_by_day[d] for d in joint_days), sum(spend_by_day[d] for d in joint_days), len(spend_by_day), len(joint_days), divisor)


def check_world(seed: int, module=adspend) -> list[str]:
    start, end, connections, bookings = make_world(seed)
    view = module.build(connections, start, end, bookings=bookings)
    problems: list[str] = []
    expected_weeks = week_starts(start, end)
    if [w.start for w in view.weeks][1:] != expected_weeks[1:] or view.weeks[0].start != start or view.weeks[-1].end != end:
        problems.append("the weeks do not partition the window")
    for a, b in zip(view.weeks, view.weeks[1:]):
        if a.end != b.start:
            problems.append("a gap or overlap between weeks")
    by_label = {p.label: p for p in view.platforms}
    for (platform, label), rows in connections.items():
        p = by_label[label]
        spend, conv_num, joint, days, conv_days, divisor = reference(platform, rows, start, end)
        if p.spend != spend or p.days != days or p.conversion_days != conv_days or p.joint_spend != joint:
            problems.append("%s: window figures %r differ from the reference" % (label, (p.spend, p.days, p.conversion_days, p.joint_spend, spend, days, conv_days, joint)))
        want_conv = Decimal(conv_num) / divisor if conv_days else None
        if p.conversions != want_conv:
            problems.append("%s: conversions %r, reference %r" % (label, p.conversions, want_conv))
        want_cpc = Decimal(joint) / want_conv if want_conv else None
        if p.cost_per_conversion != want_cpc:
            problems.append("%s: cost per conversion %r, reference %r" % (label, p.cost_per_conversion, want_cpc))
        weekly = [next(q for q in w.platforms if q.label == label) for w in view.weeks]
        if sum(q.spend for q in weekly) != p.spend or sum(q.joint_spend for q in weekly) != p.joint_spend:
            problems.append("%s: the weeks do not add up to the window" % label)
        if sum((q.conversions or Decimal(0)) for q in weekly) != (p.conversions or Decimal(0)):
            problems.append("%s: weekly conversions do not add up to the window" % label)
    currencies = {p.currency for p in view.platforms if p.currency}
    if sorted(t.currency for t in view.totals) != sorted(currencies):
        problems.append("totals are not one row per currency")
    for t in view.totals:
        group = [p for p in view.platforms if p.currency == t.currency]
        if t.spend != sum(p.spend for p in group):
            problems.append("a total in %s is not the sum of its platforms" % t.currency)
    jobs = [r["value_numeric"] for r in bookings if start <= r["observed_on"] < end]
    in_window = [r for r in bookings if start <= r["observed_on"] < end]
    if view.booked_jobs != (sum(jobs) if in_window else None):
        problems.append("booked jobs %r, reference %r" % (view.booked_jobs, sum(jobs) if in_window else None))
    return problems


SEEDS = range(300)


@pytest.mark.parametrize("seed", SEEDS)
def test_the_view_obeys_its_rules_in_every_seeded_world(seed):
    assert check_world(seed) == []


def test_the_worlds_cover_the_cases_the_rules_are_about():
    seen = set()
    for seed in SEEDS:
        start, end, connections, bookings = make_world(seed)
        view = build(connections, start, end, bookings=bookings)
        seen.add("multi_currency" if len({p.currency for p in view.platforms if p.currency}) > 1 else "one_currency")
        seen.add("no_conv" if any(p.conversions is None for p in view.platforms) else "all_conv")
        seen.add("zero_conv" if any(p.conversions == 0 for p in view.platforms) else "no_zero")
        seen.add("jobs" if view.booked_jobs is not None else "no_jobs")
        seen.add("two_same" if len({p.platform for p in view.platforms}) < len(view.platforms) else "distinct")
        seen.add("partial" if any(0 < p.conversion_days < p.days for p in view.platforms) else "none_partial")
    assert {"multi_currency", "one_currency", "no_conv", "all_conv", "zero_conv", "jobs", "no_jobs", "two_same", "partial"} <= seen


class MixedCurrencyModule:
    """MUTATION CONTROL: totals add spend across currencies."""

    def build(self, connections, start, end, bookings=()):
        view = adspend.build(connections, start, end, bookings=bookings)
        if view.platforms:
            from app.report.adspend import CurrencyTotal
            view.totals = [CurrencyTotal("MIXED", tuple(p.label for p in view.platforms), sum(p.spend for p in view.platforms), None, None)]
        return view


class AllSpendModule:
    """MUTATION CONTROL: cost per conversion divides ALL spend, including days with no conversion figure."""

    def build(self, connections, start, end, bookings=()):
        view = adspend.build(connections, start, end, bookings=bookings)
        from dataclasses import replace
        view.platforms = [replace(p, cost_per_conversion=(Decimal(p.spend) / p.conversions) if p.conversions else None) for p in view.platforms]
        return view


class ZeroJobsModule:
    """MUTATION CONTROL: a window with no synced work order day reports zero booked jobs."""

    def build(self, connections, start, end, bookings=()):
        view = adspend.build(connections, start, end, bookings=bookings)
        view.booked_jobs = view.booked_jobs or 0
        return view


class OffByOneWeekModule:
    """MUTATION CONTROL: the last week stops a day short."""

    def build(self, connections, start, end, bookings=()):
        view = adspend.build(connections, start, end, bookings=bookings)
        from dataclasses import replace
        view.weeks[-1] = replace(view.weeks[-1], end=view.weeks[-1].end - timedelta(days=1))
        return view


@pytest.mark.parametrize("mutant", [MixedCurrencyModule, AllSpendModule, ZeroJobsModule, OffByOneWeekModule])
def test_mutation_controls_turn_the_simulator_red(mutant):
    assert any(check_world(seed, mutant()) for seed in SEEDS), mutant.__name__ + " should break a rule"
    assert check_world(0) == [], "positive control: the real view passes the same world"
