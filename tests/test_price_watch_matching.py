"""Price watch: matching a competitor listing to our catalogue. Pure, no database and no browser.

The guard that matters is the first one: a match that is not certain is never made automatically. It goes to needs_review.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.price_watch import matching as m  # noqa: E402
from app.price_watch.matching import CatalogueItem, CompetitorItem, match_listing, normalise_gtin, title_score, tokens  # noqa: E402

BOTTLE = CatalogueItem("c-bottle", "KTB-750", "Kestrel Insulated Bottle 750ml", "9300000001016", "Kestrel")
BATTERY = CatalogueItem("c-battery", "MER-48V", "Meridian E-Bike Battery 48V 14Ah", None, "Meridian")
JUMPER = CatalogueItem("c-jumper", "HMJ-NVY-M", "Harbour Merino Jumper Navy M", None, "Harbour")
CATALOGUE = [BOTTLE, BATTERY, JUMPER]


def test_the_thresholds_are_pinned():
    assert (m.AUTO_THRESHOLD, m.REVIEW_THRESHOLD, m.SKU_TITLE_FLOOR, m.TIE_MARGIN) == (0.85, 0.60, 0.30, 0.03)
    assert m.REVIEW_THRESHOLD < m.AUTO_THRESHOLD and m.SIZE_CONFLICT_CAP < m.REVIEW_THRESHOLD and m.BRAND_MISSING_CAP < m.REVIEW_THRESHOLD


# ---- exact -------------------------------------------------------------------------------------------------------------

def test_a_valid_gtin_is_an_exact_match_whatever_the_title_says():
    got = match_listing(CompetitorItem("Completely different words", "THEIR-SKU", "9300000001016"), CATALOGUE)
    assert (got.status, got.catalogue_id, got.method, got.score) == ("matched", "c-bottle", "gtin", 1.0)


def test_gtin_forms_are_normalised_and_the_check_digit_is_verified():
    assert normalise_gtin("9300000001016") == "09300000001016"
    assert normalise_gtin("0 9300000001016") == "09300000001016"   # a 14 digit form with spaces
    assert normalise_gtin("9300000001017") is None       # wrong check digit
    assert normalise_gtin("12345") is None
    assert normalise_gtin("93000000010AB") is None


def test_an_invalid_gtin_is_ignored_not_trusted():
    got = match_listing(CompetitorItem("Kestrel Insulated Bottle 750ml", None, "9300000001017"), CATALOGUE)
    assert got.method == "title" and got.status == "matched"


def test_two_different_valid_gtins_mean_different_products_even_with_identical_titles():
    other = CompetitorItem("Kestrel Insulated Bottle 750ml", None, "9300000001030")
    got = match_listing(other, CATALOGUE)
    assert got.status == "no_match" and got.catalogue_id is None


def test_a_catalogue_that_repeats_a_gtin_is_a_review_not_a_guess():
    twin = CatalogueItem("c-twin", "KTB-750-B", "Kestrel Bottle", "9300000001016", "Kestrel")
    got = match_listing(CompetitorItem("x", None, "9300000001016"), [BOTTLE, twin])
    assert got.status == "needs_review" and got.catalogue_id is None


def test_an_equal_sku_with_a_similar_title_matches():
    got = match_listing(CompetitorItem("Harbour Merino Wool Jumper (Navy, M)", "hmj nvy-m"), CATALOGUE)
    assert (got.status, got.catalogue_id, got.method) == ("matched", "c-jumper", "sku")


def test_an_equal_sku_with_an_unrelated_title_goes_to_review():
    got = match_listing(CompetitorItem("Garden hose 20m", "HMJ-NVY-M"), CATALOGUE)
    assert got.status == "needs_review" and "same SKU" in got.reason


# ---- fuzzy -------------------------------------------------------------------------------------------------------------

def test_the_same_words_in_another_order_with_other_punctuation_match():
    got = match_listing(CompetitorItem("Meridian 48V 14Ah E-Bike Battery"), CATALOGUE)
    assert (got.status, got.catalogue_id, got.method) == ("matched", "c-battery", "title")


def test_spelling_variants_count_as_the_same_word():
    cat = [CatalogueItem("c-shampoo", "SH-500", "Acme Moisturising Shampoo 500ml", None, "Acme")]
    got = match_listing(CompetitorItem("ACME Moisturizing Shampoo - 500 ml"), cat)
    assert got.status == "matched" and got.score >= m.AUTO_THRESHOLD


def test_accents_and_case_do_not_matter():
    assert tokens("Crème Brûlée Torch") == ["creme", "brulee", "torch"]


def test_a_partial_overlap_is_a_review_not_a_match():
    got = match_listing(CompetitorItem("Harbour Merino Wool Jumper Navy M"), CATALOGUE)
    assert got.status == "needs_review" and got.catalogue_id == "c-jumper"
    assert m.REVIEW_THRESHOLD <= got.score < m.AUTO_THRESHOLD


# ---- near misses that must NOT match ----------------------------------------------------------------------------------

def test_same_brand_same_name_different_size_does_not_match():
    got = match_listing(CompetitorItem("Kestrel Insulated Bottle 500ml"), CATALOGUE)
    assert got.status == "no_match" and got.catalogue_id is None
    assert "size or model differs" in got.reason


@pytest.mark.parametrize("theirs", [
    "Meridian E-Bike Battery 36V 14Ah",       # a different voltage
    "Meridian E-Bike Battery 48V 10Ah",       # a different capacity
    "Meridian E-Bike Battery 48V 14Ah 2 pack",  # a bundle: extra number token
])
def test_a_different_model_number_never_matches(theirs):
    got = match_listing(CompetitorItem(theirs), [BATTERY])
    assert got.status != "matched"


def test_a_long_title_with_one_size_changed_is_still_held_back():
    cat = [CatalogueItem("c-1", "S-1", "Acme Deep Moisturising Argan Oil Shampoo For Dry Damaged Hair 500ml", None, "Acme")]
    assert match_listing(CompetitorItem("Acme Deep Moisturising Argan Oil Shampoo For Dry Damaged Hair 250ml"), cat).status == "no_match"


def test_the_right_words_under_another_brand_do_not_match():
    got = match_listing(CompetitorItem("Zeta Insulated Bottle 750ml"), [BOTTLE])
    assert got.status == "no_match" and "brand" in got.reason


def test_nothing_in_common_is_no_match():
    assert match_listing(CompetitorItem("Garden hose 20m"), CATALOGUE).status == "no_match"
    assert match_listing(CompetitorItem(None), CATALOGUE).status == "no_match"
    assert match_listing(CompetitorItem("Kestrel Insulated Bottle 750ml"), []).status == "no_match"


# ---- ties --------------------------------------------------------------------------------------------------------------

def test_two_catalogue_rows_that_fit_equally_well_are_a_review_never_a_pick():
    a = CatalogueItem("c-a", "TS-A", "Ridge Trail Shoe Blue", None, "Ridge")
    b = CatalogueItem("c-b", "TS-B", "Ridge Trail Shoe Blue", None, "Ridge")
    got = match_listing(CompetitorItem("Ridge Trail Shoe Blue"), [a, b])
    assert got.status == "needs_review" and got.catalogue_id is None and "tie" in got.reason
    assert {cid for cid, _ in got.candidates} == {"c-a", "c-b"}


def test_a_clear_winner_is_not_a_tie():
    a = CatalogueItem("c-a", "TS-A", "Ridge Trail Shoe Blue", None, "Ridge")
    b = CatalogueItem("c-b", "TS-B", "Ridge Trail Shoe Red", None, "Ridge")
    assert match_listing(CompetitorItem("Ridge Trail Shoe Blue"), [a, b]).catalogue_id == "c-a"


def test_title_score_is_symmetric_without_a_brand():
    assert title_score("a b c d", "a b c e")[0] == title_score("a b c e", "a b c d")[0]
