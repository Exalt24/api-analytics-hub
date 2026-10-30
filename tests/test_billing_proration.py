"""Proration of a mid period plan change: Stripe's worked example, the written rounding rule checked against an exact fraction oracle over a generated table,
and the edges (a change before the period, after it, at either end). Pure: no database."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.billing.proration import half_up, prorate  # noqa: E402

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 31, tzinfo=timezone.utc)          # a 30 day period
MID = START + (END - START) / 2


def oracle_half_up(n: int, d: int) -> int:
    """Round half up with exact fractions: floor(n/d + 1/2)."""
    x = Fraction(n, d) + Fraction(1, 2)
    return x.numerator // x.denominator


def test_stripes_worked_example_a_10_plan_to_a_20_plan_halfway_bills_5_more():
    p = prorate(1000, 2000, START, END, MID)
    assert (p.credit_minor, p.charge_minor, p.net_minor) == (500, 1000, 500)


def test_the_rounding_rule_is_half_up_so_a_half_cent_goes_up_and_never_to_even():
    assert half_up(1, 2) == 1 and half_up(3, 2) == 2 and half_up(5, 2) == 3
    assert half_up(1, 3) == 0 and half_up(2, 3) == 1
    assert half_up(0, 7) == 0


def test_half_up_matches_an_exact_fraction_oracle_over_a_generated_table():
    for d in (1, 2, 3, 7, 30, 31, 86400, 2_592_000):
        for n in list(range(0, 40)) + [d - 1, d, d + 1, 2 * d - 1, 12345 * d // 7]:
            assert half_up(n, d) == oracle_half_up(n, d), (n, d)


@pytest.mark.parametrize("n,d", [(-1, 3), (1, 0), (1, -2)])
def test_half_up_refuses_a_negative_numerator_or_a_non_positive_denominator(n, d):
    with pytest.raises(ValueError):
        half_up(n, d)


def test_each_line_is_rounded_alone_and_the_net_is_the_difference_of_the_rounded_lines():
    for old, new in [(2900, 9900), (9900, 2900), (333, 1000), (1, 2), (5, 5), (0, 100), (100, 0), (999999, 1)]:
        for hours in (0, 1, 7, 100, 359, 360, 719, 720):
            p = prorate(old, new, START, END, START + timedelta(hours=hours))
            period = int((END - START).total_seconds())
            assert p.period_seconds == period
            assert p.credit_minor == oracle_half_up(old * p.remaining_seconds, period)
            assert p.charge_minor == oracle_half_up(new * p.remaining_seconds, period)
            assert p.net_minor == p.charge_minor - p.credit_minor


def test_a_downgrade_is_a_negative_net_and_an_upgrade_a_positive_one():
    assert prorate(9900, 2900, START, END, MID).net_minor < 0
    assert prorate(2900, 9900, START, END, MID).net_minor > 0
    assert prorate(2900, 2900, START, END, MID).net_minor == 0


def test_a_change_before_the_period_counts_the_whole_period_and_one_after_it_counts_none():
    before = prorate(1000, 2000, START, END, START - timedelta(days=3))
    assert (before.credit_minor, before.charge_minor, before.remaining_seconds) == (1000, 2000, before.period_seconds)
    after = prorate(1000, 2000, START, END, END + timedelta(days=1))
    assert (after.credit_minor, after.charge_minor, after.net_minor, after.remaining_seconds) == (0, 0, 0, 0)
    at_end = prorate(1000, 2000, START, END, END)
    assert at_end.remaining_seconds == 0 and at_end.net_minor == 0


def test_a_time_zone_does_not_change_the_answer():
    plus8 = timezone(timedelta(hours=8))
    a = prorate(2900, 9900, START, END, MID)
    b = prorate(2900, 9900, START.astimezone(plus8), END.astimezone(plus8), MID.astimezone(plus8))
    assert a == b


def test_a_naive_time_a_backwards_period_and_a_negative_price_are_refused():
    with pytest.raises(ValueError):
        prorate(1, 2, START.replace(tzinfo=None), END, MID)
    with pytest.raises(ValueError):
        prorate(1, 2, END, START, MID)
    with pytest.raises(ValueError):
        prorate(-1, 2, START, END, MID)
    with pytest.raises(ValueError):
        prorate(1, -2, START, END, MID)
