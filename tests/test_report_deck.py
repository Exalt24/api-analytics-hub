"""The deck, saved and read back through python-pptx.

Each assertion is about what a person sees on a slide: how many slides, what the titles
say, that the table cells hold the hand-computed figures from report_fixtures.py, that a
missing day is a gap in the chart, and that the data health slide names the days. The
deck is saved to disk and reopened, so a pass means the file itself says it.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

from pptx import Presentation

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.report.deck import dump_deck, save_deck
from app.report.insights import report_facts, template_insights, ungrounded_numbers
from app.report.monthly import build_report
from report_fixtures import AS_OF, COVERED, OCTOBER_COVERED, both_months, october_rows


def saved(tmp_path, rows=None, covered=COVERED, note=""):
    report = build_report(both_months() if rows is None else rows, 2026, 10, label="Demo store",
                          covered=covered, as_of=AS_OF)
    insights = template_insights(report)
    path = save_deck(report, insights, tmp_path / "deck.pptx", data_note=note)
    return Presentation(str(path)), report, insights


def table_rows(slide):
    table = next(s for s in slide.shapes if s.has_table).table
    return [[cell.text for cell in row.cells] for row in table.rows]


def charts(slide):
    return [s.chart for s in slide.shapes if s.has_chart]


def test_the_deck_has_seven_slides_with_the_planned_titles(tmp_path):
    prs, _, _ = saved(tmp_path)
    assert [s.shapes.title.text_frame.text for s in prs.slides] == [
        "Monthly performance report",
        "Net sales rose 50.0% on September 2026 to USD 210.00",
        "Net sales by day, October 2026",
        "Orders by day, October 2026",
        "Average order value rose 20.0% on September 2026 to USD 12.00",
        "What the numbers say",
        "Data health: 2 of 31 days have data in October 2026",
    ]


def test_the_kpi_table_holds_the_hand_computed_figures(tmp_path):
    prs, _, _ = saved(tmp_path)
    assert table_rows(prs.slides[1]) == [
        ["Metric", "October 2026", "September 2026", "Change", "Change %", "Note"],
        ["Orders", "20", "15", "+5", "+33.3%", "partial data"],
        ["Gross sales", "USD 240.00", "USD 150.00", "+USD 90.00", "+60.0%", "partial data"],
        ["Refunds", "USD 30.00", "USD 10.00", "+USD 20.00", "+200.0%", "partial data"],
        ["Net sales", "USD 210.00", "USD 140.00", "+USD 70.00", "+50.0%", "partial data"],
        ["Average order value", "USD 12.00", "USD 10.00", "+USD 2.00", "+20.0%", "partial data"],
        ["Buyers per active day", "8.0", "6.5", "+1.5", "+23.1%", "partial data"],
    ]


def test_the_title_slide_names_the_store_the_month_and_the_data_note(tmp_path):
    prs, _, _ = saved(tmp_path, note="Invented data")
    text = dump_deck(prs).split("--- slide 2")[0]
    assert "Demo store, October 2026" in text and "Invented data" in text


def test_the_sales_chart_has_a_gap_for_each_missing_day_and_zero_for_quiet_days(tmp_path):
    prs, _, _ = saved(tmp_path)
    (chart,) = charts(prs.slides[2])
    october, september = chart.plots[0].series
    assert october.name == "October 2026" and september.name == "September 2026"
    values = list(october.values)
    assert len(values) == 31
    assert values[0] == 150.0 and values[1] == 60.0     # (18000 - 3000) / 100 and 6000 / 100
    assert values[2] == 0.0                              # a quiet day
    assert values[9] is None and values[10] is None      # October 10 and 11 were never synced
    assert list(september.values)[:2] == [90.0, 50.0]
    assert list(september.values)[30] is None            # September has 30 days


def test_the_orders_chart_carries_the_daily_orders(tmp_path):
    prs, _, _ = saved(tmp_path)
    (chart,) = charts(prs.slides[3])
    values = list(chart.plots[0].series[0].values)
    assert values[:2] == [12.0, 8.0] and values[9] is None and len(values) == 31


def test_the_order_value_slide_lists_the_best_days(tmp_path):
    prs, _, _ = saved(tmp_path)
    slide = prs.slides[4]
    tables = [[[c.text for c in row.cells] for row in s.table.rows] for s in slide.shapes if s.has_table]
    assert tables[1] == [["Rank", "Day", "Net sales"], ["1", "2026-10-01", "USD 150.00"], ["2", "2026-10-02", "USD 60.00"]]


def test_the_insights_slide_carries_the_checked_lines_and_who_wrote_them(tmp_path):
    prs, _, insights = saved(tmp_path)
    paragraphs = [p.text for s in prs.slides[5].shapes if s.has_text_frame for p in s.text_frame.paragraphs if p.text]
    for line in insights.lines:
        assert line in paragraphs
    assert insights.label in paragraphs


def test_every_number_in_the_insight_text_on_the_slide_is_a_computed_fact(tmp_path):
    prs, report, insights = saved(tmp_path)
    facts = report_facts(report)
    for line in insights.lines:
        assert ungrounded_numbers(line, facts) == []


def test_the_data_health_slide_states_the_counts_and_names_the_missing_days(tmp_path):
    prs, _, _ = saved(tmp_path)
    slide = prs.slides[6]
    assert table_rows(slide) == [
        ["", "October 2026", "September 2026"],
        ["Days expected", "31", "30"],
        ["Days with snapshots", "2", "2"],
        ["Days with no orders (a sync ran)", "27", "28"],
        ["Days missing (no snapshot, no sync)", "2", "0"],
        ["Snapshot rows used", "12", "12"],
        ["First and last day with data", "2026-10-01 to 2026-10-02", "2026-09-01 to 2026-09-02"],
    ]
    text = dump_deck(prs).split("--- slide 7")[1]
    assert "Missing days in October 2026: 2026-10-10, 2026-10-11." in text
    assert "understated" in text


def test_the_first_month_still_renders_with_no_previous_column_figures(tmp_path):
    prs, report, _ = saved(tmp_path, rows=october_rows(), covered=OCTOBER_COVERED)
    assert len(prs.slides) == 7
    rows = table_rows(prs.slides[1])
    assert rows[0][2] == "Previous month" and rows[1][2:5] == ["n/a", "n/a", "n/a"]
    assert prs.slides[1].shapes.title.text_frame.text == "Net sales came to USD 210.00 in October 2026"
    (chart,) = charts(prs.slides[2])
    assert len(chart.plots[0].series) == 1
    assert "No earlier month is stored" in dump_deck(prs)


def test_no_slide_keeps_an_empty_placeholder_or_the_library_authors_name(tmp_path):
    prs, _, _ = saved(tmp_path)
    for slide in prs.slides:
        for ph in slide.placeholders:
            assert ph.text_frame.text.strip(), "empty placeholder on a slide"
    assert prs.core_properties.author == "api-analytics-hub"
    assert "Canny" not in prs.core_properties.author + prs.core_properties.last_modified_by


def test_every_chart_has_alt_text(tmp_path):
    prs, _, _ = saved(tmp_path)
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_chart:
                assert len(shape._element.nvGraphicFramePr.cNvPr.get("descr", "")) > 20


def test_the_text_read_back_has_one_block_per_slide_and_no_float_noise(tmp_path):
    prs, _, _ = saved(tmp_path)
    text = dump_deck(prs)
    assert len(re.findall(r"^--- slide \d+: ", text, re.M)) == 7
    assert "USD 210.00" in text


def test_the_deck_is_stamped_with_this_build_not_the_librarys_template_date(tmp_path):
    from datetime import datetime, timedelta, timezone

    prs, _, _ = saved(tmp_path)
    assert abs(datetime.now(timezone.utc).replace(tzinfo=None) - prs.core_properties.created) < timedelta(days=1)
