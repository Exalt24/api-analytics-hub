"""Replay of the second blind corpus against the monthly report stage, and the table test of its one fixed class.

tests/corpus/monthly_report_corpus_blind_2.jsonl holds 53 cases (37 must-pass and 16 must-fail) written by a reviewer on a different model who had
seen neither the code's history nor the first corpus. Each carries `promise` (a quote from docs/MONTHLY_REPORT.md, checked verbatim below), `why`
(who is harmed on an ordinary day), `source` (invented) and `expected`; `actual` is what the code did when the case was written.

The narrative cases (24) are replayed here through the real figure check against the same invented gap month. The command cases are replayed by the
class table at the bottom. A case the owner decided not to fix is in KNOWN_LIMITS by number with the reason, and its live outcome is pinned to the
recorded `actual`, so a limit cannot move silently. The arithmetic and deck cases were checked by hand sums when the corpus was written and are the
subject of the first corpus's replay; here they are only counted.

CLASS-TABLE: a failure between the report command and its deck that the command must turn into the template or into an honest reason. Cases 49 and 50
found that a dropped connection or a non-JSON reply from the model provider escaped as a traceback with no deck; case 52 found that a deck held open
by PowerPoint was reported as a database failure. The table tests below drive the whole command for every provider failure mode and for both ways a
deck cannot be written, and also for a good reply, so the fix cannot become "always fall back".
"""
from __future__ import annotations

import itertools
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import monthly_report  # noqa: E402
from app.report import service  # noqa: E402
from app.report.insights import ReportInsightWriter, check_model_text, report_facts  # noqa: E402
from app.report.monthly import build_report, month_bounds  # noqa: E402
from report_fixtures import day_rows  # noqa: E402

CORPUS = ROOT / "tests" / "corpus" / "monthly_report_corpus_blind_2.jsonl"
SPEC = (ROOT / "docs" / "MONTHLY_REPORT.md").read_text(encoding="utf-8")
CASES = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]

#: case number -> why the code is deliberately left as it is (the lead's decision, scored 3 or less). The live outcome is pinned to the recorded `actual`.
KNOWN_LIMITS = {
    46: "F3 KNOWN_LIMIT 1x3: a figure inside a bracket after a full stop is read as part of the sentence before it, the same class as the accepted right-figure-in-the-wrong-role limit",
    47: "F6 KNOWN_LIMIT 2x1: the yen sign is not a recognised symbol, so a correct JPY line is refused and the template is used, the safe direction",
}

AS_OF = date(2026, 11, 3)
GAPCOV = [(date(2026, 9, 1), date(2026, 10, 14)), (date(2026, 10, 16), date(2026, 11, 1))]


def _days(y, m):
    d, end = month_bounds(y, m)
    while d < end:
        yield d
        d += timedelta(days=1)


def _gen(y, m, skip=(), cur="USD", refund_heavy=False):
    """orders 20 + (day*7)%11; gross orders*(4200 + (day*37)%900); refunds a twentieth of gross on every fourth day (a refund heavy month: six fifths of
    gross on every third day instead); buyers three quarters of orders."""
    rows = []
    for d in _days(y, m):
        if d.day in skip:
            continue
        o = 20 + (d.day * 7) % 11
        g = o * (4200 + (d.day * 37) % 900)
        r = (g * 6) // 5 if refund_heavy and d.day % 3 == 0 else (g // 20 if d.day % 4 == 0 else 0)
        rows += day_rows(d, o, g, r, (o * 3) // 4, cur)
    return rows


FULL = [(date(2026, 9, 1), date(2026, 11, 1))]


def _full(cur="USD", heavy=False, october=True):
    rows = _gen(2026, 9, cur=cur) + (_gen(2026, 10, cur=cur, refund_heavy=heavy) if october else [])
    return build_report(rows, 2026, 10, label="Example Outfitters", covered=FULL, as_of=AS_OF)


GAP = build_report(_gen(2026, 9) + _gen(2026, 10, skip=(14, 15, 22)), 2026, 10, label="Example Outfitters", covered=GAPCOV, as_of=AS_OF)
REPORTS = {"gap month": GAP, "EUR month": _full("EUR"), "JPY month": _full("JPY"), "no orders month": _full(october=False),
           "refund heavy month": _full(heavy=True)}


def test_the_corpus_has_53_cases_of_both_kinds_and_every_promise_is_in_the_spec():
    assert len(CASES) == 53
    assert {c["expect"] for c in CASES} == {"pass", "fail"}
    for n, c in enumerate(CASES):
        assert c["source"] in ("real", "invented") and len(c["why"]) >= 10, n
        assert len(c["promise"]) >= 15 and c["promise"].lower() in SPEC.lower(), (n, c["promise"])


NARRATIVE = [(n, c) for n, c in enumerate(CASES) if c["input"]["kind"] == "narrative"]


def _outcome(case) -> str:
    rep = REPORTS[case["input"]["report"]]
    _, reason = check_model_text(case["input"]["text"], report_facts(rep))
    return "refused" if reason else "accepted"


@pytest.mark.parametrize("n, case", NARRATIVE, ids=[str(n) for n, _ in NARRATIVE])
def test_every_narrative_case_gets_the_outcome_the_spec_gives_or_its_pinned_limit(n, case):
    live = _outcome(case)
    if n in KNOWN_LIMITS:
        assert live == case["actual"].split()[0].rstrip(":"), KNOWN_LIMITS[n]
        assert live != case["expected"].split()[0], "the limit is gone, delete it from KNOWN_LIMITS"
    else:
        assert live == case["expected"].split()[0], case["why"]


# ---------------------------------------------------------------- the command, end to end, no database

class _FakeDb:
    def __init__(self, *a, **k):
        pass

    async def connect(self):
        pass

    async def close(self):
        pass


GOOD = "Net sales total was $31,720.89 in October, 7.1% below September's $34,130.23."


def _writer(handler):
    return ReportInsightWriter("k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _reply(content):
    return lambda req: httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _raise(exc):
    def handler(req):
        raise exc(exc.__name__, request=req)
    return handler


PROVIDER = {
    "connection dropped": _raise(httpx.ConnectError),
    "timeout": _raise(httpx.ReadTimeout),
    "not json": lambda req: httpx.Response(200, text="<html>bad gateway</html>"),
    "http 500": lambda req: httpx.Response(500, text="upstream said no"),
    "rate limited": lambda req: httpx.Response(429, text="slow down"),
    "no choices": lambda req: httpx.Response(200, json={"choices": []}),
    "refused figures": _reply("Net sales fell 7% from September."),
}


def _rows():
    return _gen(2026, 9) + _gen(2026, 10, skip=(14, 15, 22))


def _run(monkeypatch, tmp_path, writer, extra=()):
    async def load_inputs(db, tenant_id, year, month, connection_id=None):
        return service.ReportInputs(label="Store", rows=_rows(), covered=GAPCOV)

    monkeypatch.setattr(monthly_report, "Database", _FakeDb)
    monkeypatch.setattr(service, "load_inputs", load_inputs)
    monkeypatch.setattr(monthly_report, "model_writer", lambda disabled: writer)
    out = tmp_path / "deck.pptx"
    code = monthly_report.main(["--month", "2026-10", "--tenant", "t", "--out", str(out), "--as-of", "2026-11-03", *extra])
    return code, out


# CLASS-TABLE: every provider failure mode, with and without --dump, generated with itertools.product
@pytest.mark.parametrize("mode, dump", list(itertools.product(sorted(PROVIDER), ("", "--dump"))))
def test_any_provider_failure_gives_a_template_deck_and_exit_0(monkeypatch, tmp_path, capsys, mode, dump):
    code, out = _run(monkeypatch, tmp_path, _writer(PROVIDER[mode]), extra=(dump,) if dump else ())
    printed = capsys.readouterr()
    assert code == 0 and out.exists(), mode
    assert "insights: template_fallback" in printed.out
    assert "traceback" not in printed.err.lower()


def test_a_good_model_reply_is_still_used_so_the_fix_is_not_always_fall_back(monkeypatch, tmp_path, capsys):
    code, out = _run(monkeypatch, tmp_path, _writer(_reply(GOOD)))
    assert code == 0 and out.exists()
    assert "insights: model" in capsys.readouterr().out


def test_a_deck_that_cannot_be_written_is_named_as_the_deck_not_the_database(monkeypatch, tmp_path, capsys):
    def save(*a, **k):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(service, "save_deck", save)
    code, _ = _run(monkeypatch, tmp_path, None, extra=("--no-model",))
    err = capsys.readouterr().err
    assert code == 2 and "deck could not be saved" in err and "PermissionError" in err and "database" not in err


def test_a_database_that_cannot_be_read_is_still_named_as_the_database(monkeypatch, tmp_path, capsys):
    async def load_inputs(db, *a, **k):
        raise ConnectionRefusedError(111, "refused")

    monkeypatch.setattr(monthly_report, "Database", _FakeDb)
    monkeypatch.setattr(service, "load_inputs", load_inputs)
    monkeypatch.setattr(monthly_report, "model_writer", lambda disabled: None)
    code = monthly_report.main(["--month", "2026-10", "--tenant", "t", "--out", str(tmp_path / "d.pptx"), "--no-model"])
    err = capsys.readouterr().err
    assert code == 2 and "database could not be read" in err
