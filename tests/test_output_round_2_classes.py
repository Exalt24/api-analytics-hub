"""Three classes found in output round 2 of the ads, search and monthly run work, each closed with a generated table.

CLASS-TABLE 1, a ratio whose two sides come from different days: the return on ad spend picked its days by a purchase VALUE row, and
Meta leaves a zero out of `action_values`, so a day with spend and no purchases (count stored as 0, no value row) was dropped from the
return while its spend stayed in the spend total (spend EUR 2,000.00 and value EUR 3,000.00 printed a return of 3.00, not 1.50). The
table gives each of two days every mix of a spend row, a purchase count (none, 0, 3) and a purchase value (none, 0, 300) and holds the
return, its coverage and the purchase value's coverage to one rule written here again, independently of the code. The same file holds the
check for the other ratios (click through rate, cost per click, search click through rate, average position): the day shapes every
connector can write are derived from the connector itself, and each ratio must take both sides from the same days.

CLASS-TABLE 2, a default sync window that cannot cover a 31 day month: `--days 30` ending 1 November stored 2 October to 1 November, so
1 October was never stored for ads, search or the website in any 31 day month. The table runs every month of 2024 to 2028 at the
instants the schedule allows (12:00, 12:30 and the 13:00 trigger) through the three scripts' real argument parser and the shared sync
body, and the window it hands the runner must hold the whole previous month.

CLASS-TABLE 3, a sync that writes the account the environment names into the connection it was told to use: with client A's
META_AD_ACCOUNT_ID or GSC_SITE_URL still set, client B's deck showed A's figures and the sync said it succeeded. The table runs every
platform with every spelling of one account on both sides (a leading act_, case, a trailing slash, a sc-domain: prefix, a properties/
prefix) and with another account, and the other account must be refused before anything is claimed while every spelling of the same
one runs.

KNOWN_LIMIT: a day with clicks and no impressions row (or the reverse), or spend and no clicks | no connector can write it: Meta and Search
Console store a day's figures together and refuse a row that lacks one, so it needs a hand edited row, and the table covers every shape
the connectors do write.
KNOWN_LIMIT: a day with purchases above 0 and no value row stays out of the return | its value was never reported, so counting it as 0 would
print a return of 0.00 beside "Purchase value n/a" for a pixel that sends counts and no values, a figure nobody measured.
KNOWN_LIMIT: the store source has no default window to check | no script here syncs it (Sync Now takes a number of days, default 7), so
the default window table covers the three sources that have one: ads, search and website.
"""
from __future__ import annotations

import asyncio
import calendar
import importlib
import itertools
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app.sync.cli as cli  # noqa: E402
from app.connectors.gsc import GSCConnector  # noqa: E402
from app.connectors.meta import MetaConnector  # noqa: E402
from app.report.paid import PaidMonth, _values, paid_month  # noqa: E402
from app.report.search import search_month  # noqa: E402
from app.sync.runner import RunOutcome  # noqa: E402

AS_OF = date(2026, 11, 15)
#: One client for every connector the tables build: making one loads the certificate store, and none of these connectors sends a request.
CLIENT = httpx.AsyncClient()


# ---------------------------------------------------------------- CLASS-TABLE 1: the return takes both sides from the same days

def _rows(day: date, spend, count, value):
    out = []
    if spend is not None:
        out += [{"observed_on": day, "metric_key": "ad_spend", "value_numeric": spend, "currency": "EUR"},
                {"observed_on": day, "metric_key": "ad_impressions", "value_numeric": 1000, "currency": None},
                {"observed_on": day, "metric_key": "ad_clicks", "value_numeric": 10, "currency": None}]
    if count is not None:
        out.append({"observed_on": day, "metric_key": "ad_purchases", "value_numeric": count, "currency": None})
    if value is not None:
        out.append({"observed_on": day, "metric_key": "ad_purchase_value", "value_numeric": value, "currency": "EUR"})
    return out


def _oracle(days):
    """The rule, written again: a day is in the return when it has spend and a purchase value, a value being its row or 0 when the
    day's purchase count is 0 (Meta leaves a zero out) in a month that has a value row on some day. A count above 0 with no value row has
    an unknown value and stays out, as does a day with spend and no purchase figure at all, and so does every day of a month with no
    value row (a pixel that sends counts and no values). Returns (days in the return, spend over them, value over them, days with a value)."""
    spend_sum = value_sum = used = valued = 0
    reports_values = any(value is not None for _, _, value in days)
    for spend, count, value in days:
        day_value = value if value is not None else (0 if reports_values and count == 0 else None)
        if day_value is not None:
            valued += 1
        if spend is not None and day_value is not None:
            spend_sum += spend
            value_sum += day_value
            used += 1
    return used, spend_sum, value_sum, valued


SHAPES = list(itertools.product((None, 10000), (None, 0, 3), (None, 0, 30000)))
OCTOBER = [date(2026, 10, d) for d in (1, 2, 3)]
PAIR = OCTOBER[:2]


# CLASS-TABLE: two days, each with every mix of a spend row, a purchase count and a purchase value (18 shapes a day, 324 months)
@pytest.mark.parametrize("days", list(itertools.product(SHAPES, repeat=2)))
def test_the_return_is_taken_over_one_set_of_days_for_every_mix_of_count_value_and_spend(days):
    rows = [r for day, shape in zip(PAIR, days) for r in _rows(day, *shape)]
    month = paid_month(rows, 2026, 10, as_of=AS_OF)
    used, spend_sum, value_sum, valued = _oracle(days)
    assert (month.joint_days, month.joint_spend, month.joint_value) == (used, spend_sum, value_sum), days
    assert month.value_days == valued, days
    assert month.purchase_days == sum(1 for _, count, _ in days if count is not None), days
    if month.has_baseline:
        shown, exact = _values(month)
        want = None if spend_sum == 0 else Decimal(value_sum) / spend_sum
        assert exact["roas"] == want, days
        if month.value_days:
            assert month.totals["ad_purchase_value"] == value_sum + sum(v for s, c, v in days if v is not None and s is None), days


def test_a_pixel_that_sends_purchase_counts_and_no_values_still_shows_not_available_for_the_value_and_the_return():
    """The ordinary input beside the harm: a month of counts (0 on some days, 3 on others) and no value row at all, which printed
    n/a for the value and the return before and must not start printing EUR 0.00 and 0.00 for purchases nobody valued."""
    rows = []
    for i in range(20):
        rows += _rows(date(2026, 10, i + 1), 10000, 0 if i % 2 else 3, None)
    month = paid_month(rows, 2026, 10, as_of=AS_OF)
    shown, _ = _values(month)
    assert shown["ad_purchase_value"] is None and shown["roas"] is None and shown["ad_purchases"] == 30
    assert month.value_days == 0 and month.joint_days == 0


def test_the_month_the_finding_describes_prints_1_50_and_not_3_00():
    """Spend EUR 100 a day for 20 days, 10 days of 3 purchases worth EUR 300 and 10 days with spend and a purchase count of 0."""
    rows = []
    for i in range(20):
        day = date(2026, 10, i + 1)
        rows += _rows(day, 10000, 3, 30000) if i % 2 == 0 else _rows(day, 10000, 0, None)
    month = paid_month(rows, 2026, 10, as_of=AS_OF)
    shown, _ = _values(month)
    assert month.totals["ad_spend"] == 200000 and month.totals["ad_purchase_value"] == 300000
    assert shown["roas"] == Decimal("1.50") and month.joint_days == 20 and month.value_days == 20


# ---- the other ratios: the day shapes a connector can write, derived from the connector, and each ratio over the same days

def _meta_day_shapes() -> set[frozenset[str]]:
    """The metric keys a Graph row of every purchase list shape produces for one day."""
    connector = MetaConnector(ad_account_id="1001", access_token="t", client=CLIENT)
    lists = (None, [], [{"action_type": "link_click", "7d_click": "4"}], [{"action_type": "omni_purchase", "7d_click": "3"}])
    shapes = set()
    for actions, values in itertools.product(lists, lists):
        row = {"account_currency": "EUR", "spend": "10.00", "impressions": "100", "clicks": "5",
               "date_start": "2026-10-03", "date_stop": "2026-10-03"}
        if actions is not None:
            row["actions"] = actions
        if values is not None:
            row["action_values"] = values
        points = connector._points({"data": [row]}, date(2026, 10, 3), date(2026, 10, 4), set(), set())
        shapes.add(frozenset(p.metric_key for p in points))
    return shapes


def _gsc_day_shapes() -> set[frozenset[str]]:
    connector = GSCConnector(site_url="sc-domain:example.com", access_token="t", client=CLIENT)
    shapes = set()
    for clicks, impressions in ((0, 0), (0, 10), (5, 10), (10, 10)):
        row = {"keys": ["2026-10-03"], "clicks": clicks, "impressions": impressions, "ctr": 0.1, "position": 4.5}
        points = connector._daily_points([row], date(2026, 10, 3), date(2026, 10, 4), set())
        shapes.add(frozenset(p.metric_key for p in points))
    return shapes


META_SHAPES = sorted(_meta_day_shapes(), key=sorted)
GSC_SHAPES = sorted(_gsc_day_shapes(), key=sorted)
VALUES = {"ad_spend": 12000, "ad_impressions": 1500, "ad_clicks": 40, "ad_purchases": 3, "ad_purchase_value": 9000,
          "search_clicks": 12, "search_impressions": 150, "search_position": 5250}


def _day_rows(day, keys):
    return [{"observed_on": day, "metric_key": k, "value_numeric": VALUES[k] + day.day, "currency": "EUR" if k in ("ad_spend", "ad_purchase_value") else None}
            for k in sorted(keys)]


def test_every_day_a_connector_can_write_carries_both_sides_of_each_of_its_ratios():
    assert META_SHAPES and GSC_SHAPES
    for keys in META_SHAPES:
        assert {"ad_spend", "ad_impressions", "ad_clicks"} <= keys, sorted(keys)
    for keys in GSC_SHAPES:
        assert keys == {"search_clicks", "search_impressions", "search_position"}, sorted(keys)


# CLASS-TABLE: every shape a Meta connector writes (or no row at all) on each of three days, and the same for a Search Console day
@pytest.mark.parametrize("days", list(itertools.product([None] + META_SHAPES, repeat=3)))
def test_click_through_rate_and_cost_per_click_take_both_sides_from_the_same_days(days):
    rows = [r for day, keys in zip(OCTOBER, days) if keys for r in _day_rows(day, keys)]
    month = paid_month(rows, 2026, 10, as_of=AS_OF)
    on = [d for d, keys in zip(OCTOBER, days) if keys]
    for key in ("ad_spend", "ad_impressions", "ad_clicks"):
        assert [d for d in on if key in month.daily[d]] == on, "%s is on fewer days than the others" % key
    if month.has_baseline:
        _, exact = _values(month)
        clicks = sum(VALUES["ad_clicks"] + d.day for d in on)
        assert exact["ctr"] == Decimal(clicks) * 100 / sum(VALUES["ad_impressions"] + d.day for d in on)
        assert exact["cpc"] == Decimal(sum(VALUES["ad_spend"] + d.day for d in on)) / clicks


@pytest.mark.parametrize("days", list(itertools.product([None] + GSC_SHAPES, repeat=3)))
def test_search_click_through_rate_and_position_take_both_sides_from_the_same_days(days):
    rows = [r for day, keys in zip(OCTOBER, days) if keys for r in _day_rows(day, keys)]
    month = search_month(rows, 2026, 10, as_of=AS_OF)
    on = [d for d, keys in zip(OCTOBER, days) if keys]
    if on:
        weight = sum(VALUES["search_impressions"] + d.day for d in on)
        want = Decimal(sum((VALUES["search_position"] + d.day) * (VALUES["search_impressions"] + d.day) for d in on)) / weight / 1000
        assert month.position_exact == want
        assert month.totals["search_clicks"] == sum(VALUES["search_clicks"] + d.day for d in on)
        assert month.totals["search_impressions"] == weight


# ---------------------------------------------------------------- the stand-ins for the database and the runner of the two sync tables

class _Conn:
    def __init__(self, platform, account):
        self.platform, self.account = platform, account

    async def fetchval(self, query, *args):
        return self.platform

    async def fetchrow(self, query, *args):
        return {"platform": self.platform, "external_account": self.account}


class _Unscoped:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Db:
    def __init__(self, platform, account):
        self.conn = _Conn(platform, account)

    async def connect(self):
        return None

    def unscoped(self):
        return _Unscoped(self.conn)

    async def close(self):
        return None


class _Runner:
    calls: list[dict] = []

    def __init__(self, db):
        pass

    async def run_once(self, **kw):
        _Runner.calls.append(kw)
        return RunOutcome("run-1", True, "succeeded")


SCRIPTS = {"meta": "sync_meta", "gsc": "sync_gsc", "ga4": "sync_ga4"}
ENV = {"meta": ("META_AD_ACCOUNT_ID", "META_ACCESS_TOKEN"), "gsc": ("GSC_SITE_URL", "GSC_ACCESS_TOKEN"), "ga4": ("GA4_PROPERTY_ID", "GA4_ACCESS_TOKEN")}
TOKEN = "tok-SECRET-1234"
sys.path.insert(0, str(ROOT / "scripts"))


class _Clock(datetime):
    """`datetime` whose now() is the instant a table row names, put in place of the one a sync script imported."""

    fixed = datetime(2026, 11, 1, 13, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.fixed.astimezone(tz) if tz else cls.fixed


def _sync(monkeypatch, capsys, platform, stored, account, argv, now=None):
    """Run the real script of `platform` with `account` in its environment against a connection that stores `stored`: (exit code, run_once calls, out, err)."""
    script = importlib.import_module(SCRIPTS[platform])
    account_var, token_var = ENV[platform]
    monkeypatch.setenv(account_var, account)
    monkeypatch.setenv(token_var, TOKEN)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: CLIENT)
    monkeypatch.setattr(cli, "Database", lambda **kw: _Db(platform, stored))
    monkeypatch.setattr(cli, "SyncRunner", _Runner)
    if now is not None:
        _Clock.fixed = now
        monkeypatch.setattr(script, "datetime", _Clock)
    _Runner.calls = []
    code = script.main(["--tenant", "t", "--connection", "c", *argv])
    seen = capsys.readouterr()
    return code, _Runner.calls, seen.out, seen.err


# ---------------------------------------------------------------- CLASS-TABLE 2: the default window holds the whole previous month

SCHEDULE_MINUTES = (0, 30, 60)       # 12:00, 12:30 and the 13:00 trigger: the schedule allows a sync from 12:00 to the build at 13:00
MONTHS = [(y, m) for y in range(2024, 2029) for m in range(1, 13)]


def _first_and_last_of_previous_month(year, month):
    py, pm = (year, month - 1) if month > 1 else (year - 1, 12)
    return date(py, pm, 1), date(py, pm, calendar.monthrange(py, pm)[1])


def test_the_three_sync_scripts_all_run_the_one_shared_sync_body():
    for name in SCRIPTS.values():
        source = (ROOT / "scripts" / (name + ".py")).read_text(encoding="utf-8")
        assert "run_connection_sync(" in source, name
        assert "timedelta(days=args.days)" not in source, "%s still cuts its own window" % name


# CLASS-TABLE: every source with a default window, every month of 2024 to 2028 (the leap Februaries too), at each instant of the sync's hour
@pytest.mark.parametrize("source, year, month, minutes", [(s, y, m, mi) for s in SCRIPTS for (y, m) in MONTHS for mi in SCHEDULE_MINUTES])
def test_the_default_window_at_the_schedule_time_holds_the_whole_previous_month(monkeypatch, capsys, source, year, month, minutes):
    fired = datetime(year, month, 1, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    first, last = _first_and_last_of_previous_month(year, month)
    account = {"meta": "1001", "gsc": "sc-domain:example.com", "ga4": "123456"}[source]
    code, calls, out, err = _sync(monkeypatch, capsys, source, {"meta": "act_1001", "gsc": account, "ga4": account}[source], account, [], now=fired)
    assert code == 0, err
    start, end = calls[0]["window_start"], calls[0]["window_end"]
    assert start <= first, "%s synced at %s starts %s: %s is never stored" % (source, fired.isoformat(), start, first)
    assert end > last, "%s synced at %s ends %s: %s is never stored" % (source, fired.isoformat(), end, last)
    assert end == last + timedelta(days=1)


# CLASS-TABLE: the same for a sync on any other day of the month, which may be a rerun for the month that has just ended
@pytest.mark.parametrize("year, month, day", [(y, m, d) for (y, m) in MONTHS[::5] for d in range(1, calendar.monthrange(y, m)[1] + 1)])
def test_a_default_sync_on_any_day_still_holds_the_whole_previous_month(monkeypatch, capsys, year, month, day):
    first, last = _first_and_last_of_previous_month(year, month)
    code, calls, out, err = _sync(monkeypatch, capsys, "meta", "act_1001", "1001", ["--end", date(year, month, day).isoformat()])
    start, end = calls[0]["window_start"], calls[0]["window_end"]
    assert code == 0 and end == date(year, month, day)
    assert start <= first and end > last, "a sync ending %s starts %s: %s is never stored" % (end, start, first)


def test_an_explicit_days_still_means_that_many_days_back_from_the_end(monkeypatch, capsys):
    _, calls, _, _ = _sync(monkeypatch, capsys, "meta", "act_1001", "1001", ["--days", "3", "--end", "2026-09-04"])
    assert (calls[0]["window_start"], calls[0]["window_end"]) == (date(2026, 9, 1), date(2026, 9, 4))


@pytest.mark.parametrize("source", list(SCRIPTS))
@pytest.mark.parametrize("days, accepted", [("1", True), ("3650", True), ("2", True), ("0", False), ("3651", False), ("-5", False)])
def test_days_must_be_one_to_3650_for_every_script(monkeypatch, capsys, source, days, accepted):
    argv = ["--days", days, "--end", "2026-11-01"]
    if accepted:
        _, calls, _, _ = _sync(monkeypatch, capsys, source, {"meta": "act_1001", "gsc": "sc-domain:example.com", "ga4": "123456"}[source],
                               {"meta": "1001", "gsc": "sc-domain:example.com", "ga4": "123456"}[source], argv)
        assert calls[0]["window_start"] == date(2026, 11, 1) - timedelta(days=int(days))
    else:
        with pytest.raises(SystemExit) as stopped:
            _sync(monkeypatch, capsys, source, "x", "1001", argv)
        assert stopped.value.code == 2 and "--days must be 1 to 3650" in capsys.readouterr().err


@pytest.mark.parametrize("source", list(SCRIPTS))
def test_a_missing_token_stops_every_script_with_exit_two_and_the_variable_named(monkeypatch, capsys, source):
    account_var, token_var = ENV[source]
    monkeypatch.setenv(account_var, {"meta": "1001", "gsc": "sc-domain:example.com", "ga4": "123456"}[source])
    monkeypatch.delenv(token_var, raising=False)
    monkeypatch.setattr(cli, "Database", lambda **kw: pytest.fail("the database was opened without a token"))
    assert importlib.import_module(SCRIPTS[source]).main(["--tenant", "t", "--connection", "c"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("no sync: ") and token_var in err and "Traceback" not in err


@pytest.mark.parametrize("source", list(SCRIPTS))
def test_the_help_of_every_script_says_what_the_default_window_is(capsys, source):
    with pytest.raises(SystemExit) as stopped:
        importlib.import_module(SCRIPTS[source]).main(["--help"])
    words = " ".join(capsys.readouterr().out.split())
    assert stopped.value.code == 0
    assert "how many days back from the end (default: from the first day of the month before the end's month)" in words
    assert "first day NOT pulled, YYYY-MM-DD" in words


# ---------------------------------------------------------------- CLASS-TABLE 3: a sync refuses an account the connection does not hold

#: platform -> the spellings a stored value and an environment value can take, grouped by the account they name, plus another account and a near miss.
#: A Search Console Domain property and a URL prefix property are two different properties, so they are two groups.
ACCOUNTS = {
    "meta": {"groups": {"1001": ["act_1001", "1001", "ACT_1001", " act_1001 "]}, "env": ["1001", "act_1001"], "other": "2002", "near": "100"},
    "ga4": {"groups": {"123456": ["123456", "properties/123456", "Properties/123456 "]}, "env": ["123456", "properties/123456"],
            "other": "654321", "near": "12345"},
    "gsc": {"groups": {"domain": ["sc-domain:example.com", "SC-DOMAIN:Example.com", "example.com", "sc-domain:example.com/"],
                       "prefix": ["https://www.example.com/", "https://www.example.com", "HTTPS://www.Example.com/"]},
            "env": ["sc-domain:example.com", "https://www.example.com/", "https://www.example.com"],
            "other": "sc-domain:client-b.example", "near": "sc-domain:example.co"},
}


def _group_of(platform, text):
    for group, spellings in ACCOUNTS[platform]["groups"].items():
        if text in spellings:
            return group
    raise KeyError(text)


def _stored(platform):
    return [s for spellings in ACCOUNTS[platform]["groups"].values() for s in spellings]


# CLASS-TABLE: every platform, every stored spelling against every environment spelling of the same account, then against another account and a near miss
@pytest.mark.parametrize("platform, stored, env", [(p, s, e) for p, t in ACCOUNTS.items() for s in _stored(p) for e in t["env"]])
def test_every_spelling_of_the_same_account_runs_and_a_different_property_type_is_refused(monkeypatch, capsys, platform, stored, env):
    code, calls, out, err = _sync(monkeypatch, capsys, platform, stored, env, ["--end", "2026-11-01"])
    if _group_of(platform, stored) == _group_of(platform, env):
        assert code == 0 and len(calls) == 1, "%s %r against %r was refused: %s" % (platform, stored, env, err)
    else:
        assert code == 2 and calls == [] and stored.strip() in err, (platform, stored, env, err)


@pytest.mark.parametrize("platform, stored, which", [(p, s, w) for p, t in ACCOUNTS.items() for s in _stored(p) for w in ("other", "near")])
def test_another_account_is_refused_before_anything_is_claimed_and_the_reason_names_both_and_no_token(monkeypatch, capsys, platform, stored, which):
    wrong = ACCOUNTS[platform][which]
    code, calls, out, err = _sync(monkeypatch, capsys, platform, stored, wrong, ["--end", "2026-11-01"])
    assert code == 2 and calls == [] and out == "", (platform, stored, wrong, code, out, err)
    assert stored.strip() in err and wrong.removeprefix("act_") in err, err
    assert TOKEN not in out + err and err.startswith("no sync: "), err
