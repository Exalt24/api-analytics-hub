"""Two classes found in output round 1 of the search slide and the narrative, each closed with a generated table.

CLASS-TABLE 1, keys cut for a table cell: `_cut` kept the first 27 characters, so five page addresses of one site and three long
queries that start alike each printed as one text. The table builds keys of every length from just under the width limit to well over
it, in the shapes a search report has (page addresses of one site, long queries), changes each of the last 12 characters in turn, and
fails if two keys that differ in their last 12 characters ever print the same.

CLASS-TABLE 2, a direction word on a figure where a lower number is better: "up 50.0%" about an average position that went from 6.0 to
9.0 reads as a gain. The table runs every figure the report prints through a rise, a fall, no change and a flat change, in the
template line, the headline and the phrase every template uses, and the lower-is-better ones must say worse or better in words that
are right for them while every other figure keeps up and down.
"""
from __future__ import annotations

import itertools
import re
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.report.insights import template_insights  # noqa: E402
from app.report.monthly import LOWER_IS_BETTER, build_report, change_verb, claim_title, move_phrase  # noqa: E402
from app.report.paid import PAID_ORDER, attach_paid, paid_claim_title  # noqa: E402
from app.report.search import KEY_TAIL, KEY_WIDTH, SEARCH_ORDER, _cut, attach_search, search_claim_title  # noqa: E402
from app.report.web import attach_web, web_claim_title  # noqa: E402
from report_fixtures import AS_OF, COVERED, both_months, day_rows  # noqa: E402
from sources_fixtures import paid_day, search_day  # noqa: E402
from test_report_web import web_day  # noqa: E402

# ---------------------------------------------------------------- CLASS-TABLE 1: different keys print differently

LAST = 12      # the keys the table separates differ in at least one of their last 12 characters
SITE = "https://www.acmeroofing.com/"
ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-"


def _key(shape: str, length: int) -> str:
    """A key of exactly `length` characters in the shape of a page address of one site or a long query (no blank at its end)."""
    head, pattern = (SITE, "replacement-") if shape == "page" else ("how much does a roof ", "replacement cost ")
    text = (head + pattern * 8)[:length]
    return text[:-1] + "x" if text.endswith(" ") else text


def _vary(key: str, position: int) -> str:
    """The same key with one character changed at `position` (a character that is not already there, never a space)."""
    other = next(c for c in ALPHABET if c != key[position] and c != " ")
    return key[:position] + other + key[position + 1:]


KEY_SHAPES = ("page", "query")
KEY_LENGTHS = range(KEY_WIDTH - 8, KEY_WIDTH + 35)


# CLASS-TABLE: shape x key length from 8 under the width to 34 over it x each of the last 12 character positions
@pytest.mark.parametrize("shape, length", list(itertools.product(KEY_SHAPES, KEY_LENGTHS)))
def test_two_keys_that_differ_in_their_last_12_characters_never_print_the_same(shape, length):
    base = _key(shape, length)
    assert len(base) == length
    for offset in range(1, LAST + 1):
        position = length - offset
        other = _vary(base, position)
        assert other != base
        assert _cut(base) != _cut(other), "%r and %r both print as %r" % (base, other, _cut(base))
    # and a key that is a longer or shorter version of the same address
    assert _cut(base) != _cut(base + "x") and _cut(base) != _cut(base[:-1])


@pytest.mark.parametrize("shape, length", list(itertools.product(KEY_SHAPES, KEY_LENGTHS)))
def test_a_cut_key_fits_the_cell_and_a_short_key_is_untouched(shape, length):
    key = _key(shape, length)
    shown = _cut(key)
    assert len(shown) <= KEY_WIDTH
    if length <= KEY_WIDTH:
        assert shown == key
    else:
        assert KEY_WIDTH - 3 <= len(shown) <= KEY_WIDTH and "..." in shown and shown.endswith(key[-KEY_TAIL:]) and shown.startswith(key[:3])


def test_the_five_page_addresses_and_the_three_long_queries_of_the_finding_print_as_eight_different_texts():
    pages = [SITE + p for p in ("services/roof-repair/", "services/roof-replacement/", "blog/how-much-does-a-new-roof-cost/", "contact/",
                                "services/gutters/")]
    queries = ["how much does a roof replacement cost", "how much does a roof replacement cost in austin", "how much does a roof replacement take"]
    assert len({_cut(k) for k in pages}) == 5 and len({_cut(k) for k in queries}) == 3


def test_the_cut_of_the_finding_keys_is_exactly_this_so_the_split_of_30_characters_is_pinned():
    """The start (12), three dots and the end (15): a characterisation of the choice, so a different split is a decision, not a drift."""
    assert [_cut(SITE + p) for p in ("services/roof-repair/", "services/roof-replacement/", "blog/how-much-does-a-new-roof-cost/", "contact/",
                                     "services/gutters/")] == ["https://www....es/roof-repair/", "https://www....of-replacement/",
                                                               "https://www....-new-roof-cost/", "https://www....ng.com/contact/",
                                                               "https://www....rvices/gutters/"]
    assert [_cut(q) for q in ("how much does a roof replacement cost", "how much does a roof replacement cost in austin")] == [
        "how much doe...eplacement cost", "how much doe... cost in austin"]
    assert (KEY_WIDTH, KEY_TAIL) == (30, 15) and all(len(_cut(SITE + "x" * n)) == KEY_WIDTH for n in range(10, 40))


def test_a_key_with_runs_of_blanks_or_line_breaks_is_one_line_and_text_is_never_dropped_from_the_end():
    assert _cut("line\nbreak   and  spaces") == "line break and spaces"
    long_query = "a very long query that people really did type into a search box one day"
    assert _cut(long_query).endswith("box one day"[-KEY_TAIL:]) and "\n" not in _cut(long_query)


# ---------------------------------------------------------------- CLASS-TABLE 2: the words for a figure where a lower number is better

SEPT_DAYS = [date(2026, 9, 1) + timedelta(days=i) for i in range(30)]
OCT_DAYS = [date(2026, 10, 1) + timedelta(days=i) for i in range(31)]
FULL = [(date(2026, 9, 1), date(2026, 11, 1))]

MOVES = ("rise", "fall", "equal", "flat", "small_rise", "small_fall")

#: the two values (September, October) for each move of a figure. A small move is half a percent: shown as +0.5%, which is not flat.
#: A ratio is the same every day (position, cost per click). A total is over the days, and September has 30 days and October 31, so
#: 31 a day against 30 a day is the same total, and the small moves are 0.5% of that.
POSITION = {"rise": (6000, 9000), "fall": (9000, 6000), "equal": (6000, 6000), "flat": (6000, 6001), "small_rise": (60000, 60300), "small_fall": (60000, 59700)}
#: cost per click is spend over clicks: the same spend and fewer clicks is a rise
CLICKS = {"rise": (50, 25), "fall": (25, 50), "equal": (50, 50), "flat": (50000, 50001), "small_rise": (200, 199), "small_fall": (199, 200)}
TOTAL = {"rise": (10, 20), "fall": (20, 10), "equal": (31, 30), "flat": (31000, 30001), "small_rise": (31000, 30150),
         "small_fall": (31000, 29850)}


def _store_only(sept: int, octo: int):
    rows = [r for d in SEPT_DAYS for r in day_rows(d, 10, sept, 0, 8)] + [r for d in OCT_DAYS for r in day_rows(d, 10, octo, 0, 8)]
    return build_report(rows, 2026, 10, label="Store", covered=FULL, as_of=AS_OF)


def _web_report(sept: int, octo: int):
    rows = [r for d in SEPT_DAYS for r in web_day(d, sept, 100, 300, 5)] + [r for d in OCT_DAYS for r in web_day(d, octo, 100, 300, 5)]
    report = build_report(both_months(), 2026, 10, label="Store", covered=COVERED, as_of=AS_OF)
    return attach_web(report, rows, label="Site", covered=FULL, as_of=AS_OF)


def _search_report(sept: int, octo: int):
    rows = [r for d in SEPT_DAYS for r in search_day(d, 10, 100, sept)] + [r for d in OCT_DAYS for r in search_day(d, 10, 100, octo)]
    report = build_report(both_months(), 2026, 10, label="Store", covered=COVERED, as_of=AS_OF)
    return attach_search(report, rows, [], label="Search", covered=FULL, as_of=AS_OF)


def _paid_report(sept: int, octo: int):
    rows = [r for d in SEPT_DAYS for r in paid_day(d, 1000000, 1000, sept)] + [r for d in OCT_DAYS for r in paid_day(d, 1000000, 1000, octo)]
    report = build_report(both_months(), 2026, 10, label="Store", covered=COVERED, as_of=AS_OF)
    return attach_paid(report, rows, label="Ads", covered=FULL, as_of=AS_OF)


def _search_clicks_report(sept: int, octo: int):
    rows = [r for d in SEPT_DAYS for r in search_day(d, sept, 1000, 7000)] + [r for d in OCT_DAYS for r in search_day(d, octo, 1000, 7000)]
    report = build_report(both_months(), 2026, 10, label="Store", covered=COVERED, as_of=AS_OF)
    return attach_search(report, rows, [], label="Search", covered=FULL, as_of=AS_OF)


def _store_title(report, key):
    return claim_title(report, key)


FIGURES = {
    # figure -> (table of values, report builder, the source of the row, key, claim title function): the two lower-is-better figures, then a
    # control for every source's headline wording (the store, the website, ads and search) that must keep rose, fell, up and down
    "position": (POSITION, _search_report, lambda r: r.search, "position", search_claim_title),
    "cpc": (CLICKS, _paid_report, lambda r: r.paid, "cpc", paid_claim_title),
    "search_clicks": (TOTAL, _search_clicks_report, lambda r: r.search, "search_clicks", search_claim_title),
    "net_sales": (TOTAL, _store_only, lambda r: r, "net_sales", _store_title),
    "sessions": (TOTAL, _web_report, lambda r: r.web, "sessions", web_claim_title),
}
WORDS_WITH_NO_RIGHT_TO_A_RANKING = re.compile(r"\b(up|down|rose|fell|rise|fall)\b", re.I)


# CLASS-TABLE: every figure the report prints with a lower-is-better meaning, and a control for each source's headline, x six moves
@pytest.mark.parametrize("figure, move", list(itertools.product(FIGURES, MOVES)))
def test_a_move_of_a_figure_is_worded_the_way_that_figure_reads(figure, move):
    values, build, source, key, title = FIGURES[figure]
    report = build(*values[move])
    src = source(report)
    row = src.kpi(key)
    phrase = move_phrase(row)
    headline = title(src, key)
    if move == "equal":
        assert phrase == "unchanged" and headline == "%s did not change from September" % row.label
        return
    if move == "flat":
        assert phrase.startswith("essentially flat") and headline.startswith("%s stayed essentially flat" % row.label)
        return
    rise = move in ("rise", "small_rise")
    amount = row.pct_display.lstrip("+-")
    assert row.pct_display == ("+" if rise else "-") + amount
    if figure in LOWER_IS_BETTER:
        worse, better = LOWER_IS_BETTER[figure]
        assert phrase == "%s by %s (%s)" % ("worse" if rise else "better", amount, worse if rise else better)
        assert change_verb(row) == ("worsened" if rise else "improved")
        assert headline == "%s %s %s from September" % (row.label, "worsened" if rise else "improved", amount)
        assert not WORDS_WITH_NO_RIGHT_TO_A_RANKING.search(phrase) and not WORDS_WITH_NO_RIGHT_TO_A_RANKING.search(headline)
    else:
        assert phrase == "%s %s" % ("up" if rise else "down", amount)
        assert change_verb(row) == ("rose" if rise else "fell")
        assert headline == "%s %s %s from September" % (row.label, "rose" if rise else "fell", amount)



@pytest.mark.parametrize("move", ("rise", "fall", "equal", "flat"))
def test_the_narrative_line_for_average_position_names_the_direction_in_words_that_are_right(move):
    report = _search_report(*POSITION[move])
    lines = [ln for ln in template_insights(report).lines if ln.startswith("Average position")]
    assert len(lines) == 1
    line = lines[0]
    if move == "rise":
        assert line.startswith("Average position was 9.0 in October, worse by 50.0% (further from the top) from September (6.0)")
    elif move == "fall":
        assert line.startswith("Average position was 6.0 in October, better by 33.3% (nearer the top) from September (9.0)")
    elif move == "equal":
        assert line.startswith("Average position was 6.0 in October, unchanged from September (6.0)")
    else:
        assert "essentially flat" in line
    assert not re.search(r"\b(up|down)\b", line.split(" from ")[0].split("in October")[1]), line


def test_no_lower_is_better_figure_gets_a_per_reported_day_comparison_so_the_up_down_of_that_phrase_cannot_reach_one():
    """per_day_phrase only ever words a total (sums of days); a position or a cost per click is a ratio and has no per day line."""
    report = _search_report(*POSITION["rise"])
    paid = _paid_report(*CLICKS["rise"])
    assert report.search.kpi("position").per_day is None and paid.paid.kpi("cpc").per_day is None


def test_every_figure_the_report_prints_is_classified_as_lower_is_better_or_not():
    """A new metric must be placed in one of the two lists below on purpose, or this fails and names it."""
    higher_is_better_or_neutral = {"net_sales", "orders", "avg_order_value", "buyers_per_day", "sessions", "users", "page_views", "key_events",
                                   "order_rate", "ad_spend", "ad_impressions", "ad_clicks", "ctr", "ad_purchases", "ad_purchase_value", "roas",
                                   "search_clicks", "search_impressions", "gross_sales", "users_per_day"}
    # a refund amount is a lower-is-better figure whose up and down is plain (nobody reads "refunds up" as good news), so it keeps
    # the plain words: KNOWN_LIMIT, vocabulary only
    plain_amounts_where_lower_is_better = {"refunds"}
    from app.report.web import WEB_ORDER, attach_web
    from test_report_web import WEB_COVERED, WEB_ROWS
    full = attach_web(_paid_report(*CLICKS["rise"]), WEB_ROWS, label="Site", covered=WEB_COVERED, as_of=AS_OF)
    keys = {k.key for k in full.kpis} | {k.key for k in full.web.kpis} | {k.key for k in full.paid.kpis} | set(PAID_ORDER) | set(SEARCH_ORDER) | set(WEB_ORDER)
    unplaced = keys - higher_is_better_or_neutral - plain_amounts_where_lower_is_better - set(LOWER_IS_BETTER)
    assert not unplaced, "classify %s as lower-is-better or not" % sorted(unplaced)
    assert set(LOWER_IS_BETTER) == {"position", "cpc"} and set(LOWER_IS_BETTER) <= keys


def test_a_zero_base_rise_of_a_lower_is_better_figure_is_worded_too():
    """pct is None when the earlier value is zero: the amount is the change, and the words are still worse or better."""
    from app.report.monthly import KpiRow
    base = dict(label="Cost per click", current=Decimal(2), previous=Decimal(0), current_display="2", previous_display="0", pct=None,
                pct_display="n/a", exact_equal=False)
    for amount in (1, 2):
        shown = "+%d" % amount
        assert move_phrase(KpiRow(key="cpc", change=Decimal(amount), change_display=shown, **base)) == "worse by %d (each click costs more)" % amount
        assert move_phrase(KpiRow(key="roas", change=Decimal(amount), change_display=shown, **base)) == "up %d" % amount
        assert move_phrase(KpiRow(key="cpc", change=Decimal(-amount), change_display="-%d" % amount, **base)) == "better by %d (each click costs less)" % amount
        assert move_phrase(KpiRow(key="roas", change=Decimal(-amount), change_display="-%d" % amount, **base)) == "down %d" % amount


def test_the_prompt_that_writes_the_narrative_tells_the_model_the_same_rule_for_the_figures_where_a_lower_number_is_better():
    from app.report.insights import REPORT_PROMPT
    flat = " ".join(REPORT_PROMPT.split())
    assert "average position and cost per click, where a lower number is better" in flat and "never write up or down for those two" in flat
