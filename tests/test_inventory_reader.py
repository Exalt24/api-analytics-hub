"""Reading variants, stock levels and order lines through the existing Shopify connector, against a stand-in written from the documented response shapes (tests/inventory_fixtures.py).

What is pinned: stock is summed over locations and every location is kept; a level that does not report `available` is refused (a guessed zero would read as a stockout); cost is read as
exact minor units; paging follows the cursor; a cancelled order is skipped; an order with over 100 lines is read to the end; the quantity standing after an edit is the one counted; and the
revenue of a line is its discounted unit price times that quantity. The reader never sends a mutation.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from inventory_fixtures import TODAY, FakeShop, days_ago, gid, line_node, order_node, variant_node  # noqa: E402

from app.connectors.base import UpstreamBroken  # noqa: E402
from app.connectors.shopify import ShopifyConnector  # noqa: E402
from app.inventory import reader  # noqa: E402
from app.inventory.reader import ShopifyInventoryReader, parse_variant  # noqa: E402


def make_reader(shop: FakeShop) -> ShopifyInventoryReader:
    return ShopifyInventoryReader(ShopifyConnector(shop_domain="stand-in.myshopify.com", access_token="shpat_standin", client=shop.client(), page_size=100))


def run(coro):
    return asyncio.run(coro)


def test_stock_is_summed_over_locations_and_each_location_is_kept():
    shop = FakeShop([variant_node(1, product="Tee", title="Small", sku="TEE-S", available=7, incoming=3, locations=2)], [])
    [v] = run(make_reader(shop).read_variants())
    assert (v.variant_gid, v.sku, v.product_title, v.variant_title, v.tracked) == (gid("ProductVariant", 1), "TEE-S", "Tee", "Small", True)
    assert (v.available, v.on_hand, v.committed, v.incoming) == (7, 7, 0, 3)
    assert [(l.location_gid, l.available) for l in v.levels] == [(gid("Location", 900), 7), (gid("Location", 901), 0)]


def test_a_unit_cost_is_exact_minor_units_and_a_missing_cost_is_none():
    shop = FakeShop([variant_node(1, product="A", cost="12.35"), variant_node(2, product="B")], [])
    a, b = run(make_reader(shop).read_variants())
    assert (a.unit_cost_minor, a.currency) == (1235, "USD")
    assert (b.unit_cost_minor, b.currency) == (None, None)


def test_a_level_that_does_not_report_available_is_refused_rather_than_read_as_zero():
    with pytest.raises(UpstreamBroken, match="no available quantity"):
        parse_variant(variant_node(1, product="A", withhold_available=True))


def test_an_untracked_variant_is_marked_and_a_sku_less_variant_has_none():
    [v] = run(make_reader(FakeShop([variant_node(1, product="Gift card", tracked=False)], [])).read_variants())
    assert v.tracked is False and v.sku is None


def test_variants_are_paged_to_the_end():
    shop = FakeShop([variant_node(n, product="P%d" % n) for n in range(1, 8)], [], page_size=3)
    got = run(make_reader(shop).read_variants())
    assert [v.variant_gid for v in got] == [gid("ProductVariant", n) for n in range(1, 8)]
    assert len([q for q in shop.requests if q.startswith("query Variants")]) == 3


def test_order_lines_use_the_discounted_price_and_the_quantity_that_still_stands():
    order = order_node(1, days_ago(3), [line_node(1, 1, 3, "19.99"), line_node(2, 2, 5, "4.50", current=2), line_node(3, None, 1, "100.00", sku="CUSTOM")])
    lines, orders = run(make_reader(FakeShop([], [order])).read_order_lines(date(2026, 11, 1), TODAY))
    assert orders == 1
    by = {l.line_gid: l for l in lines}
    assert (by[gid("LineItem", 1)].quantity, by[gid("LineItem", 1)].net_revenue_minor) == (3, 5997)         # 19.99 stays exact: 3 * 1999
    assert (by[gid("LineItem", 2)].quantity, by[gid("LineItem", 2)].net_revenue_minor) == (2, 900)          # an edit took 3 of the 5 away
    assert by[gid("LineItem", 3)].variant_gid is None and by[gid("LineItem", 3)].sku == "CUSTOM"
    assert {l.processed_on for l in lines} == {date(2026, 11, 21)}


def test_a_cancelled_order_contributes_nothing_and_the_window_is_half_open():
    orders = [order_node(1, days_ago(2), [line_node(1, 1, 4)], cancelled=True), order_node(2, days_ago(2), [line_node(2, 1, 1)]), order_node(3, days_ago(0), [line_node(3, 1, 9)])]
    lines, read = run(make_reader(FakeShop([], orders)).read_order_lines(date(2026, 11, 1), TODAY))
    assert [l.line_gid for l in lines] == [gid("LineItem", 2)]
    assert read == 2                                    # today (the 24th) is outside [1st, 24th): the cancelled order was read and skipped, today's was never asked for


def test_an_order_with_more_than_a_hundred_lines_is_read_to_the_end():
    first = [line_node(n, 1, 1) for n in range(1, 101)]
    order = order_node(1, days_ago(1), first)
    order["lineItems"]["pageInfo"] = {"hasNextPage": True, "endCursor": "0"}
    shop = FakeShop([], [order])
    shop.extra_lines[order["id"]] = [line_node(n, 1, 1) for n in range(101, 131)]
    lines, _ = run(make_reader(shop).read_order_lines(date(2026, 11, 1), TODAY))
    assert len(lines) == 130 and len({l.line_gid for l in lines}) == 130


def test_an_order_page_boundary_loses_no_order():
    orders = [order_node(n, days_ago(2), [line_node(n, 1, 1)]) for n in range(1, 8)]
    lines, read = run(make_reader(FakeShop([], orders, page_size=3)).read_order_lines(date(2026, 11, 1), TODAY))
    assert read == 7 and len(lines) == 7


def test_a_line_with_no_price_is_refused():
    bad = line_node(1, 1, 1)
    bad["discountedUnitPriceSet"] = None
    with pytest.raises(UpstreamBroken, match="no unit price"):
        reader.parse_order_lines(order_node(1, days_ago(1), [bad]), [bad])


def test_the_reader_sends_only_queries_never_a_mutation():
    shop = FakeShop([variant_node(1, product="A")], [order_node(1, days_ago(1), [line_node(1, 1, 1)])])
    r = make_reader(shop)
    run(r.read_variants())
    run(r.read_order_lines(date(2026, 11, 1), TODAY))
    assert shop.requests and all(q.startswith("query") for q in shop.requests)
    assert "mutation" not in reader.VARIANTS_QUERY and "mutation" not in reader.ORDERS_QUERY and "mutation" not in reader.MORE_LINES_QUERY
    assert r.api_calls == 2
