"""The report narrative: a number that is not a fact is refused, from both writers.

The template stand-in and the model writer share one rule with the catalogue copywriter:
every number in the text must be among the computed figures. These tests cover the rule
(including the case the old substring check lets through), the template across every
shape of month, and the model path with a mocked OpenAI-compatible transport, so no
network is touched.
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

FACTS = {"net sales": "USD 210.00", "orders": "412", "previous period": "September 2026"}


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


# ------------------------------------------------------------ the rule

def test_a_number_that_is_a_fact_passes_and_one_that_is_not_is_refused():
    assert ungrounded_numbers("Net sales were USD 210.00 on 412 orders.", FACTS) == []
    assert ungrounded_numbers("Net sales were USD 215.00 on 412 orders.", FACTS) == ["215.00"]


def test_a_number_hiding_inside_a_longer_fact_is_still_refused():
    """The old check is a substring test: 41 sits inside 412, so it passes. The report's
    check compares whole numbers."""
    text = "There were 41 orders."
    assert verify_grounded(text, FACTS) == []
    assert ungrounded_numbers(text, FACTS) == ["41"]


def test_separators_and_spacing_do_not_cause_a_false_refusal():
    facts = {"net sales": "USD 1,210.00"}
    assert ungrounded_numbers("Net sales were 1210.00.", facts) == []
    assert ungrounded_numbers("Net sales were USD 1,210.00.", facts) == []


def test_a_number_written_as_a_word_is_refused():
    assert ungrounded_numbers("About five orders a day.", FACTS) == ["five"]


def test_a_date_in_the_text_must_be_in_the_facts():
    facts = report_facts(make_report())
    assert ungrounded_numbers("The best day was 2026-10-01.", facts) == []
    assert ungrounded_numbers("The best day was 2026-10-17.", facts) == ["17"]


# ------------------------------------------------------------ the template stand-in

def scenarios():
    yield "full history with a gap", make_report()
    yield "first month, nothing before it", make_report(october_rows(), OCTOBER_COVERED)
    yield "zero baseline", make_report(october_rows(), COVERED)
    yield "no gap", make_report(both_months(), SEPTEMBER_COVERED + [(date(2026, 10, 1), date(2026, 11, 1))])
    yield "falling month", make_report(september_rows() + day_rows(date(2026, 10, 1), 3, 2000, 100, 2), COVERED)
    yield "flat month", make_report(september_rows() + day_rows(date(2026, 10, 1), 15, 15000, 1000, 8), COVERED)
    yield "no orders at all", make_report(september_rows(), COVERED)


@pytest.mark.parametrize("name, report", list(scenarios()), ids=[n for n, _ in scenarios()])
def test_the_template_only_says_what_the_facts_say(name, report):
    insights = template_insights(report)
    facts = report_facts(report)
    assert insights.source == "template" and "No model was used" in insights.label
    assert insights.lines
    for line in insights.lines:
        assert ungrounded_numbers(line, facts) == [], (name, line)


def test_the_template_states_direction_from_the_sign_of_the_change():
    assert "Net sales were USD 210.00 in October 2026, up 50.0% on September 2026 (USD 140.00)." in template_lines(make_report())
    falling = template_lines(make_report(september_rows() + day_rows(date(2026, 10, 1), 3, 2000, 100, 2), COVERED))
    assert any(line.startswith("Net sales were USD 19.00") and "down 86.4%" in line for line in falling)


def test_the_template_names_the_missing_days_and_the_missing_previous_month():
    lines = template_lines(make_report())
    assert "2 days have no data (2026-10-10, 2026-10-11), so the totals are understated." in lines
    first = template_lines(make_report(october_rows(), OCTOBER_COVERED))
    assert "No earlier month is stored, so no month over month change is shown." in first


# ------------------------------------------------------------ the model writer

GOOD = (
    "Net sales were USD 210.00 in October 2026, up 50.0% on September 2026.\n"
    "Orders were 20 against 15 in September 2026.\n"
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
    assert "Net sales: USD 210.00" in user
    assert "observed_on" not in user and "value_numeric" not in user


def test_a_reply_with_an_invented_number_is_dropped_whole_and_the_template_is_used():
    bad = GOOD + "\nSales will reach 99 percent growth."
    insights = run(write_insights(make_report(), writer_replying(bad)))
    assert insights.source == "template_fallback"
    assert "99" in insights.rejected_reason
    assert insights.lines == tuple(template_lines(make_report()))
    assert "99" not in " ".join(insights.lines)


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
