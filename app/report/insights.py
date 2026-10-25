"""The narrative of a monthly report, written only from numbers that were computed.

Two writers, one rule. The rule extends the one `app/ai/catalog_copy.py` enforces for
product copy: every figure in the text must be one of the report's own displayed figures,
or the text is refused.

  * `template_insights` is the stand-in used when no model key is set, and the fallback
    when a model's text is refused. It is fixed wording filled from the computed display
    strings.
  * `ReportInsightWriter` asks an OpenAI-compatible model for 3 to 6 short lines, but only
    after `report_facts` has reduced the report to a flat set of facts. A reply that fails
    the check is dropped whole and the template text is used instead, never a "mostly
    right" paragraph.

WHAT THE CHECK DOES AND DOES NOT PROVE. It proves that each figure in the text (an amount,
a percentage, a count, a date, an amount spelled as a word) exists among the report's
displayed figures, written the same way, and that a figure beside a named metric belongs to
that metric. It is checked per sentence: a sentence that names two metrics may use either
one's figures, so a figure can still be attached to the wrong one of two named metrics. It
does not check direction words ("up" about a figure that fell) or causes. The prompt forbids
both, and the KPI table carries the true direction.

The old catalogue check (`verify_grounded`, a substring test) still runs first. It lets a
made up "41" through when a fact holds "412", so the report adds the exact figure check
below; the catalogue copy keeps its own behaviour.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from app.ai.catalog_copy import (
    CatalogCopyWriter,
    GenerationRejected,
    clean_text,
    facts_to_text,
    verify_grounded,
)
from app.report.monthly import (
    MonthlyReport,
    ReportError,
    days_text,
    format_money,
    fmt_day,
    largest_move,
    move_phrase,
    partial_clause,
    plural,
)

#: A report's commentary is a few lines. Past this a reply is an essay, and the more
#: text there is the more room for a claim the numbers do not carry.
MAX_LINES = 6

REPORT_PROMPT = """You write the commentary for a monthly performance report on one online store.

Rules, in order of importance:
1. Use ONLY the facts given. Copy every amount, percentage, count and date exactly as it is
   written in the facts: same digits, same dollar sign, same rounding, dates as "Oct 14".
   Never compute, round, add, subtract or estimate, and never write a number that is not in
   the facts. Never write a year. Never write a number as a word.
2. Put a figure only next to the metric it belongs to. Say up when a change is positive and
   down when it is negative, as the facts say.
3. Give no cause and no forecast. Do not say why a number moved.
4. If the facts say days have no data, say the totals are understated and name no cause.
5. Three to five lines, one sentence per line, plain declarative English, no bullets,
   no headings, no marketing language, no exclamation marks.

Reply with the lines only. No preamble, no label."""


@dataclass(frozen=True)
class Insights:
    lines: tuple[str, ...]
    #: "template" (no model asked), "model" (checked and used) or "template_fallback"
    source: str
    #: Which writer ran and why. Kept in the file's properties and the text read back, not on a slide.
    label: str
    model: str | None = None
    rejected_reason: str | None = None


# ---------------------------------------------------------------- facts

def report_facts(report: MonthlyReport) -> dict[str, Any]:
    """The report as flat facts, in the very strings the deck prints.

    Keys that start with a metric's label belong to that metric: the check uses that to
    keep a figure next to its own metric. Dates are written `Oct 14` and months by name
    with no year, so there is no bare year or day number to borrow.
    """
    cur, prev = report.current, report.previous
    facts: dict[str, Any] = {
        "report period": cur.short,
        "store": report.label,
        "days in the month": cur.days_in_month,
        "days counted": cur.days_expected,
        "days with orders": len(cur.data_days),
        "days with no orders": len(cur.quiet_days),
        "days with no data": len(cur.missing_days),
        "dates with no data": days_text(cur.missing_days),
        "previous period": prev.short if prev else "none, no earlier month is stored",
    }
    if prev is not None:
        facts["previous period days with no data"] = len(prev.missing_days)
    for row in report.kpis:
        facts[row.label] = row.current_display
        if prev is not None:
            facts[row.label + " previous"] = row.previous_display
        facts[row.label + " change"] = row.change_display
        facts[row.label + " change percent"] = row.pct_display
        if row.note:
            facts[row.label + " note"] = row.note
    mover = largest_move(report)
    if mover is not None:
        facts["largest move"] = mover.label
    if report.top_days and report.currency:
        day, value = report.top_days[0]
        facts["best day"] = fmt_day(day)
        facts["Net sales on the best day"] = format_money(value, report.currency)
    return facts


# ---------------------------------------------------------------- the check

_MONTHS_FULL = ("January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December")
_MONTHS = "|".join(_MONTHS_FULL + tuple(m[:3] for m in _MONTHS_FULL))

#: A figure is a date (`Oct 14`, `October 2026`) or an amount, count or percentage. Every
#: part is bounded, the digits may sit against letters (`USD9`, `x5`, `Q4`) because a figure
#: glued to a letter is still a figure, and a comma only counts inside a valid thousands group.
_FIGURE_RE = re.compile((
    r"(?P<date>\b(?:@MONTHS@)\.?\s\d{4}\b|\b(?:@MONTHS@)\.?\s\d{1,2}\b)"
    r"|(?P<num>[+-]?(?P<cur>\$|[A-Z]{3}[ ]?)?(?P<digits>\d{1,3}(?:,\d{3}){1,8}(?:\.\d{1,9})?|\d{1,15}(?:\.\d{1,9})?)(?P<pct>%)?)"
).replace("@MONTHS@", _MONTHS))

#: A comma between digits that is not a thousands separator: `1,4%`, `6,07`, `1,2000`.
_BAD_COMMA_RE = re.compile(r"\d,(?=\d)(?!\d{3}(?!\d))")

#: Amounts and changes written as words, whole words only ("one" is not found inside "none").
NUMBER_WORDS = frozenset((
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy "
    "eighty ninety hundred thousand million billion dozen half twice thrice double doubled "
    "triple tripled halved hundreds thousands millions dozens").split())

#: What a sentence may call a metric. A label not listed here is matched by its own words.
ALIASES = {
    "Average order value": ("average order value", "order value", "aov"),
    "Buyers per active day": ("buyers per active day", "buyers"),
}

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z$\d])|\n")
_WORD_RE = re.compile(r"[a-z]+")


def _canonical(match: re.Match) -> tuple[str, str]:
    if match.group("date"):
        words = match.group("date").replace(".", "").split()
        return "date", "%s %s" % (words[0][:3].title(), words[1])
    kind = "pct" if match.group("pct") else "money" if match.group("cur") else "num"
    return kind, match.group("digits").replace(",", "")


def figures(text: str) -> list[tuple[tuple[str, str], str, int]]:
    """Every figure in `text` as ((kind, value), the text it was read from, where it ends)."""
    return [(_canonical(m), m.group(0).strip(), m.end()) for m in _FIGURE_RE.finditer(text)]


#: A bare count is a day count only when "days" follows it ("2 days", "9 of 31 days").
_DAY_COUNT_RE = re.compile(r"\s+(?:of\s+\d{1,3}\s+)?days?\b")


def _metric_groups(facts: Mapping[str, Any]) -> dict[str, set]:
    """label -> the figures of every fact whose key is that label or starts with it."""
    labels = [k[: -len(" change percent")] for k in facts if k.endswith(" change percent")]
    groups: dict[str, set] = {}
    for label in labels:
        group: set = set()
        for key, value in facts.items():
            if key == label or key.startswith(label + " "):
                group |= {fig for fig, _, _ in figures(str(value)) if fig[0] != "date"}
        groups[label] = group
    return groups


def _mentioned(sentence: str, labels) -> list[str]:
    low = sentence.lower()
    found = []
    for label in labels:
        for alias in ALIASES.get(label, (label.lower(),)):
            if re.search(r"\b%s\b" % re.escape(alias), low):
                found.append(label)
                break
    return found


def ungrounded_numbers(text: str, facts: Mapping[str, Any]) -> list[str]:
    """Every figure or number word in `text` that the facts do not back, as written in `text`.

    Order: the catalogue check first, then number words, malformed separators, and the exact
    figure check (a figure must be a displayed figure of a fact, a date must be a fact's date,
    and a figure in a sentence that names a metric must be one of that metric's own, or a
    count of days).
    """
    bad: list[str] = list(verify_grounded(text, facts))

    def flag(item: str) -> None:
        if item not in bad:
            bad.append(item)

    fact_words = set(_WORD_RE.findall(" ".join(str(v) for v in facts.values()).lower()))
    for word in _WORD_RE.findall(text.lower()):
        if word in NUMBER_WORDS and word not in fact_words:
            flag(word)
    for m in _BAD_COMMA_RE.finditer(text):
        flag(text[m.start(): m.end() + 1])

    groups = _metric_groups(facts)
    grouped_keys = {k for k in facts for g in groups if k == g or k.startswith(g + " ")}
    global_figs: set = set()
    all_dates: set = set()
    for key, value in facts.items():
        for fig, _, _ in figures(str(value)):
            if fig[0] == "date":
                all_dates.add(fig)
            elif key not in grouped_keys:
                global_figs.add(fig)
    everything = set().union(*groups.values()) if groups else set()

    for sentence in _SENTENCE_RE.split(text):
        mentioned = _mentioned(sentence, groups)
        allowed_metric = set().union(*(groups[g] for g in mentioned)) if mentioned else everything
        for fig, raw, end in figures(sentence):
            if fig[0] == "date":
                if fig not in all_dates:
                    flag(raw)
            elif fig in allowed_metric:
                continue
            elif fig in global_figs and (not mentioned or _DAY_COUNT_RE.match(sentence, end)):
                continue
            else:
                flag(raw)
    # "215.00" from the catalogue check and "$215.00" from this one are one finding.
    return [b for b in bad if not any(b != other and b in other for other in bad)]


# ---------------------------------------------------------------- the template stand-in

_VERBS = {"avg_order_value": "was"}


def _comparison(row, prev) -> str:
    if prev is None or row.change is None:
        return ""
    return ", %s from %s (%s)" % (move_phrase(row), prev.short, row.previous_display)


def template_lines(report: MonthlyReport) -> list[str]:
    cur, prev = report.current, report.previous
    clause = partial_clause(report)
    lines: list[str] = []
    for key in ("net_sales", "orders", "avg_order_value", "refunds"):
        row = report.kpi(key)
        if row.current is None:
            continue
        sentence = "%s %s %s in %s" % (row.label, _VERBS.get(key, "were"), row.current_display, cur.short)
        compare = _comparison(row, prev)
        if compare and clause:
            compare += ", and %s" % clause
        lines.append(sentence + compare + ".")
    mover = largest_move(report)
    if mover is not None and prev is not None:
        sentence = "The biggest move was %s, %s from %s" % (mover.label, move_phrase(mover), prev.short)
        lines.append(sentence + (", and %s" % clause if clause else "") + ".")
    if report.top_days and report.currency:
        day, value = report.top_days[0]
        lines.append("The best day was %s with net sales of %s." % (fmt_day(day), format_money(value, report.currency)))
    if prev is None:
        lines.append("No earlier month is stored, so no month over month change is shown.")
    if cur.missing_days:
        n = len(cur.missing_days)
        lines.append("%s no data (%s), so the totals are understated."
                     % (plural(n, "day") + (" has" if n == 1 else " have"), days_text(cur.missing_days)))
    if not cur.complete:
        lines.append("Only %d of %d days of %s are counted, so the totals are partial."
                     % (cur.days_expected, cur.days_in_month, cur.short))
    return lines


def template_insights(report: MonthlyReport, *, reason: str = "no model key is set") -> Insights:
    lines = template_lines(report)
    facts = report_facts(report)
    bad = [n for line in lines for n in ungrounded_numbers(line, facts)]
    if bad:
        # A template that quotes a figure it was not given is a bug in this file, and the
        # one place that must not publish it.
        raise ReportError("the template text quotes figures that are not facts: %s" % ", ".join(bad))
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
        return [], "figures that are not in the computed figures: %s" % ", ".join(bad)
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
        "Written by %s. Every figure was checked against the computed figures." % writer.model,
        model=writer.model,
    )


def _fallback(report: MonthlyReport, reason: str) -> Insights:
    base = template_insights(report, reason=reason)
    return Insights(base.lines, "template_fallback", base.label, rejected_reason=reason)
