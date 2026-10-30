"""Price watch: shipping (stated, estimated, unestimatable) and the daily report. Pure, no database, no browser.

The guards pinned here, each with a mutation control in tests/mutation_check.py:
  * an estimate is labelled ESTIMATED and never presented as the competitor's own figure;
  * a proposed price never goes below the product's floor;
  * a different currency, an out of stock competitor, a missing price and unknown shipping are excluded with a reason, not compared.
"""
from __future__ import annotations

import csv
import io
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.price_watch.report import Observation, ProductRow, ReviewItem, build_report, render_csv, render_markdown  # noqa: E402
from app.price_watch.shipping import ESTIMATED, STATED, UNKNOWN, RateBand, estimate_shipping, resolve_shipping  # noqa: E402

DAY = date(2026, 10, 30)
BANDS = [RateBand("domestic", 0, 500, 850, "AUD"), RateBand("domestic", 500, 2000, 1200, "AUD"), RateBand("remote", 0, 2000, 2500, "AUD"),
         RateBand("domestic", 0, 500, 700, "NZD")]


def product(**kw):
    base = dict(id="p1", sku="KTB-750", title="Bottle 750ml", price_minor=4995, currency="AUD", floor_minor=3800, our_shipping_minor=0, weight_grams=480)
    base.update(kw)
    return ProductRow(**base)


def obs(**kw):
    base = dict(listing_id="l1", product_id="p1", competitor="Depot", url="http://x/1", ship_zone="domestic", observed_on=DAY, price_minor=4100,
                currency="AUD", availability="in_stock", shipping_minor=695, problem=None)
    base.update(kw)
    return Observation(**base)


# ---- shipping ----------------------------------------------------------------------------------------------------------

def test_a_stated_figure_is_used_as_the_competitors_own_and_labelled_so():
    got = resolve_shipping(695, "AUD", 480, "domestic", BANDS)
    assert (got.source, got.cost_minor, got.label) == (STATED, 695, "stated by competitor")


def test_free_shipping_is_a_stated_zero_not_a_missing_figure():
    got = resolve_shipping(0, "AUD", None, "domestic", BANDS)     # no weight needed when the page said it
    assert (got.source, got.cost_minor) == (STATED, 0)


def test_a_missing_figure_is_estimated_from_the_weight_band_and_labelled_estimated():
    got = resolve_shipping(None, "AUD", 480, "domestic", BANDS)
    assert (got.source, got.cost_minor, got.label) == (ESTIMATED, 850, "ESTIMATED")
    assert resolve_shipping(None, "AUD", 500, "domestic", BANDS).cost_minor == 1200      # the upper edge of a band belongs to the next band
    assert resolve_shipping(None, "AUD", 480, "remote", BANDS).cost_minor == 2500        # the zone picks the table


def test_without_a_weight_there_is_no_estimate():
    got = resolve_shipping(None, "AUD", None, "domestic", BANDS)
    assert got.source == UNKNOWN and got.cost_minor is None and "weight" in got.reason
    assert resolve_shipping(None, "AUD", 0, "domestic", BANDS).source == UNKNOWN


def test_no_band_for_the_weight_the_zone_or_the_currency_is_unknown_not_a_nearby_band():
    assert estimate_shipping(5000, "domestic", "AUD", BANDS).source == UNKNOWN
    assert "no rate table for zone" in estimate_shipping(100, "mars", "AUD", BANDS).reason
    assert estimate_shipping(100, "domestic", "USD", BANDS).source == UNKNOWN
    assert estimate_shipping(100, "domestic", "NZD", BANDS).cost_minor == 700           # the NZD band is not mixed with the AUD one


def test_two_bands_that_both_fit_are_unknown_rather_than_the_cheaper_one():
    overlapping = BANDS + [RateBand("domestic", 400, 900, 999, "AUD")]
    got = estimate_shipping(480, "domestic", "AUD", overlapping)
    assert got.source == UNKNOWN and "overlap" in got.reason


# ---- the report --------------------------------------------------------------------------------------------------------

def test_a_lower_landed_price_is_reported_and_the_proposal_matches_it():
    r = build_report(DAY, [product()], [obs()], BANDS)
    (line,) = r.lines
    assert (line.comp_landed_minor, line.our_landed_minor, line.gap_minor) == (4795, 4995, 200)
    assert line.proposed_price_minor == 4795 and not line.floor_limited
    assert line.shipping_source == STATED


def test_landed_price_not_the_sticker_price_decides():
    # their sticker price is lower than ours (4100 < 4995) but with 1000 shipping their landed price is higher
    r = build_report(DAY, [product()], [obs(shipping_minor=1000)], BANDS)
    assert r.lines == [] and r.not_lower == 1 and r.compared == 1


def test_our_own_shipping_is_part_of_our_landed_price():
    # their landed 5100 is above our sticker price 4995, so on sticker prices we are cheaper; our 900 shipping makes our landed 5895
    r = build_report(DAY, [product(our_shipping_minor=900)], [obs(price_minor=4400, shipping_minor=700)], BANDS)
    (line,) = r.lines
    assert (line.comp_landed_minor, line.our_landed_minor) == (5100, 5895)
    # to land at 5100 with our 900 shipping on top we charge 4200 for the item
    assert line.proposed_price_minor == 4200
    assert build_report(DAY, [product(our_shipping_minor=0)], [obs(price_minor=4400, shipping_minor=700)], BANDS).lines == []


def test_the_proposed_price_never_goes_below_the_floor():
    r = build_report(DAY, [product(floor_minor=4500)], [obs(price_minor=3000, shipping_minor=0)], BANDS)
    (line,) = r.lines
    assert line.proposed_price_minor == 4500 and line.floor_limited
    assert "floor-limited" in line.note and "30.00" in line.note
    for floor in range(0, 6000, 250):
        for theirs in range(500, 6000, 250):
            rep = build_report(DAY, [product(floor_minor=floor)], [obs(price_minor=theirs, shipping_minor=0)], BANDS)
            for l in rep.lines:
                assert l.proposed_price_minor is None or l.proposed_price_minor >= floor


def test_a_floor_at_or_above_our_price_proposes_nothing_but_still_reports_the_gap():
    r = build_report(DAY, [product(floor_minor=4995)], [obs(price_minor=3000, shipping_minor=0)], BANDS)
    (line,) = r.lines
    assert line.proposed_price_minor is None and "nothing can be proposed" in line.note
    assert r.best_by_product()["p1"].proposed_price_minor is None


def test_the_proposal_is_never_above_the_current_price():
    r = build_report(DAY, [product()], [obs(price_minor=4990, shipping_minor=0)], BANDS)
    assert all(l.proposed_price_minor < l.our_price_minor for l in r.lines if l.proposed_price_minor is not None)


def test_an_estimated_shipping_figure_is_labelled_in_every_render_and_in_the_note():
    r = build_report(DAY, [product()], [obs(price_minor=3000, shipping_minor=None)], BANDS)
    (line,) = r.lines
    assert line.shipping_source == ESTIMATED and line.comp_shipping_minor == 850
    assert "based on ESTIMATED shipping" in line.note
    md = render_markdown(r)
    assert "8.50 ESTIMATED" in md and "(stated)" not in md
    rows = list(csv.DictReader(io.StringIO(render_csv(r))))
    assert rows[0]["shipping_source"] == "ESTIMATED"


def test_a_stated_figure_is_never_labelled_estimated():
    r = build_report(DAY, [product()], [obs()], BANDS)
    assert "6.95 (stated)" in render_markdown(r) and "ESTIMATED" not in render_markdown(r).replace("Shipping marked ESTIMATED is our estimate", "")
    assert list(csv.DictReader(io.StringIO(render_csv(r))))[0]["shipping_source"] == "stated"


def test_what_cannot_be_compared_is_excluded_with_its_reason():
    cases = {
        "price": obs(price_minor=None, problem="no price found"),
        "zero": obs(price_minor=0),
        "currency": obs(currency="EUR"),
        "no currency": obs(currency=None),
        "stock": obs(availability="out_of_stock"),
        "stale": obs(observed_on=DAY - timedelta(days=3)),
        "shipping": obs(shipping_minor=None),
    }
    heavy = product(weight_grams=None)       # with no weight the missing shipping cannot be estimated either
    for name, o in cases.items():
        r = build_report(DAY, [heavy], [o], BANDS)
        assert r.lines == [] and r.compared == 0 and len(r.excluded) == 1, name
    reasons = {n: build_report(DAY, [heavy], [o], BANDS).excluded[0].reason for n, o in cases.items()}
    assert "no price" in reasons["price"] and "currency EUR against our AUD" in reasons["currency"]
    assert "out of stock" in reasons["stock"] and "not scanned" in reasons["stale"] and "weight" in reasons["shipping"]


def test_an_observation_for_a_product_that_is_not_in_the_catalogue_is_ignored():
    assert build_report(DAY, [product()], [obs(product_id="gone")], BANDS).excluded == []


def test_one_proposal_per_product_from_the_lowest_landed_price_with_stated_winning_a_tie():
    r = build_report(DAY, [product()], [
        obs(listing_id="a", competitor="Alpha", price_minor=4300, shipping_minor=0),                  # 4300 stated
        obs(listing_id="b", competitor="Beta", price_minor=3800, shipping_minor=None),                # 3800 + 850 estimated = 4650
        obs(listing_id="c", competitor="Gamma", price_minor=3450, shipping_minor=850)], BANDS)        # 4300 stated, ties Alpha
    best = r.best_by_product()["p1"]
    assert len(r.lines) == 3 and best.comp_landed_minor == 4300 and best.shipping_source == STATED and best.competitor == "Alpha"


def test_a_tie_between_stated_and_estimated_goes_to_the_stated_one():
    r = build_report(DAY, [product()], [obs(listing_id="a", competitor="Alpha", price_minor=3450, shipping_minor=None),   # 3450 + 850
                                        obs(listing_id="b", competitor="Beta", price_minor=3450, shipping_minor=850)], BANDS)
    assert r.best_by_product()["p1"].competitor == "Beta"


def test_the_markdown_and_csv_for_a_seeded_day():
    products = [product(), product(id="p2", sku="LMP-210", title="Lamp", price_minor=3450, floor_minor=2200, weight_grams=150),
                product(id="p3", sku="PCS-1", title="Stove", price_minor=5900, floor_minor=4200)]
    observations = [obs(), obs(listing_id="l2", product_id="p2", competitor="Depot", price_minor=2490, shipping_minor=None),
                    obs(listing_id="l3", product_id="p3", competitor="Depot", price_minor=5400, shipping_minor=1250)]
    r = build_report(DAY, products, observations, BANDS, [ReviewItem("Alpen", "http://x/9", "title score 0.72")])
    assert r.summary() == {"lower": 2, "compared": 3, "not_lower": 1, "excluded": 0, "needs_review": 1, "estimated_shipping": 1, "floor_limited": 0}
    assert [l.sku for l in r.lines] == ["KTB-750", "LMP-210"]          # biggest gap first (200 against 110)
    md = render_markdown(r)
    assert md.startswith("# Competitor price report, 2026-10-30")
    assert "2 product lines where a competitor's landed price is lower than ours, out of 3 compared" in md
    assert "## Matches that need a person" in md and "Alpen (http://x/9): title score 0.72" in md
    rows = list(csv.DictReader(io.StringIO(render_csv(r))))
    assert [row["sku"] for row in rows] == ["KTB-750", "LMP-210"]
    assert rows[0]["their_landed"] == "47.95" and rows[0]["proposed_price"] == "47.95" and rows[0]["shipping_source"] == "stated"
    assert rows[1]["their_landed"] == "33.40" and rows[1]["proposed_price"] == "33.40" and rows[1]["shipping_source"] == "ESTIMATED"


def test_an_empty_day_says_so():
    md = render_markdown(build_report(DAY, [product()], [], BANDS))
    assert "No competitor was cheaper on landed price today." in md


def test_a_spreadsheet_formula_in_a_cell_is_made_text():
    r = build_report(DAY, [product(title="=HYPERLINK(\"http://evil\")")], [obs()], BANDS)
    assert list(csv.DictReader(io.StringIO(render_csv(r))))[0]["title"].startswith("'=")
