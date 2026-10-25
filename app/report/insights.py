"""The narrative of a monthly report, written only from numbers that were computed.

Two writers, one rule. The rule is the one `app/ai/catalog_copy.py` enforces for product
copy: every number in the text must already be among the facts, or the text is refused.
Here it is applied twice, the original substring check (`verify_grounded`) and a stricter
whole number check below, because a substring check passes a made up "4" whenever any
fact happens to contain a 4 inside a longer number.

  * `template_insights` is the stand-in used when no model key is set, and the fallback
    when a model's text is refused. It is fixed wording filled from the computed
    display strings, and the deck labels it as a template.
  * `ReportInsightWriter` asks an OpenAI-compatible model for 3 to 6 short lines, but
    only after `report_facts` has reduced the report to a flat set of facts. A reply that
    fails the check is dropped whole and the template text is used instead, never a
    "mostly right" paragraph.

What is checked is numbers. A model can still say "up" about a figure that fell, or
claim a cause; the prompt forbids both and nothing in code verifies it, so the deck
says which writer produced the text and the KPI table carries the real direction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.ai.catalog_copy import (
    NUMBER_RE,
    CatalogCopyWriter,
    GenerationRejected,
    _normalise,
    clean_text,
    facts_to_text,
    verify_grounded,
)
from app.report.monthly import MonthlyReport, ReportError, days_text, format_money

#: A report's commentary is a few lines. Past this a reply is an essay, and the more
#: text there is the more room for a claim the numbers do not carry.
MAX_LINES = 6

REPORT_PROMPT = """You write the commentary for a monthly performance report on one online store.

Rules, in order of importance:
1. Use ONLY the facts given. Copy every number exactly as it is written in the facts,
   with the same digits and the same rounding. Never compute, round, add, subtract or
   estimate a number, and never write a number that is not in the facts.
2. Say up when a change is positive and down when it is negative, as the facts say.
3. Give no cause and no forecast. Do not say why a number moved.
4. If a fact says days are missing, say the totals are understated and name no cause.
5. Three to five lines, one sentence per line, plain declarative English, no bullets,
   no headings, no marketing language, no exclamation marks.

Reply with the lines only. No preamble, no label."""


@dataclass(frozen=True)
class Insights:
    lines: tuple[str, ...]
    #: "template" (no model asked), "model" (checked and used) or "template_fallback"
    source: str
    #: The sentence printed on the insights slide, so a reader knows who wrote the text.
    label: str
    model: str | None = None
    rejected_reason: str | None = None


# ---------------------------------------------------------------- facts

def report_facts(report: MonthlyReport) -> dict[str, Any]:
    """The report as flat facts, using the same display strings the deck prints.

    The strings are shared on purpose: a number a reader sees on a slide and a number the
    text is allowed to quote are the same characters, so "grounded" means "matches the
    table".
    """
    cur, prev = report.current, report.previous
    facts: dict[str, Any] = {
        "report period": cur.name,
        "store": report.label,
        "days with data": len(cur.data_days),
        "days in the period": cur.days_expected,
        "days with no orders": len(cur.quiet_days),
        "days missing": len(cur.missing_days),
        "missing dates": days_text(cur.missing_days),
        "previous period": prev.name if prev else "none, no earlier month is stored",
    }
    for row in report.kpis:
        facts[row.label] = row.current_display
        if prev is not None:
            facts[row.label + " previous"] = row.previous_display
        facts[row.label + " change"] = row.change_display
        facts[row.label + " change percent"] = row.pct_display
        if row.note:
            facts[row.label + " note"] = row.note
    if report.top_days and report.currency:
        day, value = report.top_days[0]
        facts["best day"] = day.isoformat()
        facts["best day net sales"] = format_money(value, report.currency)
    return facts


# ---------------------------------------------------------------- the check

def ungrounded_numbers(text: str, facts: Mapping[str, Any]) -> list[str]:
    """Every number or number word in `text` that is not one of the facts' own numbers.

    Number words ("five") are covered by verify_grounded, which is why it runs first.
    """
    bad = list(verify_grounded(text, facts))
    allowed = {_normalise(token) for token in NUMBER_RE.findall(facts_to_text(facts))}
    for raw in NUMBER_RE.findall(text):
        if _normalise(raw) not in allowed and raw not in bad:
            bad.append(raw)
    return bad


# ---------------------------------------------------------------- the template stand-in

def _move(pct_display: str, change_display: str, note: str) -> str:
    """'up 8.4%', 'down 12.4%', 'unchanged', or '' when there is no comparison."""
    if note in ("no previous month", "no previous value", "not available") or change_display == "n/a":
        return ""
    if note == "zero baseline":
        return "from a previous month of zero"
    if pct_display in ("0.0%",):
        return "unchanged"
    return ("up " if pct_display.startswith("+") else "down ") + pct_display.lstrip("+-")


def template_lines(report: MonthlyReport) -> list[str]:
    cur, prev = report.current, report.previous
    lines: list[str] = []
    for key, noun in (("net_sales", "Net sales"), ("orders", "Orders")):
        row = report.kpi(key)
        move = _move(row.pct_display, row.change_display, row.note)
        sentence = "%s were %s in %s" % (noun, row.current_display, cur.name)
        if move and prev is not None:
            sentence += ", %s on %s (%s)" % (move, prev.name, row.previous_display)
        lines.append(sentence + ".")
    aov = report.kpi("avg_order_value")
    if aov.current is not None:
        move = _move(aov.pct_display, aov.change_display, aov.note)
        sentence = "Average order value was %s" % aov.current_display
        if move and prev is not None:
            sentence += ", %s on %s" % (move, prev.name)
        lines.append(sentence + ".")
    refunds = report.kpi("refunds")
    lines.append("Refunds were %s." % refunds.current_display)
    if report.top_days and report.currency:
        day, value = report.top_days[0]
        lines.append("The best day was %s with net sales of %s." % (day.isoformat(), format_money(value, report.currency)))
    if prev is None:
        lines.append("No earlier month is stored, so no month over month change is shown.")
    if cur.missing_days:
        lines.append("%d days have no data (%s), so the totals are understated."
                     % (len(cur.missing_days), days_text(cur.missing_days)))
    return lines


def template_insights(report: MonthlyReport, *, reason: str = "no model key is set") -> Insights:
    lines = template_lines(report)
    facts = report_facts(report)
    bad = [n for line in lines for n in ungrounded_numbers(line, facts)]
    if bad:
        # A template that quotes a number it was not given is a bug in this file, and the
        # one place that must not publish it.
        raise ReportError("the template text quotes numbers that are not facts: %s" % ", ".join(bad))
    return Insights(
        tuple(lines), "template",
        "Written by a fixed template from the computed figures (%s). No model was used." % reason,
    )


# ---------------------------------------------------------------- the model writer

class ReportInsightWriter(CatalogCopyWriter):
    """The catalogue copywriter's transport and error handling, with the report prompt."""

    async def write(self, facts: Mapping[str, Any]) -> str:
        text = facts_to_text(facts)
        if not text.strip():
            raise GenerationRejected("no usable facts supplied")
        return await self._complete(text, system_prompt=REPORT_PROMPT)

    @property
    def model(self) -> str:
        return self._model


def check_model_text(text: str, facts: Mapping[str, Any]) -> tuple[list[str], str | None]:
    """The lines of a model reply, or (empty, the reason it was refused)."""
    lines = [ln.strip(" \t-*") for ln in clean_text_lines(text)]
    lines = [ln for ln in lines if ln]
    if not lines:
        return [], "the model returned no text"
    if len(lines) > MAX_LINES:
        return [], "the model returned %d lines, the cap is %d" % (len(lines), MAX_LINES)
    if any("!" in ln for ln in lines):
        return [], "the model used an exclamation mark"
    bad = ungrounded_numbers("\n".join(lines), facts)
    if bad:
        return [], "numbers that are not in the computed figures: %s" % ", ".join(bad)
    return lines, None


def clean_text_lines(text: str) -> list[str]:
    return [clean_text(line) for line in text.splitlines()]


async def write_insights(report: MonthlyReport, writer: ReportInsightWriter | None = None) -> Insights:
    """Model text when it passes every check, otherwise the template, and say which."""
    if writer is None:
        return template_insights(report)
    facts = report_facts(report)
    try:
        raw = await writer.write(facts)
    except GenerationRejected as exc:
        return _fallback(report, "the model call failed: %s" % str(exc)[:120])
    lines, reason = check_model_text(raw, facts)
    if reason:
        return _fallback(report, "the model text was refused, " + reason)
    return Insights(
        tuple(lines), "model",
        "Written by %s. Every number was checked against the computed figures." % writer.model,
        model=writer.model,
    )


def _fallback(report: MonthlyReport, reason: str) -> Insights:
    base = template_insights(report, reason=reason)
    return Insights(base.lines, "template_fallback", base.label, rejected_reason=reason)
