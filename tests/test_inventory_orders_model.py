"""The purchase order rules that need no database (app/inventory/orders.py) and how the plan reads open orders (app/inventory/model.py): hand worked cases, then properties over
seeded random worlds.

Three rules carry the module and each gets a property, not only an example:
  * a status move is legal only if the table says so, a refused move is loud and leaves the order as it was, and a received or cancelled order never moves again;
  * what is received on a line never exceeds what was ordered unless an over-receipt was explicitly accepted, and the open quantity is never negative;
  * inbound stock is exactly the quantity still to arrive on open orders: a draft, a cancelled order and a received line add nothing, and an order on the way stops the plan from
    suggesting a second one.
The hand worked case is the one in docs/inventory-planning.md: a variant that sells 2 a day, 10 on the shelf, a 9 day lead time, a minimum of 50 and packs of 12.
"""
from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.inventory import orders  # noqa: E402
from app.inventory.alerts import shortage_kind  # noqa: E402
from app.inventory.model import PlanParams, Promo, VariantInput, date_range, plan_variant, projected_cover  # noqa: E402
from app.inventory.orders import (  # noqa: E402
    Arrival, BadQuantity, IllegalMove, NotAllowedInStatus, OverReceipt, arrivals_by_variant, can_move, check_move, expected_from_lead, is_late, next_statuses, open_quantity,
    receive_line, status_after_receipt)

AS_OF = date(2026, 11, 24)
WINDOW = date_range(AS_OF - timedelta(days=28), AS_OF)

# ------------------------------------------------------------------------------------------------ the transition table

LEGAL = {
    ("draft", "ordered"), ("draft", "cancelled"),
    ("ordered", "confirmed"), ("ordered", "shipped"), ("ordered", "partly_received"), ("ordered", "received"), ("ordered", "cancelled"),
    ("confirmed", "shipped"), ("confirmed", "partly_received"), ("confirmed", "received"), ("confirmed", "cancelled"),
    ("shipped", "partly_received"), ("shipped", "received"),
    ("partly_received", "received"), ("partly_received", "cancelled"),
}


def test_the_transition_table_is_exactly_the_written_one():
    """Every pair of statuses, 49 of them: legal when listed here and illegal otherwise. A status cannot move to itself."""
    for a in orders.STATUSES:
        for b in orders.STATUSES:
            assert can_move(a, b) == ((a, b) in LEGAL), (a, b)
    assert len(orders.STATUSES) == 7 and len(LEGAL) == 15


def test_a_received_or_cancelled_order_never_moves_again():
    for end in orders.TERMINAL_STATUSES:
        assert orders.TRANSITIONS[end] == frozenset() and next_statuses(end) == []


def test_every_status_after_draft_can_be_reached_from_draft_and_none_leads_back_to_draft():
    reached, frontier = {"draft"}, ["draft"]
    while frontier:
        for to in orders.TRANSITIONS[frontier.pop()]:
            if to not in reached:
                reached.add(to)
                frontier.append(to)
    assert reached == set(orders.STATUSES)
    assert all("draft" not in orders.TRANSITIONS[s] for s in orders.STATUSES)


def test_a_shipped_order_is_not_cancelled_and_a_draft_cannot_skip_to_shipped():
    assert not can_move("shipped", "cancelled") and not can_move("draft", "shipped") and not can_move("draft", "received")


def test_a_person_moves_an_order_to_neither_receipt_status_and_is_told_to_record_a_receipt():
    for src in ("ordered", "confirmed", "shipped", "partly_received"):
        for to in orders.RECEIPT_ONLY:
            if can_move(src, to):
                with pytest.raises(IllegalMove, match="receipt"):
                    check_move(src, to)
    assert next_statuses("ordered") == ["confirmed", "shipped", "cancelled"]
    assert next_statuses("partly_received") == ["cancelled"] and next_statuses("draft") == ["ordered", "cancelled"]


def test_an_illegal_or_unknown_move_is_refused_with_its_own_code():
    with pytest.raises(IllegalMove, match="cannot move from received to ordered") as err:
        check_move("received", "ordered")
    assert err.value.code == "PO001"
    with pytest.raises(IllegalMove):
        check_move("draft", "lost")
    assert orders.ERROR_BY_CODE["PO004"] is OverReceipt and orders.ERROR_BY_CODE["PO003"] is NotAllowedInStatus


# ------------------------------------------------------------------------------------------------ receipts

def test_receiving_adds_to_the_line_and_refuses_more_than_ordered_unless_accepted():
    assert receive_line(60, 0, 25) == (25, False)
    assert receive_line(60, 25, 35) == (60, False)                        # exactly in full is not over
    with pytest.raises(OverReceipt, match="61 of 60"):
        receive_line(60, 25, 36)
    assert receive_line(60, 25, 36, allow_over=True) == (61, True)         # accepted, and flagged
    with pytest.raises(BadQuantity):
        receive_line(60, 0, 0)
    with pytest.raises(BadQuantity):
        receive_line(60, 0, -3, allow_over=True)


def test_the_open_quantity_is_never_negative():
    assert open_quantity(60, 25) == 35 and open_quantity(60, 60) == 0 and open_quantity(60, 75) == 0


def test_the_status_after_a_receipt_is_received_only_when_every_line_is_in_full():
    assert status_after_receipt("shipped", [(10, 10), (5, 5)]) == "received"
    assert status_after_receipt("shipped", [(10, 10), (5, 4)]) == "partly_received"
    assert status_after_receipt("ordered", [(10, 1)]) == "partly_received"
    assert status_after_receipt("partly_received", [(10, 12), (5, 5)]) == "received"      # an accepted over-receipt is in full
    for closed in ("draft", "received", "cancelled"):
        with pytest.raises(NotAllowedInStatus):
            status_after_receipt(closed, [(1, 0)])


def _random_order(rng: random.Random):
    return {"status": "draft", "lines": [[rng.randint(1, 40), 0, False] for _ in range(rng.randint(1, 4))]}


def _step(rng: random.Random, o: dict) -> None:
    """One random operation on an in-memory order, through the rules. A refused one must leave the order exactly as it was."""
    before = (o["status"], [list(l) for l in o["lines"]])
    kind = rng.choice(("move", "move", "receive", "receive", "receive"))
    try:
        if kind == "move":
            to = rng.choice(orders.STATUSES)
            check_move(o["status"], to)
            o["status"] = to
        else:
            i = rng.randrange(len(o["lines"]))
            ordered, received, _flag = o["lines"][i]
            qty = rng.choice((1, 2, 5, 17, ordered - received, ordered, 0))
            allow = rng.random() < 0.25
            status_after_receipt(o["status"], [(l[0], l[1]) for l in o["lines"]])           # an order that cannot receive is refused first
            total, over = receive_line(ordered, received, qty, allow)
            o["lines"][i] = [ordered, total, over or o["lines"][i][2]]
            o["status"] = status_after_receipt(o["status"], [(l[0], l[1]) for l in o["lines"]])
    except orders.OrderError:
        assert (o["status"], [list(l) for l in o["lines"]]) == before, "a refused operation changed the order"


def test_property_random_orders_keep_every_invariant_over_many_steps():
    moves_made = refusals = 0
    for seed in range(300):
        rng = random.Random(seed)
        o = _random_order(rng)
        seen = [o["status"]]
        for _ in range(60):
            previous = o["status"]
            _step(rng, o)
            if o["status"] != previous:
                moves_made += 1
                assert can_move(previous, o["status"]), (seed, previous, o["status"])            # no illegal move ever happened, however it was reached
                seen.append(o["status"])
            else:
                refusals += 1
            for ordered, received, flag in o["lines"]:
                assert received >= 0 and open_quantity(ordered, received) >= 0, seed
                assert received <= ordered or flag, (seed, ordered, received)                     # over-receipt only when accepted
                assert not flag or received > ordered, seed
            if o["status"] == "received":
                assert all(r >= q for q, r, _f in o["lines"]), seed
            if o["status"] == "draft":
                assert all(r == 0 for _q, r, _f in o["lines"]), seed                              # nothing is received on a draft
        end = o["status"]
        if end in orders.TERMINAL_STATUSES:
            assert seen.count(end) == 1 and seen[-1] == end, seed                                 # it arrived once and stayed
    assert moves_made > 500 and refusals > 500                                                    # the walks moved and were refused often: the property had something to bite on


# ------------------------------------------------------------------------------------------------ expected date and lateness

def test_the_expected_date_is_the_order_date_plus_the_lead_time_then():
    assert expected_from_lead(date(2026, 11, 24), 21) == date(2026, 12, 15)
    assert expected_from_lead(date(2026, 11, 24), 0) == date(2026, 11, 24)
    assert expected_from_lead(date(2026, 11, 24), None) is None


@pytest.mark.parametrize("status,expected_on,open_units,as_of,late", [
    ("ordered", date(2026, 11, 23), 5, AS_OF, True),
    ("shipped", date(2026, 11, 1), 1, AS_OF, True),
    ("partly_received", date(2026, 11, 20), 4, AS_OF, True),
    ("confirmed", date(2026, 11, 24), 5, AS_OF, False),            # expected today is not late yet
    ("ordered", date(2026, 11, 25), 5, AS_OF, False),
    ("ordered", date(2026, 11, 1), 0, AS_OF, False),               # nothing left to arrive
    ("ordered", None, 5, AS_OF, False),                            # no date, so it cannot be late
    ("draft", date(2026, 11, 1), 5, AS_OF, False),
    ("received", date(2026, 11, 1), 5, AS_OF, False),
    ("cancelled", date(2026, 11, 1), 5, AS_OF, False),
])
def test_when_an_order_is_late(status, expected_on, open_units, as_of, late):
    assert is_late(status, expected_on, open_units, as_of) is late


# ------------------------------------------------------------------------------------------------ inbound stock

def test_inbound_is_the_open_quantity_of_open_orders_only():
    rows = [
        ("tee", "draft", 100, 0, date(2026, 12, 1)),                  # nothing was placed
        ("tee", "ordered", 60, 0, date(2026, 12, 1)),
        ("tee", "partly_received", 40, 15, date(2026, 12, 5)),
        ("tee", "received", 30, 30, date(2026, 11, 1)),
        ("tee", "cancelled", 50, 0, date(2026, 12, 1)),
        ("mug", "shipped", 10, 12, None),                             # over-received: nothing is open, never negative
        ("mug", "confirmed", 8, 0, None),
    ]
    got = arrivals_by_variant(rows)
    assert got == {"tee": [Arrival(60, date(2026, 12, 1)), Arrival(25, date(2026, 12, 5))], "mug": [Arrival(8, None)]}


def test_property_inbound_equals_the_open_quantity_of_open_orders_over_random_worlds():
    for seed in range(300):
        rng = random.Random(seed)
        rows = []
        for _ in range(rng.randint(0, 25)):
            ordered = rng.randint(1, 50)
            rows.append((rng.choice(("a", "b", "c")), rng.choice(orders.STATUSES), ordered, rng.choice((0, 0, rng.randint(0, ordered), ordered + rng.randint(0, 5))),
                         rng.choice((None, AS_OF + timedelta(days=rng.randint(-10, 40))))))
        got = arrivals_by_variant(rows)
        for variant in "abc":
            expected = sum(max(q - r, 0) for v, s, q, r, _d in rows if v == variant and s in orders.OPEN_STATUSES)
            assert sum(a.quantity for a in got.get(variant, [])) == expected, seed
            assert all(a.quantity > 0 for a in got.get(variant, [])), seed


# ------------------------------------------------------------------------------------------------ the plan reads open orders

def _item(**over) -> VariantInput:
    """The doc's case: 2 a day with a standard deviation of 1 over a window on the shelf, 10 on hand, a 9 day lead time, minimum 50, packs of 12."""
    sales = {d: (4 if i % 2 == 0 else 0) for i, d in enumerate(WINDOW)}
    base = dict(key="v", available=10, snapshots={d: 9 for d in WINDOW}, sales={d: q for d, q in sales.items() if q}, units_all=56, revenue_minor=56000, lead_time_days=9, moq=50, pack_size=12)
    return VariantInput(**(base | over))


def test_the_baseline_without_an_order_is_short_and_suggests_sixty():
    row = plan_variant(_item(), AS_OF, "A", PlanParams())
    assert row.suggested_qty == 60 and row.reorder_point == 18 + row.safety_stock and row.reorder_point > 10      # the shelf holds 10, under the reorder point
    assert shortage_kind(row.status, row.velocity.mean, row.available, row.lead_time_days, row.alert_cover_days, row.forecast_cover_days) == "cover_below_lead_time"
    assert (row.inbound_source, row.incoming, row.inbound_expected_on, row.inbound_late) == ("none", 0, None, False)


def test_an_order_on_the_way_before_the_shelf_empties_raises_no_shortage_and_no_second_order():
    """60 expected in 3 days, the shelf (5 days of cover) lasts until then: position 70 is over the reorder point, and the cover with the arrival is 35 days."""
    arrival = Arrival(60, AS_OF + timedelta(days=3))
    row = plan_variant(_item(arrivals=(arrival,)), AS_OF, "A", PlanParams())
    assert row.incoming == 60 and row.inbound_source == "orders" and row.inbound_expected_on == AS_OF + timedelta(days=3) and not row.inbound_late
    assert not row.needs_reorder and row.suggested_qty == 0
    assert row.cover_days == pytest.approx(5.0)                              # the plain cover is unchanged
    assert row.alert_cover_days == pytest.approx(35.0) and row.forecast_cover_days == pytest.approx(35.0)
    assert shortage_kind(row.status, row.velocity.mean, row.available, row.lead_time_days, row.alert_cover_days, row.forecast_cover_days) is None


def test_an_order_that_lands_after_the_shelf_is_empty_still_shows_the_shortage_but_suggests_no_second_order():
    """The same 60, expected on day 8: the shelf is empty on day 5, so a shortage is real (the arrival is too late to help), yet ordering the same goods again is not the answer."""
    row = plan_variant(_item(arrivals=(Arrival(60, AS_OF + timedelta(days=8)),)), AS_OF, "A", PlanParams())
    assert row.alert_cover_days == pytest.approx(5.0)
    assert shortage_kind(row.status, row.velocity.mean, row.available, row.lead_time_days, row.alert_cover_days, row.forecast_cover_days) == "cover_below_lead_time"
    assert not row.needs_reorder and row.suggested_qty == 0


def test_an_order_past_its_date_counts_as_stock_on_order_but_is_not_assumed_to_land():
    row = plan_variant(_item(arrivals=(Arrival(60, AS_OF - timedelta(days=4)),)), AS_OF, "A", PlanParams())
    assert row.incoming == 60 and row.inbound_late and row.inbound_expected_on == AS_OF - timedelta(days=4)
    assert not row.needs_reorder and row.suggested_qty == 0                  # no second order for goods already ordered
    assert row.alert_cover_days == pytest.approx(5.0) and row.forecast_cover_days == pytest.approx(5.0)       # but the plan does not count on them landing


def test_an_order_with_no_date_adds_to_the_position_and_not_to_the_cover():
    row = plan_variant(_item(arrivals=(Arrival(60, None),)), AS_OF, "A", PlanParams())
    assert row.incoming == 60 and row.inbound_expected_on is None and not row.needs_reorder
    assert row.alert_cover_days == pytest.approx(5.0)


def test_orders_replace_shopifys_incoming_figure_and_are_never_added_to_it():
    both = plan_variant(_item(incoming=45, arrivals=(Arrival(20, AS_OF + timedelta(days=3)),)), AS_OF, "A", PlanParams())
    assert both.incoming == 20 and both.inbound_source == "orders"
    only_shopify = plan_variant(_item(incoming=45), AS_OF, "A", PlanParams())
    assert only_shopify.incoming == 45 and only_shopify.inbound_source == "shopify" and only_shopify.inbound_expected_on is None
    assert only_shopify.alert_cover_days == pytest.approx(5.0)               # Shopify gives no date, so it lengthens nothing


def test_two_open_orders_add_up_and_the_earliest_date_is_reported():
    row = plan_variant(_item(arrivals=(Arrival(30, AS_OF + timedelta(days=10)), Arrival(25, AS_OF + timedelta(days=4)))), AS_OF, "A", PlanParams())
    assert row.incoming == 55 and row.inbound_expected_on == AS_OF + timedelta(days=4)


def test_projected_cover_adds_an_arrival_at_the_start_of_its_day():
    assert projected_cover(10, 2.0, AS_OF, (), arrivals=[Arrival(20, AS_OF + timedelta(days=5))]) == pytest.approx(5.0)      # empties exactly as it lands: the order is too late
    assert projected_cover(10, 2.0, AS_OF, (), arrivals=[Arrival(20, AS_OF + timedelta(days=4))]) == pytest.approx(15.0)
    assert projected_cover(10, 2.0, AS_OF, (), arrivals=[Arrival(20, AS_OF - timedelta(days=1))]) == pytest.approx(5.0)       # already late: not counted
    assert projected_cover(0, 2.0, AS_OF, (), arrivals=[Arrival(20, AS_OF)]) == 0.0                                           # an empty shelf now is zero cover


def test_a_promotion_and_an_arrival_work_together():
    promo = [Promo(AS_OF, AS_OF + timedelta(days=100), 2.0)]
    # 4 a day under the promotion: 10 lasts 2.5 days, a 40 unit order on day 2 (stock left 2) gives 42 and it runs out on day 2 + 42 / 4 = 12.5
    assert projected_cover(10, 2.0, AS_OF, promo, arrivals=[Arrival(40, AS_OF + timedelta(days=2))]) == pytest.approx(12.5)


def test_property_adding_an_open_order_never_raises_the_suggestion_and_never_shortens_the_cover():
    checked = 0
    for seed in range(300):
        rng = random.Random(seed)
        sales = {d: rng.randint(1, 6) for d in WINDOW if rng.random() < rng.choice((0.3, 0.7, 1.0))}
        item = VariantInput(key="v", available=rng.randint(0, 80), snapshots={d: rng.choice((0, 9, 30)) for d in WINDOW if rng.random() < 0.5}, sales=sales,
                            units_all=sum(sales.values()), revenue_minor=1000 * sum(sales.values()), lead_time_days=rng.randint(1, 45), lead_time_sd_days=rng.choice((0.0, 2.0, 6.0)),
                            moq=rng.choice((1, 10, 40)), pack_size=rng.choice((1, 6, 12)))
        arrival = Arrival(rng.randint(1, 120), rng.choice((None, AS_OF + timedelta(days=rng.randint(-10, 60)))))
        without = plan_variant(item, AS_OF, "A", PlanParams())
        with_order = plan_variant(VariantInput(**{**item.__dict__, "arrivals": (arrival,)}), AS_OF, "A", PlanParams())
        assert with_order.suggested_qty <= without.suggested_qty, seed
        assert not (with_order.needs_reorder and not without.needs_reorder), seed
        assert with_order.incoming == arrival.quantity, seed
        if without.alert_cover_days is not None and with_order.alert_cover_days is not None:
            assert with_order.alert_cover_days >= without.alert_cover_days - 1e-9, seed
        if without.forecast_cover_days is not None and with_order.forecast_cover_days is not None:
            assert with_order.forecast_cover_days >= without.forecast_cover_days - 1e-9, seed
        checked += 1
    assert checked == 300


def test_the_plan_with_no_orders_is_the_plan_it_was_before_orders_existed():
    """The existing figures do not move: the doc's case with Shopify's incoming quantity and no orders reads as it always did."""
    row = plan_variant(_item(incoming=7), AS_OF, "A", PlanParams())
    assert row.incoming == 7 and row.inbound_source == "shopify"
    assert row.needs_reorder and 17 <= row.reorder_point                    # position 17 is under the reorder point
    assert row.suggested_qty == 60                                          # a target a little over 50 less 17 on hand and on the way, raised to the minimum 50 and to packs of 12
