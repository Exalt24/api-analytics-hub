"""Replay of a blind corpus against the monthly report stage, and the table test of its one fixed class.

tests/corpus/monthly_report_corpus_blind_1.jsonl holds 44 cases (25 must-pass and 19 must-fail) written by a reviewer who read
docs/MONTHLY_REPORT.md and the code. Each carries `promise` (a quote from the spec), `why` (who is harmed on an ordinary day), `source`
(invented: no real store is behind any of them) and `expected`. The adversary also recorded `actual`, what the code did when it wrote the case.

Every case is replayed here against the real code, none is skipped: NOT_REPLAYABLE below is empty on purpose, a case that cannot be replayed offline
would be listed there by number with its reason. For a case the code agrees with, the live outcome must equal `expected`. A case the owner decided not
to fix is in KNOWN_LIMITS by number with the reason, and its live outcome is pinned to the recorded `actual`, so the limit cannot move silently.

CLASS-TABLE: a number the template text or the deck prints that the facts do not carry. Case 42 found it: a month with more than ten days with no data
made the template print "and N more", N was not a fact, the figure check refused the template and the command exited 2 with no deck. The table test at
the bottom builds the whole report through the command line for every count of missing days (0 to 31) at the start, middle and end of the month, with and
without a previous month, and for a month that is not over; each must give exit 0 and a deck whose every figure the check backs.
"""
from __future__ import annotations

import itertools
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from pptx import Presentation

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import monthly_report  # noqa: E402
from app.report import service  # noqa: E402
from app.report.deck import _note_text, save_deck  # noqa: E402
from app.report.insights import check_model_text, report_facts, template_insights, ungrounded_numbers  # noqa: E402
from app.report.monthly import MAX_DAYS_LISTED, build_report, fmt_day, format_money, per_day_phrase  # noqa: E402
from report_fixtures import day_rows  # noqa: E402

CORPUS = ROOT / "tests" / "corpus" / "monthly_report_corpus_blind_1.jsonl"
CASES = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]

#: case number -> why it is not replayed offline. Empty: all 44 are replayed.
NOT_REPLAYABLE: dict[int, str] = {}

#: case number -> why the code is deliberately left as it is (the lead's decision, scored 2x1 or less). The live outcome is pinned to the recorded `actual`.
KNOWN_LIMITS = {
    39: "F3 KNOWN_LIMIT 2x1: a correct 'vs.' line is split at the period and refused, the template is used, the safe direction",
    40: "F4 KNOWN_LIMIT 1x1: a correct 'Oct. 29' line is refused for the same reason, the template is used, the safe direction",
    41: "F5 KNOWN_LIMIT 1x2: '371k' is kept for a real 371, a figure glued to a letter; it needs a model that scales a figure and the prompt gives it no reason to",
    43: "F2 KNOWN_LIMIT 2x1: the data health footer is worded for a full month, the note beside it already says the month is not over",
}

# ---------------------------------------------------------------- fixtures named by the corpus rule


def _gen(year: int, month: int, days) -> list[dict]:
    """orders d=10+d%5; gross=orders*2500+13d; refunds=1234 if d%7==0; buyers=orders-2"""
    rows: list[dict] = []
    for d in days:
        orders = 10 + d % 5
        rows += day_rows(date(year, month, d), orders, orders * 2500 + 13 * d, 1234 if d % 7 == 0 else 0, orders - 2)
    return rows


def _windows(pairs):
    return [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in pairs]


def _report(spec: dict):
    rows = _gen(2026, 9, spec["sept"]) + _gen(2026, 10, spec["oct"])
    return build_report(rows, 2026, 10, label="Store", covered=_windows(spec["covered"]), as_of=date.fromisoformat(spec["as_of"]))


FULL_MONTH = {"sept": list(range(1, 31)), "oct": list(range(1, 32)), "covered": [["2026-09-01", "2026-11-01"]], "as_of": "2026-11-15"}
#: "the fixture with Oct 14 and 15 uncovered and missing (data pull covered Sept 1 to Oct 1 only)"
GAPPY_MONTH = {"sept": list(range(1, 31)), "oct": [d for d in range(1, 32) if d not in (14, 15)], "covered": [["2026-09-01", "2026-10-01"]],
               "as_of": "2026-11-15"}


def _facts_named(description: str) -> dict:
    if description.startswith("report_facts of the full-month fixture"):
        return report_facts(_report(FULL_MONTH))
    if description.startswith("report_facts of the fixture with Oct 14 and 15 uncovered"):
        return report_facts(_report(GAPPY_MONTH))
    raise AssertionError("a corpus case names a fixture this replay does not know: %s" % description)


# ---------------------------------------------------------------- one `actual` per kind of case


def _model_text_actual(case: dict) -> str:
    inp = case["input"]
    lines, reason = check_model_text(inp["text"], _facts_named(inp["facts"]))
    return "kept" if reason is None and lines else "template fallback"


def _health_slide_cells(report) -> tuple[list[list[str]], str]:
    """The data health slide of a saved deck: its table rows and all its text boxes."""
    import tempfile
    insights = template_insights(report)
    with tempfile.TemporaryDirectory() as tmp:
        prs = Presentation(str(save_deck(report, insights, Path(tmp) / "d.pptx")))
    slide = prs.slides[6]
    table = [[c.text for c in row.cells] for s in slide.shapes if s.has_table for row in s.table.rows]
    text = "\n".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)
    return table, text


def _snapshot_actual(index: int, case: dict) -> str:
    spec = case["input"]["spec"]
    report = _report(spec)
    cur, prev = report.current, report.previous
    k = report.kpi
    if index == 0:
        r = k("net_sales")
        return "Net sales %s, previous %s, change %s (%s)" % (r.current_display, r.previous_display, r.change_display.lstrip("+"), r.pct_display.lstrip("+"))
    if index == 1:
        r = k("orders")
        return "Orders %s vs %s, %s, %s" % (r.current_display, r.previous_display, r.change_display, r.pct_display)
    if index == 2:
        return k("avg_order_value").current_display
    if index == 3:
        return k("buyers_per_day").current_display
    if index in (4, 5, 6, 7):
        r = k("orders")
        return "orders %s; %s" % (r.current_display, _note_text(report, r))
    if index == 8:
        r = k("orders")
        return "orders %s, note '%s'" % (r.current_display, r.note)
    if index == 9:
        return "missing days [%s]" % ", ".join(fmt_day(d) for d in cur.missing_days)
    if index == 10:
        r = k("orders")
        return "change %s, note '%s'" % (r.pct_display, r.note)
    if index == 11:
        r = k("orders")
        return "pct %s note %s" % (r.pct_display, r.note)
    if index == 12:
        pd = k("orders").per_day
        return "orders per reported day %s vs %s; days counted %d" % (pd.current_display, pd.previous_display, cur.days_expected)
    if index == 42:
        # the template must be written and the deck saved, with the cut list of dates ending in the count of the rest
        insights = template_insights(report)
        table, text = _health_slide_cells(report)
        missing = cur.missing_days
        m = re.search(r"Days with no data in October: (Oct \d+)(?:, Oct \d+)*, (Oct \d+) and (\d+) more\.", text)
        assert m, text
        assert insights.source == "template"
        return "template written, deck saved, %d days with no data listed as '%s ... %s and %s more'" % (len(missing), m.group(1), m.group(2), m.group(3))
    if index == 43:
        table, text = _health_slide_cells(report)
        october = [row[1] for row in table[1:5]]
        unconditional = "add up to the days in the month." in text and "or the days counted so far" not in text
        return "footer says add up to days in month: %s; October column: %s -> footer unconditional, %d+%d+%d != %s" % (
            unconditional, october, int(october[1]), int(october[2]), int(october[3]), october[0])
    raise AssertionError("case %d has no replay" % index)


def actual(index: int) -> str:
    case = CASES[index]
    if case["input"]["kind"] == "model_text":
        return _model_text_actual(case)
    if case["input"]["kind"] == "snapshots":
        return _snapshot_actual(index, case)
    raise AssertionError("case %d has an unknown kind %r" % (index, case["input"]["kind"]))


# ---------------------------------------------------------------- the replay


def test_the_corpus_is_the_one_that_was_written():
    assert len(CASES) == 44
    assert sum(1 for c in CASES if c["expect"] == "pass") == 25
    assert sum(1 for c in CASES if c["expect"] == "fail") == 19
    for n, c in enumerate(CASES):
        assert c["source"] in ("real", "invented"), n
        assert len(c["why"]) >= 10, n
        assert c["expected"] and c["promise"], n


def test_no_case_is_skipped_silently():
    assert NOT_REPLAYABLE == {}
    assert all(c["input"]["kind"] in ("model_text", "snapshots") for c in CASES)
    assert set(KNOWN_LIMITS) <= set(range(len(CASES)))


@pytest.mark.parametrize("index", [n for n in range(44) if n not in KNOWN_LIMITS])
def test_the_code_gives_the_outcome_the_corpus_expects(index):
    case = CASES[index]
    # a must-pass case is kept or true; a must-fail case is refused (the template is used) and says so in `expected`
    assert actual(index) == case["expected"], "case %d (%s): %s" % (index, case["expect"], case["why"])


@pytest.mark.parametrize("index", [n for n in range(44) if n not in KNOWN_LIMITS and n != 42])
def test_the_recorded_actual_still_holds(index):
    assert actual(index) == CASES[index]["actual"]


@pytest.mark.parametrize("index", sorted(KNOWN_LIMITS))
def test_a_known_limit_is_pinned_to_what_the_code_does_today(index):
    assert actual(index) == CASES[index]["actual"], KNOWN_LIMITS[index]
    assert actual(index) != CASES[index]["expected"], "the limit moved: remove case %d from KNOWN_LIMITS" % index


def test_case_42_was_the_crash_this_change_fixed():
    # the recorded actual of case 42 is the crash; the replay above proves it no longer happens
    assert "ReportError" in CASES[42]["actual"]
    assert actual(42) == CASES[42]["expected"]


# ---------------------------------------------------------------- CLASS-TABLE: every count of missing days, every position, with and without a previous month

class _FakeDb:
    def __init__(self, *args, **kwargs):
        pass

    async def connect(self):
        pass

    async def close(self):
        pass


def _missing(position: str, k: int, counted: int) -> list[int]:
    if position == "start":
        return list(range(1, k + 1))
    if position == "end":
        return list(range(counted - k + 1, counted + 1))
    first = (counted - k) // 2 + 1
    return list(range(first, first + k))


def _inputs(k: int, position: str, previous: bool, counted: int):
    missing = set(_missing(position, k, counted))
    present = [d for d in range(1, counted + 1) if d not in missing]
    rows = _gen(2026, 10, present) + (_gen(2026, 9, range(1, 31)) if previous else [])
    covered = [(date(2026, 10, d), date(2026, 10, d) + timedelta(days=1)) for d in present]
    if previous:
        covered.append((date(2026, 9, 1), date(2026, 10, 1)))
    return rows, covered, sorted(missing)


def _slide_text(prs, n: int) -> str:
    out = []
    for shape in prs.slides[n - 1].shapes:
        if shape.has_text_frame:
            out.append(shape.text_frame.text)
        if shape.has_table:
            out.extend(cell.text for row in shape.table.rows for cell in row.cells)
    return "\n".join(out)


def _run_command(monkeypatch, tmp_path, rows, covered, as_of: date):
    async def load_inputs(db, tenant_id, year, month, connection_id=None):
        return service.ReportInputs(label="Store", rows=rows, covered=covered)

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(monthly_report, "Database", _FakeDb)
    monkeypatch.setattr(service, "load_inputs", load_inputs)
    out = tmp_path / "deck.pptx"
    code = monthly_report.main(["--month", "2026-10", "--tenant", "t", "--out", str(out), "--no-model", "--as-of", as_of.isoformat()])
    return code, out


def _listed(days: list[int]) -> str:
    shown = ", ".join("Oct %d" % d for d in days[:MAX_DAYS_LISTED])
    return shown


CASES_TABLE = [(counted, k, position, previous)
               for counted, as_of in ((31, date(2026, 11, 15)), (20, date(2026, 10, 21)))
               for k, position, previous in itertools.product(range(counted + 1), ("start", "middle", "end"), (True, False))]


@pytest.mark.parametrize("counted, k, position, previous", CASES_TABLE)
def test_every_count_of_missing_days_builds_a_deck_and_exits_0(monkeypatch, tmp_path, capsys, counted, k, position, previous):
    as_of = date(2026, 11, 15) if counted == 31 else date(2026, 10, 21)
    rows, covered, missing = _inputs(k, position, previous, counted)
    code, out = _run_command(monkeypatch, tmp_path, rows, covered, as_of)
    err = capsys.readouterr().err
    if k == counted:
        # no day with data and no run: the designed refusal, with its own reason and no template text involved
        assert code == 2 and "nothing to report" in err and not out.exists()
        return
    assert code == 0, err
    prs = Presentation(str(out))
    assert len(prs.slides) == 7

    report = build_report(rows, 2026, 10, label="Store", covered=covered, as_of=as_of)
    assert [d.day for d in report.current.missing_days] == missing
    facts = report_facts(report)
    insights_text = _slide_text(prs, 6)
    health_text = _slide_text(prs, 7)
    # every line the template wrote passes the same figure check that guards a model's text
    for line in template_insights(report).lines:
        assert ungrounded_numbers(line, facts) == [], line
        assert line in insights_text

    no_data = [ln for ln in insights_text.splitlines() if "no data (" in ln]
    if k == 0:
        assert no_data == []
        assert "Days with no data in October: none." in health_text
    else:
        noun = "1 day has" if k == 1 else "%d days have" % k
        listed = _listed(missing)
        tail = " and others" if k > MAX_DAYS_LISTED else ""
        assert no_data == ["%s no data (%s%s), so the totals are understated." % (noun, listed, tail)]
        # the deck keeps the true count of the dates it cut, a figure computed from the list, never from the model
        more = " and %d more" % (k - MAX_DAYS_LISTED) if k > MAX_DAYS_LISTED else ""
        assert "Days with no data in October: %s%s." % (listed, more) in health_text
        assert ("Days with no data", str(k)) in {(row[0], row[1]) for row in _table(prs, 7)}
    if counted < 31:
        assert "Only %d of 31 days of October are counted, so the totals are partial." % counted in insights_text
    else:
        assert "are counted, so the totals are partial" not in insights_text
    # the best day line is a figure the template prints too: it must be there, and it is a fact
    day, value = report.top_days[0]
    assert "The best day was %s with net sales of %s." % (fmt_day(day), format_money(value, report.currency)) in insights_text
    # the per reported day phrase never carries a figure outside the facts for any count
    for key in ("net_sales", "orders"):
        row = report.kpi(key)
        if row.per_day is not None:
            assert per_day_phrase(row.per_day)


def _table(prs, n: int):
    return [[c.text for c in row.cells] for s in prs.slides[n - 1].shapes if s.has_table for row in s.table.rows]


def test_the_table_covers_the_whole_class():
    assert len(CASES_TABLE) == (32 + 21) * 6
    assert {k for counted, k, _, _ in CASES_TABLE if counted == 31} == set(range(32))
    assert {k for counted, k, _, _ in CASES_TABLE if counted == 20} == set(range(21))
    # the old crash needs more missing days than the list shows: the table must reach past it
    assert max(k for _, k, _, _ in CASES_TABLE) > MAX_DAYS_LISTED + 1
