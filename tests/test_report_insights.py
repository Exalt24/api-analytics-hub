"""The report narrative: a figure that is not backed is refused, from both writers.

The template stand-in and the model writer share one rule with the catalogue copywriter,
tightened for the report: every figure in the text must be one of the report's own displayed
figures (no date fragments, no bare day numbers), a figure beside a named metric must belong
to it, separators and glued letters cannot hide a figure, and number words are whole words.
These tests cover each of those, the template across every shape of month, and the model
path through a mocked OpenAI-compatible transport, so no network is touched.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.ai.catalog_copy import verify_grounded
from app.report.insights import (
    MAX_LINES,
    REPORT_PROMPT,
    ReportInsightWriter,
    report_facts,
    template_insights,
    template_lines,
    ungrounded_numbers,
    write_insights,
)
from app.report.monthly import build_report
from report_fixtures import (
    AS_OF,
    COVERED,
    OCTOBER_COVERED,
    SEPTEMBER_COVERED,
    both_months,
    day_rows,
    october_rows,
    september_rows,
)

FACTS = {"net sales": "$210.00", "orders": "412", "previous period": "September"}


def run(coro):
    return asyncio.run(coro)


def make_report(rows=None, covered=COVERED):
    return build_report(both_months() if rows is None else rows, 2026, 10, label="Demo", covered=covered, as_of=AS_OF)


def writer_replying(content=None, *, status=200, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, text="upstream said no")
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return ReportInsightWriter("test-key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# ------------------------------------------------------------ exact figures

def test_a_figure_that_is_a_fact_passes_and_one_that_is_not_is_refused():
    assert ungrounded_numbers("Net sales were $210.00 on 412 orders.", FACTS) == []
    assert ungrounded_numbers("Net sales were $215.00 on 412 orders.", FACTS) == ["$215.00"]


def test_a_number_hiding_inside_a_longer_fact_is_still_refused():
    """The catalogue check is a substring test: 41 sits inside 412, so it passes. The report
    compares whole figures."""
    text = "There were 41 orders."
    assert verify_grounded(text, FACTS) == []
    assert ungrounded_numbers(text, FACTS) == ["41"]


def test_separators_and_spacing_do_not_cause_a_false_refusal():
    facts = {"net sales": "$1,210.00"}
    assert ungrounded_numbers("Net sales were $1,210.00.", facts) == []
    assert ungrounded_numbers("Net sales were 1210.00.", facts) == ["1210.00"]  # a plain number is not an amount


def test_a_currency_code_and_a_dollar_sign_are_the_same_amount():
    assert ungrounded_numbers("Net sales were USD 210.00.", FACTS) == []


def test_a_date_must_be_one_of_the_facts_dates_as_a_whole():
    facts = report_facts(make_report())
    assert ungrounded_numbers("The best day was Oct 1.", facts) == []
    assert ungrounded_numbers("The best day was Oct 17.", facts) == ["Oct 17"]


def test_a_year_or_a_day_number_cannot_be_borrowed_from_a_date():
    facts = report_facts(make_report())
    assert ungrounded_numbers("Net sales in 2026 were strong.", facts) == ["2026"]
    assert ungrounded_numbers("On the 10 the shop closed.", facts) == ["10"]


def test_a_bare_count_in_a_sentence_about_a_metric_must_be_a_day_count():
    facts = report_facts(make_report())
    assert ungrounded_numbers("Orders were 20 in October, and October is missing 2 days.", facts) == []
    assert ungrounded_numbers("Orders were 31 in October.", facts) == ["31"]
    assert ungrounded_numbers("2 days have no data.", facts) == []


# ------------------------------------------------------------ a figure belongs to its metric

def test_a_figure_beside_the_wrong_metric_is_refused():
    facts = report_facts(make_report())
    assert ungrounded_numbers("Net sales rose 50.0%.", facts) == []
    assert ungrounded_numbers("Net sales rose 200.0%.", facts) == ["200.0%"]   # that is the refunds figure
    assert ungrounded_numbers("Refunds rose 200.0%.", facts) == []
    assert ungrounded_numbers("Orders rose 10%.", facts) == ["10%"]


def test_a_metric_alias_maps_to_its_metric():
    facts = report_facts(make_report())
    assert ungrounded_numbers("Order value rose 20.0%.", facts) == []
    assert ungrounded_numbers("Order value rose 50.0%.", facts) == ["50.0%"]


def test_the_check_is_per_sentence_not_per_clause_and_says_so():
    """Documented limit: a sentence that names two metrics may use either one's figures."""
    facts = report_facts(make_report())
    assert ungrounded_numbers("Net sales rose 33.3% and orders rose 50.0%.", facts) == []
    assert ungrounded_numbers("Sales rose 200.0%.", facts) == []   # no metric named, so existence only


# ------------------------------------------------------------ separators, glue, words

def test_a_comma_that_is_not_a_thousands_separator_is_refused():
    facts = report_facts(make_report())
    assert ungrounded_numbers("Orders rose 1,4% on September.", facts)
    assert ungrounded_numbers("Orders were 6,07.", {"orders": "607"})
    assert ungrounded_numbers("Net sales were $1,2000.", {"net sales": "$12,000.00"})


def test_two_real_figures_joined_by_a_comma_are_refused_not_read_as_a_list():
    """20 and 15 are both facts and 2015 is a substring of a third, so neither the catalogue
    check nor a tokenizer alone would refuse "20,15", which reads 2015."""
    facts = {"orders": "20", "previous orders": "15", "founded": "2015"}
    assert ungrounded_numbers("Orders were 20 against 15.", facts) == []
    assert verify_grounded("Orders were 20,15.", facts) == []
    assert ungrounded_numbers("Orders were 20,15.", facts)
    assert ungrounded_numbers("Orders were 20, 15.", facts) == []   # a comma and a space is punctuation


def test_a_figure_glued_to_a_letter_is_still_checked():
    assert ungrounded_numbers("Sales were USD9.", FACTS) == ["USD9"]
    assert ungrounded_numbers("Q4 grew 18%.", {"x": "18%"}) == ["4"]
    assert ungrounded_numbers("About x5 the usual.", FACTS) == ["5"]


@pytest.mark.parametrize("sentence, word", [
    ("Fifteen days passed.", "fifteen"), ("Nineteen orders came in.", "nineteen"), ("A dozen orders.", "dozen"),
    ("Hundreds of orders.", "hundreds"), ("Orders doubled.", "doubled"), ("Orders halved.", "halved"),
    ("Twice as many orders.", "twice"), ("One day is missing.", "one"),
])
def test_number_words_are_refused(sentence, word):
    assert word in ungrounded_numbers(sentence, {"previous period": "September"})


def test_a_number_word_is_a_whole_word_never_a_substring():
    facts = {"previous period": "none, no earlier month is stored"}
    assert "one" in ungrounded_numbers("One day is missing.", facts)       # "one" is not found inside "none"
    assert ungrounded_numbers("The month is done and nobody phoned.", facts) == []


# ------------------------------------------------------------ the template stand-in

def scenarios():
    yield "full history with a gap", make_report()
    yield "first month, nothing before it", make_report(october_rows(), OCTOBER_COVERED)
    yield "zero baseline", make_report(october_rows(), COVERED)
    yield "no gap", make_report(both_months(), SEPTEMBER_COVERED + [(date(2026, 10, 1), date(2026, 11, 1))])
    yield "falling month", make_report(september_rows() + day_rows(date(2026, 10, 1), 3, 2000, 100, 2), COVERED)
    yield "flat month", make_report(september_rows() + day_rows(date(2026, 10, 1), 15, 15004, 1000, 8), COVERED)
    yield "equal month", make_report(september_rows() + day_rows(date(2026, 10, 1), 15, 15000, 1000, 8), COVERED)
    yield "no orders at all", make_report(september_rows(), COVERED)
    yield "month in progress", build_report(both_months(), 2026, 10, covered=COVERED, as_of=date(2026, 10, 10))
    yield "one day missing", make_report(october_rows() + day_rows(date(2026, 10, 3), 1, 100, 0),
                                         [(date(2026, 10, 1), date(2026, 10, 10)), (date(2026, 10, 11), date(2026, 11, 1))] + SEPTEMBER_COVERED)


@pytest.mark.parametrize("name, report", list(scenarios()), ids=[n for n, _ in scenarios()])
def test_the_template_only_says_what_the_facts_say(name, report):
    insights = template_insights(report)
    facts = report_facts(report)
    assert insights.source == "template" and "No model was used" in insights.label
    assert insights.lines
    for line in insights.lines:
        assert ungrounded_numbers(line, facts) == [], (name, line)


def test_the_template_states_direction_and_the_caveat_in_each_change_sentence():
    lines = template_lines(make_report())
    assert lines[0] == "Net sales were $210.00 in October, up 50.0% from September ($140.00), and October is missing 2 days."
    assert lines[1] == "Orders were 20 in October, up 33.3% from September (15), and October is missing 2 days."
    falling = template_lines(make_report(september_rows() + day_rows(date(2026, 10, 1), 3, 2000, 100, 2), COVERED))
    assert any(line.startswith("Net sales were $19.00") and "down 86.4%" in line for line in falling)


def test_the_template_lists_the_largest_movement():
    assert "The biggest move was Refunds, up 200.0% from September, and October is missing 2 days." in template_lines(make_report())


def test_the_template_says_flat_with_the_amount_and_unchanged_only_for_equal_values():
    flat = template_lines(make_report(september_rows() + day_rows(date(2026, 10, 1), 15, 15004, 1000, 8), COVERED))
    assert flat[0].startswith("Net sales were $140.04 in October, essentially flat (+$0.04) from September ($140.00)")
    equal = template_lines(make_report(september_rows() + day_rows(date(2026, 10, 1), 15, 15000, 1000, 8), COVERED))
    assert equal[0].startswith("Net sales were $140.00 in October, unchanged from September ($140.00)")


def test_the_template_names_the_missing_days_in_the_singular_and_the_plural():
    two = template_lines(make_report())
    assert "2 days have no data (Oct 10, Oct 11), so the totals are understated." in two
    one_gap = [(date(2026, 10, 1), date(2026, 10, 10)), (date(2026, 10, 11), date(2026, 11, 1))] + SEPTEMBER_COVERED
    one = template_lines(make_report(october_rows() + september_rows(), one_gap))
    assert "1 day has no data (Oct 10), so the totals are understated." in one


def test_the_template_names_the_missing_previous_month_and_a_month_in_progress():
    first = template_lines(make_report(october_rows(), OCTOBER_COVERED))
    assert "No earlier month is stored, so no month over month change is shown." in first
    mid = template_lines(build_report(both_months(), 2026, 10, covered=COVERED, as_of=date(2026, 10, 10)))
    assert "Only 9 of 31 days of October are counted, so the totals are partial." in mid


# ------------------------------------------------------------ the model writer

GOOD = (
    "Net sales were $210.00 in October, up 50.0% from September.\n"
    "Orders were 20 against 15 in September.\n"
    "2 days have no data, so the totals are understated."
)


def test_a_grounded_model_reply_is_used_and_labelled_with_the_model():
    insights = run(write_insights(make_report(), writer_replying(GOOD)))
    assert insights.source == "model"
    assert insights.lines == tuple(GOOD.splitlines())
    assert insights.model in insights.label and "checked against the computed figures" in insights.label


def test_the_model_is_sent_the_report_prompt_and_the_facts_not_the_rows():
    seen = []
    run(write_insights(make_report(), writer_replying(GOOD, seen=seen)))
    body = seen[0]
    assert body["messages"][0] == {"role": "system", "content": REPORT_PROMPT}
    user = body["messages"][1]["content"]
    assert "Net sales: $210.00" in user and "dates with no data: Oct 10, Oct 11" in user
    assert "observed_on" not in user and "value_numeric" not in user and "2026" not in user


def test_a_reply_with_an_invented_figure_is_dropped_whole_and_the_template_is_used():
    bad = GOOD + "\nSales will reach 99 percent growth."
    insights = run(write_insights(make_report(), writer_replying(bad)))
    assert insights.source == "template_fallback"
    assert "99" in insights.rejected_reason
    assert insights.lines == tuple(template_lines(make_report()))
    assert "99" not in " ".join(insights.lines)


def test_a_reply_that_puts_a_real_figure_on_the_wrong_metric_is_dropped():
    insights = run(write_insights(make_report(), writer_replying(GOOD + "\nNet sales rose 200.0%.")))
    assert insights.source == "template_fallback" and "200.0%" in insights.rejected_reason


@pytest.mark.parametrize("reply", ["", "   \n  ", "Great month!\nOrders were 20.", "\n".join("Orders were 20." for _ in range(MAX_LINES + 1))])
def test_an_empty_shouting_or_overlong_reply_falls_back(reply):
    insights = run(write_insights(make_report(), writer_replying(reply)))
    assert insights.source == "template_fallback"
    assert insights.rejected_reason


def test_a_provider_error_falls_back_instead_of_failing_the_report():
    insights = run(write_insights(make_report(), writer_replying(status=500)))
    assert insights.source == "template_fallback"
    assert "provider returned 500" in insights.rejected_reason


def test_with_no_writer_the_template_is_used_and_says_no_model_key_is_set():
    insights = run(write_insights(make_report(), None))
    assert insights.source == "template"
    assert "no model key is set" in insights.label
