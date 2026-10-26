"""Two classes found in output round 1 of the monthly run, each closed with a generated table.

CLASS-TABLE 1, the schedule: the ads, search and website syncs end their default window twelve hours behind UTC, so a deck built at
06:00 UTC on the 1st never held the last day of the month for those three sources. The table runs every source that has a
`settled_window_end` through every month of 2026 and 2027 (and the leap years 2024 and 2028) at the instant the committed workflow
fires, and each sync end must cover the last day of the month the deck reports and stop at the 1st.

CLASS-TABLE 2, the request body: the endpoint takes four optional connection ids and answers 422 "name one" for a client with two
active ad accounts, but the workflow's body could only carry month and as_of. The table evaluates the committed body expression text
in node for every subset of the four ids, with and without as_of, and for an id left blank.
"""
from __future__ import annotations

import calendar
import importlib
import itertools
import json
import pkgutil
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app.connectors as connectors  # noqa: E402
from test_n8n_workflow import NODES, WF  # noqa: E402

SCHEDULE = "Schedule, 1st of the month 13:00 UTC"
SCHEDULE_YEARS = (2024, 2026, 2027, 2028)      # 2026 and 2027 in full, and the leap years 2024 and 2028 for a 29 day February


def _sources_with_a_settled_window_end() -> list[str]:
    """Every connector module that ends its default sync window at settled_window_end: ga4 (website), gsc (search), meta (ads)."""
    names = []
    for found in pkgutil.iter_modules(connectors.__path__):
        if hasattr(importlib.import_module("app.connectors." + found.name), "settled_window_end"):
            names.append(found.name)
    return sorted(names)


SETTLED_SOURCES = _sources_with_a_settled_window_end()


def _schedule_time(year: int, month: int) -> datetime:
    """The instant the committed workflow fires in `month`: its cron expression is `minute hour 1 * *` in the workflow's UTC timezone."""
    minute, hour, day, month_field, weekday = NODES[SCHEDULE]["parameters"]["rule"]["interval"][0]["expression"].split()
    assert (day, month_field, weekday) == ("1", "*", "*")
    assert WF["settings"]["timezone"] == "UTC"
    return datetime(year, month, 1, int(hour), int(minute), tzinfo=timezone.utc)


def _reported_month(fired: datetime) -> tuple[int, int]:
    """The month the Settings node names at that instant: `$now.minus({ months: 1 })` in UTC."""
    return (fired.year, fired.month - 1) if fired.month > 1 else (fired.year - 1, 12)


def test_the_sources_with_a_settled_window_end_are_exactly_the_three_the_sync_scripts_use():
    assert SETTLED_SOURCES == ["ga4", "gsc", "meta"]
    for name in SETTLED_SOURCES:
        script = (ROOT / "scripts" / ("sync_%s.py" % name)).read_text(encoding="utf-8")
        assert "settled_window_end(datetime.now(timezone.utc))" in script, name


# CLASS-TABLE: every source with a settled window, every month of 2026 and 2027 (and the leap years 2024 and 2028), at the instant the workflow fires
@pytest.mark.parametrize("source, year, month", list(itertools.product(SETTLED_SOURCES, SCHEDULE_YEARS, range(1, 13))))
def test_the_sync_end_at_the_schedule_time_covers_the_last_day_of_the_month_it_reports_and_no_more(source, year, month):
    fired = _schedule_time(year, month)
    reported_year, reported_month = _reported_month(fired)
    last_day = date(reported_year, reported_month, calendar.monthrange(reported_year, reported_month)[1])
    end = importlib.import_module("app.connectors." + source).settled_window_end(fired)
    # the window is half open: an end after the last day stores the last day, an end past the 1st would reach into the next month
    assert end > last_day, "%s synced at %s ends %s: %s is never stored" % (source, fired.isoformat(), end, last_day)
    assert end == last_day + timedelta(days=1), "%s synced at %s ends %s, past the month it reports" % (source, fired.isoformat(), end)


def test_the_table_reaches_a_leap_february_a_month_ending_on_sunday_and_every_month_of_2026_and_2027():
    reported = {_reported_month(_schedule_time(y, m)) for y in SCHEDULE_YEARS for m in range(1, 13)}
    assert (2028, 2) in reported and calendar.monthrange(2028, 2)[1] == 29
    assert (2024, 2) in reported and calendar.monthrange(2024, 2)[1] == 29
    assert (2027, 2) in reported and calendar.monthrange(2027, 2)[1] == 28
    assert {(y, m) for y in (2026, 2027) for m in range(1, 13)} <= reported
    assert any(date(y, m, calendar.monthrange(y, m)[1]).weekday() == 6 for y, m in reported), "no month in the table ends on a Sunday"


def test_the_old_six_in_the_morning_schedule_run_through_the_same_rule_misses_the_last_day():
    """The failure the table exists for: at 06:00 UTC on 1 November the sync ends 31 October, so 31 October is never stored."""
    from app.connectors.ga4 import settled_window_end
    old = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
    assert settled_window_end(old) == date(2026, 10, 31)
    assert not settled_window_end(old) > date(2026, 10, 31)


# ---------------------------------------------------------------- the request body carries the tenant's own connection ids

NODE = shutil.which("node")
BODY = NODES["Build the report"]["parameters"]["jsonBody"]
CONNECTION_FIELDS = ("connection_id", "web_connection_id", "paid_connection_id", "search_connection_id")

_EVAL = r"""
const items = JSON.parse(process.argv[2]);
const text = process.argv[1].replace(/^=\{\{\s*/, '').replace(/\s*\}\}$/, '');
console.log(JSON.stringify(items.map($json => JSON.parse(eval(text)))));
"""


def _evaluate_body(items: list[dict]) -> list[dict]:
    """Evaluate the committed body expression text (and nothing else) in node with each item as `$json`, returning the parsed bodies."""
    done = subprocess.run([NODE, "-e", _EVAL, BODY, json.dumps(items)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed: the body expression is evaluated by node")


@needs_node
def test_a_tenant_without_the_ids_gets_the_body_it_always_got():
    plain = {"name": "Example Outfitters", "api_key": "aah_x"}
    got = _evaluate_body([{"month": "2026-10", "as_of": "", "tenant": plain}, {"month": "2026-10", "as_of": "2026-11-02", "tenant": plain}])
    assert got == [{"month": "2026-10"}, {"month": "2026-10", "as_of": "2026-11-02"}]
    assert list(got[1]) == ["month", "as_of"]


# CLASS-TABLE: every subset of the four ids (16), with and without as_of, plus each id named and left blank
@needs_node
@pytest.mark.parametrize("as_of", ["", "2026-11-02"])
def test_each_id_a_tenant_carries_reaches_the_body_and_nothing_else_does(as_of):
    ids = {f: "00000000-0000-4000-8000-00000000000%d" % n for n, f in enumerate(CONNECTION_FIELDS, 1)}
    subsets = [c for r in range(0, 5) for c in itertools.combinations(CONNECTION_FIELDS, r)]
    assert len(subsets) == 16
    items = []
    for chosen in subsets:
        tenant = {"name": "Dune Surf", "api_key": "aah_x", "note": "kept out", **{f: ids[f] for f in chosen}}
        items.append({"month": "2026-10", "as_of": as_of, "tenant": tenant})
    for blank in CONNECTION_FIELDS:                      # an entry that names the field and leaves it empty sends nothing for it
        items.append({"month": "2026-10", "as_of": as_of, "tenant": {"name": "Dune Surf", "api_key": "aah_x", blank: ""}})
    bodies = _evaluate_body(items)
    for chosen, body in zip(subsets, bodies):
        expect = {"month": "2026-10", **({"as_of": as_of} if as_of else {}), **{f: ids[f] for f in CONNECTION_FIELDS if f in chosen}}
        assert body == expect, chosen
        assert "api_key" not in body and "name" not in body and "note" not in body
    assert bodies[len(subsets):] == [{"month": "2026-10", **({"as_of": as_of} if as_of else {})}] * 4


def test_the_body_expression_names_exactly_the_fields_the_endpoint_accepts():
    from app.api.reports import MonthlyRequest
    accepted = {f for f in MonthlyRequest.model_fields if f.endswith("connection_id")}
    assert accepted == set(CONNECTION_FIELDS)
    assert all("'%s'" % f in BODY for f in CONNECTION_FIELDS)


def test_the_readme_names_the_four_optional_ids_and_the_schedule_hour():
    readme = (ROOT / "n8n" / "README.md").read_text(encoding="utf-8")
    assert all(f in readme for f in CONNECTION_FIELDS)
    assert "0 13 1 * *" in readme and "13:00 UTC" in readme and "06:00" not in readme
