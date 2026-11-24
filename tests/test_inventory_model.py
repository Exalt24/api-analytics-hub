"""The inventory planning rules (app/inventory/model.py), by hand worked case and as properties over seeded random worlds.

Three rules carry the module and each gets a property, not only an example:
  * a stockout must not fool the forecast: velocity is units over the days the variant was on sale, an out of stock day is never counted, and taking a no-sale day out of the count
    never lowers the velocity;
  * the reorder point is monotone: a longer lead time, a higher service level, a faster or more erratic demand never lowers it;
  * the classes and labels partition the variants: every variant gets exactly one class and one label, whatever order they arrive in.

The hand worked cases are written out in docs/inventory-planning.md; each number below was worked on paper first.
"""
from __future__ import annotations

import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.inventory import model  # noqa: E402
from app.inventory.model import (  # noqa: E402
    IN_STOCK, OUT_OF_STOCK, UNKNOWN, PlanParams, Promo, VariantInput, abc_classes, ceil_units, date_range, days_of_cover, demand_over, label_for, last_restock, plan_all,
    plan_variant, projected_cover, promo_factor_on, reorder_point, round_to_order, safety_stock, stock_states, stockout_adjusted_velocity, z_score)

AS_OF = date(2026, 11, 24)
WINDOW = date_range(AS_OF - timedelta(days=28), AS_OF)           # 2026-10-27 .. 2026-11-23


def day(n: int) -> date:
    """The n th day of the 28 day window, from 1."""
    return WINDOW[n - 1]


# ------------------------------------------------------------------------------------------------ which days were on sale

def test_a_sale_proves_stock_even_when_the_snapshot_that_day_is_zero():
    states = stock_states([day(1)], {day(1): 0}, {day(1): 3})
    assert states[day(1)] == IN_STOCK


def test_a_zero_snapshot_with_no_sale_is_out_and_a_positive_one_is_in():
    states = stock_states([day(1), day(2)], {day(1): 0, day(2): 4}, {})
    assert states == {day(1): OUT_OF_STOCK, day(2): IN_STOCK}


def test_a_gap_between_two_empty_snapshots_is_out_and_a_gap_beside_stock_is_unknown():
    snaps = {day(1): 0, day(5): 0, day(9): 7, day(13): 0}
    states = stock_states([day(i) for i in range(1, 15)], snaps, {})
    assert [states[day(i)] for i in (2, 3, 4)] == [OUT_OF_STOCK] * 3        # 0 before and 0 after
    assert [states[day(i)] for i in (6, 7, 8)] == [UNKNOWN] * 3             # 0 before, stock after: it may have restocked on any of them
    assert [states[day(i)] for i in (10, 11, 12)] == [UNKNOWN] * 3          # stock before, 0 after
    assert [states[day(i)] for i in (14,)] == [OUT_OF_STOCK]                # the last snapshot was 0 and nothing later says otherwise


def test_days_before_the_first_snapshot_are_unknown_not_out():
    states = stock_states([day(1), day(2)], {day(3): 0}, {})
    assert states[day(1)] == UNKNOWN and states[day(2)] == UNKNOWN


def test_hand_worked_velocity_divides_by_the_days_on_sale_not_the_days_in_the_window():
    """28 day window: stock on days 1 to 10, none on days 11 to 18 (8 days), stock again on days 19 to 28. 2 units every day it was on sale: 40 units over 20 days is 2.0 a day,
    where dividing by the 28 day window would read 1.43 and say demand fell by 29 percent."""
    snaps = {day(d): (5 if d <= 10 else 0 if d <= 18 else 20) for d in range(1, 29)}
    sales = {day(d): 2 for d in range(1, 29) if not 11 <= d <= 18}
    states = stock_states(WINDOW, snaps, sales)
    v = stockout_adjusted_velocity(WINDOW, sales, states)
    assert (v.units, v.counted_days, v.in_days, v.out_days, v.unknown_days) == (40, 20, 20, 8, 0)
    assert v.mean == pytest.approx(2.0) and v.variance == pytest.approx(0.0)
    assert 40 / 28 == pytest.approx(1.4286, abs=1e-4)


def test_hand_worked_variance_of_a_lumpy_seller():
    """20 days on sale, 4 units on 10 of them and none on the other 10: mean 2, sample variance 20 * 4 / 19 = 4.2105, so a standard deviation of 2.052 and a standard error of 0.459."""
    sales = {day(d): 4 for d in range(1, 21, 2)}
    snaps = {day(d): 9 for d in range(1, 21)} | {day(d): 0 for d in range(21, 29)}
    states = stock_states(WINDOW, snaps, sales)
    v = stockout_adjusted_velocity(WINDOW, sales, states)
    assert (v.counted_days, v.units, v.out_days) == (20, 40, 8)
    assert v.mean == pytest.approx(2.0)
    assert v.variance == pytest.approx(80 / 19)
    assert v.sd == pytest.approx(math.sqrt(80 / 19)) and v.stderr == pytest.approx(math.sqrt(80 / 19 / 20))
    assert v.coverage == 1.0


def test_unknown_days_are_counted_by_default_and_left_out_when_asked():
    sales = {day(3): 6}
    states = stock_states(WINDOW, {}, sales)                         # no snapshots at all: every day but one is unknown
    assumed = stockout_adjusted_velocity(WINDOW, sales, states, "assume_in")
    excluded = stockout_adjusted_velocity(WINDOW, sales, states, "exclude")
    assert (assumed.counted_days, assumed.mean) == (28, pytest.approx(6 / 28))
    assert (excluded.counted_days, excluded.mean) == (1, pytest.approx(6.0))
    assert assumed.coverage == pytest.approx(1 / 28)                 # only one day was observed: the figure says how little it rests on
    with pytest.raises(ValueError):
        stockout_adjusted_velocity(WINDOW, sales, states, "guess")


def test_nothing_counted_gives_zero_velocity_and_no_division():
    states = {d: OUT_OF_STOCK for d in WINDOW}
    v = stockout_adjusted_velocity(WINDOW, {}, states)
    assert (v.counted_days, v.mean, v.variance, v.stderr) == (0, 0.0, 0.0, 0.0)


def _world(rng: random.Random):
    """A random variant: snapshots on some days (zero or positive), sales on some days, a stretch with no stock."""
    snaps = {}
    for d in WINDOW:
        if rng.random() < rng.choice((0.0, 0.5, 1.0)):
            snaps[d] = rng.choice((0, 0, 3, 12, 40))
    sales = {d: rng.randint(1, 6) for d in WINDOW if rng.random() < 0.35}
    return snaps, sales


def test_property_velocity_never_counts_a_day_the_variant_could_not_sell():
    for seed in range(400):
        rng = random.Random(seed)
        snaps, sales = _world(rng)
        states = stock_states(WINDOW, snaps, sales)
        for policy in ("assume_in", "exclude"):
            v = stockout_adjusted_velocity(WINDOW, sales, states, policy)
            out_days = [d for d in WINDOW if states[d] == OUT_OF_STOCK]
            # an out of stock day sold nothing by definition (a sale makes the day IN), and it is not in the denominator under either policy
            assert all(sales.get(d, 0) == 0 for d in out_days), seed
            counted = [d for d in WINDOW if states[d] == IN_STOCK or (policy == "assume_in" and states[d] == UNKNOWN)]
            assert v.counted_days == len(counted) and v.out_days == len(out_days), seed
            assert v.units == sum(sales.get(d, 0) for d in WINDOW), seed            # every unit sold is in the numerator
            expected = v.units / len(counted) if counted else 0.0
            assert v.mean == pytest.approx(expected), seed
            assert v.in_days + v.out_days + v.unknown_days == len(WINDOW), seed


def test_property_taking_a_no_sale_day_out_of_the_count_never_lowers_the_velocity():
    for seed in range(400):
        rng = random.Random(1000 + seed)
        snaps, sales = _world(rng)
        before = stockout_adjusted_velocity(WINDOW, sales, stock_states(WINDOW, snaps, sales))
        quiet = [d for d in WINDOW if sales.get(d, 0) == 0 and d not in snaps]
        if not quiet:
            continue
        snaps2 = dict(snaps)
        snaps2[rng.choice(quiet)] = 0                                                # one more day with an empty shelf and no sale
        after = stockout_adjusted_velocity(WINDOW, sales, stock_states(WINDOW, snaps2, sales))
        assert after.mean >= before.mean - 1e-12, seed


def test_an_out_of_stock_stretch_does_not_make_a_product_look_slow():
    """The failure the module exists to prevent: the same demand, with and without a ten day stockout, reads the same velocity."""
    always = {day(d): 1 for d in range(1, 29)}
    full = stockout_adjusted_velocity(WINDOW, always, stock_states(WINDOW, {day(d): 5 for d in range(1, 29)}, always))
    sales = {day(d): 1 for d in range(1, 29) if d > 10}
    snaps = {day(d): (0 if d <= 10 else 5) for d in range(1, 29)}
    stocked_out = stockout_adjusted_velocity(WINDOW, sales, stock_states(WINDOW, snaps, sales))
    assert stocked_out.mean == pytest.approx(full.mean) == pytest.approx(1.0)
    assert 18 / 28 == pytest.approx(0.643, abs=1e-3)                                  # what dividing by the whole window would have said


# ------------------------------------------------------------------------------------------------ safety stock, reorder point, cover, orders

def test_the_normal_quantile_for_the_usual_service_levels():
    assert z_score(0.95) == pytest.approx(1.6448536, abs=1e-6)
    assert z_score(0.90) == pytest.approx(1.2815516, abs=1e-6)
    assert z_score(0.5) == 0.0
    for bad in (0.49, 1.0, 1.5):
        with pytest.raises(ValueError):
            z_score(bad)


def test_hand_worked_safety_stock_and_reorder_point():
    """Demand 2 a day with a standard deviation of 1, a nine day lead time, 95 percent service: z * sqrt(9 * 1) = 1.6449 * 3 = 4.93, held as 5. The reorder point is 2 * 9 + 4.93 = 22.93, held as 23.
    With the lead time itself uncertain by 2 days: z * sqrt(9 * 1 + 4 * 4) = 1.6449 * 5 = 8.22, held as 9, and the reorder point 26.22, held as 27."""
    ss = safety_stock(2.0, 1.0, 9, 0.0, 0.95)
    assert ss == pytest.approx(4.9346, abs=1e-4) and ceil_units(ss) == 5
    assert ceil_units(reorder_point(18.0, ss)) == 23
    ss_late = safety_stock(2.0, 1.0, 9, 2.0, 0.95)
    assert ss_late == pytest.approx(8.2243, abs=1e-4) and ceil_units(ss_late) == 9
    assert ceil_units(reorder_point(18.0, ss_late)) == 27


def test_no_variation_means_no_safety_stock_and_a_negative_input_is_refused():
    assert safety_stock(5.0, 0.0, 30, 0.0, 0.99) == 0.0
    for args in ((-1, 1, 1, 0), (1, -1, 1, 0), (1, 1, -1, 0), (1, 1, 1, -1)):
        with pytest.raises(ValueError):
            safety_stock(*args)


def test_property_the_reorder_point_never_falls_when_lead_time_service_demand_or_variation_rise():
    for seed in range(400):
        rng = random.Random(2000 + seed)
        d, sd = rng.uniform(0, 30), rng.uniform(0, 15)
        lead, lead_sd, level = rng.randint(0, 120), rng.uniform(0, 20), rng.uniform(0.5, 0.999)

        def rop(d=d, sd=sd, lead=lead, lead_sd=lead_sd, level=level) -> float:
            return reorder_point(d * lead, safety_stock(d, sd, lead, lead_sd, level))

        base = rop()
        assert rop(lead=lead + rng.randint(1, 30)) >= base - 1e-9, seed
        assert rop(level=min(level + 0.0005, 0.9995)) >= base - 1e-9 or level >= 0.9995, seed
        assert rop(d=d + rng.uniform(0, 5)) >= base - 1e-9, seed
        assert rop(sd=sd + rng.uniform(0, 5)) >= base - 1e-9, seed
        assert rop(lead_sd=lead_sd + rng.uniform(0, 5)) >= base - 1e-9, seed
        assert base >= d * lead - 1e-9, seed                                          # safety stock is never negative


def test_days_of_cover():
    assert days_of_cover(40, 2.0) == 20.0
    assert days_of_cover(0, 2.0) == 0.0
    assert days_of_cover(-3, 2.0) == 0.0                                              # an oversold variant has no cover, not a negative one
    assert days_of_cover(40, 0.0) is None                                             # nothing sells: the cover is not a number


def test_round_to_order_takes_the_minimum_and_whole_packs():
    assert round_to_order(0, 50, 12) == 0 and round_to_order(-4, 50, 12) == 0
    assert round_to_order(41, 50, 12) == 60                                           # 41 is under the 50 minimum, 50 is not a whole number of 12s, so 60
    assert round_to_order(100, 1, 1) == 100
    assert round_to_order(7, 1, 6) == 12
    assert round_to_order(30, 25, 5) == 30


def test_property_an_order_quantity_is_a_pack_multiple_at_least_the_minimum_and_never_less_than_asked():
    for seed in range(400):
        rng = random.Random(3000 + seed)
        q, moq, pack = rng.randint(-5, 500), rng.randint(1, 80), rng.randint(1, 24)
        out = round_to_order(q, moq, pack)
        if q <= 0:
            assert out == 0
        else:
            assert out % pack == 0 and out >= moq and out >= q and out - max(q, moq) < pack, seed


# ------------------------------------------------------------------------------------------------ promotions

def test_a_promotion_factor_covers_its_days_and_overlaps_take_the_largest():
    promos = [Promo(date(2026, 12, 1), date(2026, 12, 7), 1.5), Promo(date(2026, 12, 5), date(2026, 12, 9), 2.0)]
    assert promo_factor_on(date(2026, 11, 30), promos) == 1.0
    assert promo_factor_on(date(2026, 12, 1), promos) == 1.5
    assert promo_factor_on(date(2026, 12, 6), promos) == 2.0                          # two promotions: the larger, not 3.0
    assert promo_factor_on(date(2026, 12, 9), promos) == 2.0 and promo_factor_on(date(2026, 12, 10), promos) == 1.0


def test_hand_worked_demand_and_cover_under_a_promotion():
    """2 a day, 20 on the shelf, and a 2.0 promotion starting on the fifth day (day 4 from today): days 0 to 3 sell 2 each (8, 12 left), then days 4, 5 and 6 sell 4 each and empty the
    shelf exactly at the end of day 6, 7.0 days. At the plain 2 a day the cover would be 10 days."""
    promos = [Promo(AS_OF + timedelta(days=4), AS_OF + timedelta(days=30), 2.0)]
    assert days_of_cover(20, 2.0) == 10.0
    assert projected_cover(20, 2.0, AS_OF, promos) == pytest.approx(7.0)
    assert demand_over(2.0, AS_OF, 7, promos) == pytest.approx(2 * 4 + 4 * 3)
    assert projected_cover(20, 2.0, AS_OF, []) == pytest.approx(10.0)
    assert projected_cover(0, 2.0, AS_OF, promos) == 0.0
    assert projected_cover(20, 0.0, AS_OF, promos) is None
    assert projected_cover(10 ** 6, 1.0, AS_OF, []) is None                           # beyond a year: no stockout in sight


def test_property_a_promotion_with_factors_of_one_or_more_never_lengthens_the_cover_or_lowers_demand():
    for seed in range(300):
        rng = random.Random(4000 + seed)
        base = rng.uniform(0.2, 12)
        stock = rng.randint(1, 600)
        promos = [Promo(AS_OF + timedelta(days=rng.randint(0, 40)), AS_OF + timedelta(days=rng.randint(40, 80)), rng.uniform(1.0, 4.0)) for _ in range(rng.randint(0, 3))]
        plain = projected_cover(stock, base, AS_OF, [])
        with_promo = projected_cover(stock, base, AS_OF, promos)
        if plain is not None:
            assert with_promo is not None and with_promo <= plain + 1e-9, seed
        horizon = rng.randint(1, 90)
        assert demand_over(base, AS_OF, horizon, promos) >= demand_over(base, AS_OF, horizon, []) - 1e-9, seed


# ------------------------------------------------------------------------------------------------ classes and labels

def test_hand_worked_abc_classes():
    """500, 300, 100, 60, 40 of 1000. a starts at 0 percent (A), b at 50 (A), c at 80 (B: the cut is under 80), d at 90 (B), e at 96 (C)."""
    assert abc_classes({"a": 500, "b": 300, "c": 100, "d": 60, "e": 40}) == {"a": "A", "b": "A", "c": "B", "d": "B", "e": "C"}


def test_one_product_is_class_a_and_no_sales_at_all_is_class_c():
    assert abc_classes({"only": 10}) == {"only": "A"}
    assert abc_classes({"x": 0, "y": 0}) == {"x": "C", "y": "C"}
    assert abc_classes({"big": 100, "none": 0}) == {"big": "A", "none": "C"}
    assert abc_classes({}) == {}


def test_property_the_classes_partition_the_variants_and_follow_the_revenue_rank():
    for seed in range(400):
        rng = random.Random(5000 + seed)
        n = rng.randint(1, 30)
        revenue = {"k%02d" % i: rng.choice((0, 0, rng.randint(1, 100), rng.randint(1, 100000))) for i in range(n)}
        classes = abc_classes(revenue)
        assert set(classes) == set(revenue) and set(classes.values()) <= {"A", "B", "C"}, seed          # every variant has exactly one class
        shuffled = dict(rng.sample(sorted(revenue.items()), len(revenue)))
        assert abc_classes(shuffled) == classes, seed                                                    # the answer does not depend on input order
        ranked = sorted(revenue, key=lambda k: (-revenue[k], k))
        order = [classes[k] for k in ranked]
        assert order == sorted(order), seed                                                              # A before B before C down the ranking
        total = sum(revenue.values())
        if total:
            a_items = [k for k in ranked if classes[k] == "A"]
            assert a_items, seed
            before_last_a = sum(revenue[k] for k in a_items[:-1])
            assert before_last_a * 100 < 80 * total, seed                                                # an A item starts under 80 percent of the revenue
            assert all(revenue[k] == 0 for k in ranked if classes[k] == "C") or sum(revenue[k] for k in ranked if classes[k] in "AB") * 100 >= 95 * total - 1, seed


def _velocity(units: int, days: int = 28):
    v = stockout_adjusted_velocity(WINDOW[:days], {WINDOW[0]: units} if units else {}, {d: IN_STOCK for d in WINDOW[:days]})
    return v


def test_labels_each_rule_and_the_order_they_apply_in():
    p = PlanParams()
    assert label_for("A", 50, _velocity(30), _velocity(30), p) == "best_seller"
    assert label_for("A", 9, _velocity(0), _velocity(0), p) == "slow_mover"        # class A but too few units sold in all history to be called a best seller, and it is not selling now
    assert label_for("B", 40, _velocity(20), _velocity(10), p) == "rising"         # 2x the prior window with 20 units recently
    assert label_for("B", 40, _velocity(14), _velocity(10), p) == "steady"         # 1.4x is under the 1.5 ratio
    assert label_for("B", 8, _velocity(5), _velocity(0), p) == "rising"            # a launch: nothing before, 5 units now
    assert label_for("B", 8, _velocity(4), _velocity(0), p) == "steady"           # under the 5 unit floor for rising, and 4 in 28 days is 0.14 a day, over the 0.1 slow line
    assert label_for("C", 3, _velocity(2), _velocity(2), p) == "slow_mover"
    assert label_for("A", 90, _velocity(80), _velocity(10), p) == "best_seller"    # a rising best seller is still labelled best_seller; the trend ratio shows it


def _random_item(rng: random.Random, key: str) -> VariantInput:
    snaps, sales_recent = _world(rng)
    sales = {d: q for d, q in sales_recent.items()}
    for d in date_range(AS_OF - timedelta(days=56), AS_OF - timedelta(days=28)):
        if rng.random() < 0.3:
            sales[d] = rng.randint(1, 5)
    units_all = sum(sales.values()) + rng.randint(0, 40)
    return VariantInput(
        key=key, available=rng.choice((0, 1, 5, 30, 200)), incoming=rng.choice((0, 0, 10)), tracked=rng.random() < 0.9, stock_known=rng.random() < 0.9, snapshots=snaps, sales=sales,
        units_all=units_all, revenue_minor=units_all * rng.randint(100, 9000), lead_time_days=rng.choice((None, 0, 7, 21, 60)), lead_time_sd_days=rng.choice((0.0, 3.0)),
        moq=rng.choice((1, 10, 50)), pack_size=rng.choice((1, 6, 12)),
        promos=tuple(Promo(AS_OF + timedelta(days=rng.randint(0, 30)), AS_OF + timedelta(days=rng.randint(30, 60)), rng.uniform(0.5, 3)) for _ in range(rng.randint(0, 2))))


def test_property_every_variant_gets_one_class_one_label_and_a_consistent_order():
    for seed in range(250):
        rng = random.Random(6000 + seed)
        items = [_random_item(rng, "v%02d" % i) for i in range(rng.randint(1, 15))]
        rows = plan_all(items, AS_OF)
        assert [r.key for r in rows] == [i.key for i in items], seed
        by_key = {i.key: i for i in items}
        for r in rows:
            item = by_key[r.key]
            assert r.abc_class in model.ABC and r.label in model.LABELS, seed
            assert r.velocity.mean >= 0 and r.velocity.variance >= 0 and r.velocity.counted_days <= r.velocity.window_days, seed
            if r.status != "ok":
                assert not r.needs_reorder and r.suggested_qty == 0 and r.reorder_point is None, seed
            else:
                assert r.safety_stock is not None and r.reorder_point is not None and r.reorder_point >= r.safety_stock, seed
                assert r.needs_reorder == (r.velocity.mean > 0 and item.available + item.incoming <= r.reorder_point), seed
                if r.suggested_qty:
                    assert r.needs_reorder and r.suggested_qty % item.pack_size == 0 and r.suggested_qty >= item.moq, seed
            if r.cover_days is not None and item.lead_time_days is not None and r.status == "ok":
                assert (r.cover_days >= item.lead_time_days) or r.needs_reorder or r.velocity.mean == 0 or item.incoming > 0, seed      # cover under the lead time is on the reorder list unless stock is already inbound


def test_property_the_same_variants_in_any_order_give_the_same_plan():
    for seed in range(60):
        rng = random.Random(7000 + seed)
        items = [_random_item(rng, "v%02d" % i) for i in range(rng.randint(2, 12))]
        a = {r.key: r for r in plan_all(items, AS_OF)}
        b = {r.key: r for r in plan_all(rng.sample(items, len(items)), AS_OF)}
        assert a == b, seed


def test_hand_worked_plan_for_one_variant_that_needs_an_order():
    """Sells 2 a day (sd 1) over a 28 day window, 10 on the shelf, supplier lead time 9 days, 95 percent service, review 14 days, minimum order 50, packs of 12.
    Cover is 10 / 2 = 5 days, under the 9 day lead time. Reorder point 23 (worked above), position 10 is under it. Target = 2 * (9 + 14) + 5 = 51, less 10 on hand is 41, raised to the
    minimum 50 and then to whole packs of 12: 60."""
    sales = {d: (4 if i % 2 == 0 else 0) for i, d in enumerate(WINDOW)}
    sales_rich = {d: q for d, q in sales.items() if q}
    snaps = {d: 9 for d in WINDOW}                                                    # on the shelf all window, so nothing is out of stock
    item = VariantInput(key="v", available=10, snapshots=snaps, sales=sales_rich, units_all=56, revenue_minor=56000, lead_time_days=9, moq=50, pack_size=12)
    row = plan_variant(item, AS_OF, "A", PlanParams())
    assert row.velocity.mean == pytest.approx(2.0) and row.velocity.counted_days == 28
    assert row.cover_days == pytest.approx(5.0)
    sd = row.velocity.sd
    expected_ss = ceil_units(safety_stock(2.0, sd, 9, 0.0, 0.95))
    assert row.safety_stock == expected_ss
    assert row.reorder_point == ceil_units(18.0 + safety_stock(2.0, sd, 9, 0.0, 0.95))
    assert row.needs_reorder and row.status == "ok"
    assert row.suggested_qty == round_to_order(ceil_units(2.0 * 23 + expected_ss - 10), 50, 12)
    assert row.suggested_qty % 12 == 0 and row.suggested_qty >= 50


def test_a_variant_with_no_supplier_is_classified_and_forecast_but_not_ordered():
    item = VariantInput(key="v", available=3, snapshots={d: 3 for d in WINDOW}, sales={d: 1 for d in WINDOW}, units_all=40, revenue_minor=40000)
    row = plan_variant(item, AS_OF, "A", PlanParams())
    assert row.status == "no_supplier" and row.reorder_point is None and row.suggested_qty == 0 and not row.needs_reorder
    assert row.cover_days == pytest.approx(3.0) and row.label == "best_seller"


def test_an_untracked_variant_and_one_never_read_are_not_planned():
    base = dict(key="v", available=0, sales={WINDOW[0]: 5}, units_all=5, revenue_minor=500, lead_time_days=10)
    assert plan_variant(VariantInput(tracked=False, **base), AS_OF, "A", PlanParams()).status == "untracked"
    assert plan_variant(VariantInput(stock_known=False, **base), AS_OF, "A", PlanParams()).status == "no_stock_reading"


def test_a_promotion_raises_the_reorder_point_and_the_order_and_shortens_the_forecast_cover():
    sales = {d: 2 for d in WINDOW}
    snaps = {d: 30 for d in WINDOW}
    common = dict(key="v", available=40, snapshots=snaps, sales=sales, units_all=56, revenue_minor=56000, lead_time_days=10, moq=1, pack_size=1)
    plain = plan_variant(VariantInput(**common), AS_OF, "A", PlanParams())
    promo = plan_variant(VariantInput(promos=(Promo(AS_OF + timedelta(days=3), AS_OF + timedelta(days=30), 2.0),), **common), AS_OF, "A", PlanParams())
    assert plain.cover_days == promo.cover_days == pytest.approx(20.0)                # the normal cover does not move
    assert promo.forecast_cover_days < plain.forecast_cover_days
    assert promo.reorder_point > plain.reorder_point and promo.promo_factor > 1.0 and plain.promo_factor == 1.0
    assert promo.stockout_date < plain.stockout_date


def test_property_the_reorder_point_of_a_planned_variant_never_falls_as_the_lead_time_grows():
    for seed in range(200):
        rng = random.Random(8000 + seed)
        base = _random_item(rng, "v")
        short, long_ = sorted(rng.sample(range(0, 120), 2))
        rows = []
        for lead in (short, long_):
            item = VariantInput(key="v", available=base.available, incoming=base.incoming, tracked=True, stock_known=True, snapshots=base.snapshots, sales=base.sales,
                                units_all=base.units_all, revenue_minor=base.revenue_minor, lead_time_days=lead, lead_time_sd_days=base.lead_time_sd_days)
            rows.append(plan_variant(item, AS_OF, "B", PlanParams()))
        assert rows[1].reorder_point >= rows[0].reorder_point, seed


# ------------------------------------------------------------------------------------------------ restock dates (the alert cycle)

def test_the_last_restock_is_the_latest_day_the_quantity_rose():
    snaps = {date(2026, 11, 1): 10, date(2026, 11, 2): 8, date(2026, 11, 3): 30, date(2026, 11, 4): 27, date(2026, 11, 5): 40, date(2026, 11, 6): 40}
    assert last_restock(snaps) == date(2026, 11, 5)
    assert last_restock({date(2026, 11, 1): 5, date(2026, 11, 2): 4}) is None
    assert last_restock({}) is None
    assert last_restock({date(2026, 11, 1): 5}) is None
