"""The MSP backlog analysis: ticket and alert drivers, SLA risk and technician touch time.

Three kinds of test. Hand worked cases with the answer written out (every edge: the window's first and last instant, a goal exactly at `as_of`, exactly at the horizon, one second either side).
Seeded random worlds compared with a brute force oracle written in this file with plain loops and no code shared with `app/msp/analysis.py`. And properties that must hold for any world: the
result does not depend on the order of the input, a record outside the window or opened after `as_of` changes nothing, and every second of time is accounted for exactly once.

Every refusal runs a positive control first (the same call without the defect succeeds).
"""
from __future__ import annotations

import json
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.msp.analysis import (AnalysisError, alert_drivers, build_report, normalise_title, render_markdown, sla_risk, ticket_drivers,  # noqa: E402
                              touch_time)
from app.msp.ledger import Ledger  # noqa: E402
from app.msp.records import AlertRecord, DeviceRecord, TicketRecord, TimeRecord  # noqa: E402
from app.msp.sample import generate_world  # noqa: E402

W0, W1 = date(2026, 10, 1), date(2026, 10, 8)
AS_OF = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


def at(day, hour=9, minute=0, second=0, month=10):
    return datetime(2026, month, day, hour, minute, second, tzinfo=timezone.utc)


def t(tid, opened, **kw):
    base = dict(source="connectwise", ticket_id=str(tid), title="Disk space low on SRV-01", category="Alerts", subcategory="Storage", priority="P3", status="New", company="A",
                opened_at=opened, closed=False)
    base.update(kw)
    if base.get("closed_at") is not None:
        base["closed"] = True
    return TicketRecord(**base)


def e(eid, ticket, worked, seconds, tech="Kim", source="connectwise"):
    return TimeRecord(source=source, entry_id=str(eid), ticket_id=None if ticket is None else str(ticket), technician=tech, worked_on=worked, seconds=seconds)


def a(uid, device, **kw):
    base = dict(source="ninjaone", alert_id=str(uid), device_id=device, device_name=None, organization_id=1, source_type="CONDITION_AGENT_CPU", source_name="CPU high", subject=None,
                severity=None, created_at=at(1))
    base.update(kw)
    return AlertRecord(**base)


# ---------------------------------------------------------------- the title

@pytest.mark.parametrize("title, want", [
    ("Disk space low on SRV-042", "disk space low on srv #"), ("disk space low on srv-17", "disk space low on srv #"), ("Password reset for user 12345", "password reset for user #"),
    ("  Re:   VPN   down!!  ", "re vpn down"), ("Printer 3rd floor", "printer #rd floor"), ("12345", "#"), ("!!!", "(none)"), ("", "(none)"), ("SRV-1 and SRV-22", "srv # and srv #"),
])
def test_a_title_is_reduced_to_what_does_not_change_from_one_ticket_to_the_next(title, want):
    assert normalise_title(title) == want


# ---------------------------------------------------------------- ticket drivers

def drivers_world():
    return [t(1, at(2), company="A"), t(2, at(3), company="A", title="disk space low on srv-02"), t(3, at(3, 10), company="B"), t(4, at(4), company=None),
            t(5, at(5), company="A", title="Password reset for user 5", category="Service Desk", subcategory="Passwords"),
            t(6, at(9), company="A"),                                               # after the window
            t(7, at(30, month=9), company="A")]                                     # before it


def test_the_tickets_opened_in_the_window_are_grouped_by_category_issue_and_title_and_repeats_are_counted_per_company():
    out = ticket_drivers(drivers_world(), window_start=W0, window_end=W1)
    assert out["opened_tickets"] == 5 and out["driver_groups"] == 2
    disk, pw = out["by_title"]
    assert (disk["category"], disk["subcategory"], disk["title"], disk["tickets"], disk["companies"], disk["repeats"], disk["repeat_percent"]) == ("Alerts", "Storage", "disk space low on srv #", 4, 2, 1, 25)
    assert (disk["first_opened"], disk["last_opened"], disk["example_ticket"]) == ("2026-10-02T09:00:00Z", "2026-10-04T09:00:00Z", "1")
    assert (pw["tickets"], pw["companies"], pw["repeats"], pw["repeat_percent"]) == (1, 1, 0, 0)
    assert [(c["category"], c["tickets"], c["share_percent"], c["repeats"]) for c in out["by_category"]] == [("Alerts", 4, 80, 1), ("Service Desk", 1, 20, 0)]


def test_a_ticket_with_no_company_is_never_a_repeat_of_another_with_no_company():
    out = ticket_drivers([t(1, at(2), company=None), t(2, at(3), company=None), t(3, at(4), company=None)], window_start=W0, window_end=W1)
    assert out["by_title"][0]["tickets"] == 3 and out["by_title"][0]["repeats"] == 0 and out["by_title"][0]["companies"] == 0


def test_the_window_includes_its_first_instant_and_excludes_its_last_day():
    inside = [t(1, datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)), t(2, datetime(2026, 10, 7, 23, 59, 59, tzinfo=timezone.utc))]
    outside = [t(3, datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)), t(4, datetime(2026, 10, 8, 0, 0, 0, tzinfo=timezone.utc))]
    assert ticket_drivers(inside + outside, window_start=W0, window_end=W1)["opened_tickets"] == 2


def test_the_day_of_a_ticket_is_its_utc_day_whatever_zone_it_was_written_in():
    manila = timezone(timedelta(hours=8))
    late = t(1, datetime(2026, 10, 8, 6, 0, tzinfo=manila))                           # 22:00 UTC on the 7th: inside
    early = t(2, datetime(2026, 10, 1, 7, 0, tzinfo=manila))                          # 23:00 UTC on Sept 30: outside
    assert ticket_drivers([late, early], window_start=W0, window_end=W1)["opened_tickets"] == 1


def test_drivers_are_ranked_by_repeats_then_tickets_then_name_and_cut_to_top():
    ts = []
    n = 0
    for title, cat, count, companies in [("Alpha", "X", 4, 4), ("Beta", "X", 3, 1), ("Gamma", "A", 3, 1), ("Delta", "X", 2, 2)]:
        for i in range(count):
            n += 1
            ts.append(t(n, at(2), title=title, category=cat, company="C%d" % (i % companies)))
    out = ticket_drivers(ts, window_start=W0, window_end=W1, top=3)
    assert [d["title"] for d in out["by_title"]] == ["gamma", "beta", "alpha"], "Beta and Gamma both repeat twice and have 3 tickets: the category name breaks the tie"
    assert out["driver_groups"] == 4 and len(out["by_title"]) == 3


@pytest.mark.parametrize("kw, match", [({"window_start": W1, "window_end": W0}, "at least one day"), ({"window_start": W0, "window_end": W0}, "at least one day"),
                                       ({"window_start": datetime(2026, 10, 1), "window_end": W1}, "two dates"), ({"window_start": "2026-10-01", "window_end": W1}, "two dates"),
                                       ({"top": 0}, "top must be"), ({"top": True}, "top must be"), ({"top": 2.5}, "top must be")])
def test_ticket_drivers_refuses_a_bad_window_or_top_and_a_good_one_passes(kw, match):
    ticket_drivers([], window_start=W0, window_end=W1)
    with pytest.raises(AnalysisError, match=match):
        ticket_drivers([], **{"window_start": W0, "window_end": W1, **kw})


# ---------------------------------------------------------------- alert drivers

def test_alerts_are_grouped_by_source_and_repeats_are_the_alerts_a_device_raised_beyond_its_first():
    alerts = [a(1, 1), a(2, 1), a(3, 2), a(4, 3, source_type="AGENT_OFFLINE", source_name=None), a(5, 1, source_type="AGENT_OFFLINE", source_name=None)]
    devices = [DeviceRecord("ninjaone", 1, "SRV-01", None, 1, False), DeviceRecord("ninjaone", 2, "SRV-02", None, 1, True), DeviceRecord("ninjaone", 3, "WS-03", None, 1, True)]
    out = alert_drivers(alerts, devices)
    cpu, off = out["by_source"]
    assert (cpu["source_type"], cpu["source_name"], cpu["alerts"], cpu["devices"], cpu["repeats"], cpu["share_percent"]) == ("CONDITION_AGENT_CPU", "CPU high", 3, 2, 1, 60)
    assert (off["source_name"], off["alerts"], off["devices"], off["repeats"], off["share_percent"]) == ("(none)", 2, 2, 0, 40)
    assert [(d["device_name"], d["alerts"], d["sources"]) for d in out["noisiest_devices"]] == [("SRV-01", 3, 2), ("SRV-02", 1, 1), ("WS-03", 1, 1)]
    assert (out["active_alerts"], out["devices_with_alerts"], out["devices"], out["devices_offline"]) == (5, 3, 3, 2)


def test_a_device_is_named_from_the_device_list_then_the_alert_then_its_number():
    alerts = [a(1, 1, device_name="From alert"), a(2, 2, device_name="From alert 2"), a(3, 3)]
    out = alert_drivers(alerts, [DeviceRecord("ninjaone", 1, "From list", None, 1, False)])
    assert {d["device_id"]: d["device_name"] for d in out["noisiest_devices"]} == {1: "From list", 2: "From alert 2", 3: "device 3"}


def test_no_alerts_is_an_empty_ranking_and_not_a_division_by_zero():
    out = alert_drivers([])
    assert out["active_alerts"] == 0 and out["by_source"] == [] and out["noisiest_devices"] == []


def test_alert_drivers_refuses_a_bad_top_and_a_good_one_passes():
    alert_drivers([a(1, 1)], top=1)
    with pytest.raises(AnalysisError, match="top must be"):
        alert_drivers([a(1, 1)], top=0)


# ---------------------------------------------------------------- SLA risk

def sla(tickets, **kw):
    kw.setdefault("as_of", AS_OF)
    kw.setdefault("window_start", W0)
    kw.setdefault("window_end", W1)
    return sla_risk(tickets, **kw)


def test_an_open_ticket_is_breached_at_risk_ok_or_without_a_goal_by_where_its_goal_falls_against_as_of_and_the_horizon():
    cases = {"breached_a_second_ago": AS_OF - timedelta(seconds=1), "due_right_now": AS_OF, "one_second_inside_the_horizon": AS_OF + 4 * H - timedelta(seconds=1),
             "exactly_at_the_horizon": AS_OF + 4 * H, "far": AS_OF + 40 * H}
    ts = [t(i + 1, at(5), resolution_due_at=due) for i, due in enumerate(cases.values())] + [t(10, at(5), resolution_due_at=None)]
    out = sla(ts)
    assert (out["open"], out["breached"], out["at_risk"], out["ok"], out["no_goal"], out["likely_breaches"]) == (6, 1, 2, 2, 1, 3)
    [row] = out["by_category_and_priority"]
    assert (row["open"], row["breached"], row["at_risk"], row["ok"], row["no_goal"]) == (6, 1, 2, 2, 1)


def test_the_horizon_is_the_one_asked_for():
    ts = [t(1, at(5), resolution_due_at=AS_OF + 10 * H)]
    assert sla(ts, horizon_hours=4)["at_risk"] == 0 and sla(ts, horizon_hours=11)["at_risk"] == 1 and sla(ts, horizon_hours=10)["at_risk"] == 0


def test_what_was_open_is_judged_at_as_of_not_at_the_time_the_ledger_was_written():
    ts = [t(1, at(5), closed_at=AS_OF - H),                                          # closed before: not open
          t(2, at(5), closed_at=AS_OF + H),                                          # closed after as_of: it was open then
          t(3, at(5), resolved_at=AS_OF - H),                                        # resolved before, not closed: no longer backlog
          t(4, at(5), resolved_at=AS_OF + H),                                        # resolved after: open then
          t(5, at(5), closed_at=AS_OF, resolved_at=None),                            # closed exactly at as_of: done
          t(6, AS_OF + H)]                                                           # opened after as_of: not yet there
    assert sla(ts)["open"] == 2


def test_a_first_response_is_overdue_when_due_before_as_of_and_not_given_by_as_of():
    ts = [t(1, at(5), first_response_due_at=AS_OF - H),                                                       # never answered: overdue
          t(2, at(5), first_response_due_at=AS_OF - H, first_response_at=AS_OF - 2 * H),                      # answered in time
          t(3, at(5), first_response_due_at=AS_OF - H, first_response_at=AS_OF + H),                          # answered, but after as_of
          t(4, at(5), first_response_due_at=AS_OF),                                                           # due this very second: not yet overdue
          t(5, at(5), first_response_due_at=None),
          t(6, at(5), first_response_due_at=AS_OF - H, first_response_at=AS_OF)]                              # answered exactly at as_of: answered
    assert sla(ts)["response_overdue"] == 2


def test_groups_are_ranked_by_breached_then_at_risk_then_open_and_the_oldest_open_ticket_is_in_whole_hours():
    ts = [t(1, at(5), category="B", priority="P1", resolution_due_at=AS_OF - H), t(2, at(5), category="A", priority="P1", resolution_due_at=AS_OF + H),
          t(3, at(5), category="A", priority="P2", resolution_due_at=AS_OF + H), t(4, at(5), category="A", priority="P2", resolution_due_at=AS_OF + H),
          t(5, at(9, 11, 30), category="C", priority=None, resolution_due_at=AS_OF + 90 * H)]
    out = sla(ts)
    assert [(r["category"], r["priority"]) for r in out["by_category_and_priority"]] == [("B", "P1"), ("A", "P2"), ("A", "P1"), ("C", "(none)")]
    assert {(r["category"], r["priority"]): r["oldest_open_hours"] for r in out["by_category_and_priority"]}[("C", "(none)")] == 24, "from 11:30 on the 9th to 12:00 on the 10th is 24 hours and 30 minutes"


def test_tickets_finished_in_the_window_are_late_when_finished_after_their_goal_and_the_percent_rounds_down():
    ts = [t(1, at(2), resolved_at=at(3), resolution_due_at=at(3) - H),                  # late
          t(2, at(2), resolved_at=at(3), resolution_due_at=at(3)),                      # on the goal exactly: on time
          t(3, at(2), resolved_at=at(3), resolution_due_at=at(4)),                      # on time
          t(4, at(2), closed_at=at(4), resolution_due_at=None),                         # finished, no goal: not counted
          t(5, at(2), closed_at=at(20), resolution_due_at=at(3))]                       # finished after the window
    out = sla(ts)
    assert (out["finished_in_window_with_goal"], out["finished_late"], out["finished_late_percent"]) == (3, 1, 33)
    assert sla([t(1, at(2))])["finished_late_percent"] is None


def test_flow_is_opened_against_finished_per_category_and_ranked_by_net():
    ts = [t(1, at(2), category="A"), t(2, at(2), category="A"), t(3, at(2), category="A", closed_at=at(3)), t(4, at(2), category="B", closed_at=at(3)),
          t(5, at(30, month=9), category="B", closed_at=at(3)), t(6, at(30, month=9), category="C")]
    flow = sla(ts)["flow_by_category"]
    assert [(f["category"], f["opened"], f["finished"], f["net"]) for f in flow] == [("A", 3, 1, 2), ("B", 1, 2, -1)], "C had neither in the window"


@pytest.mark.parametrize("kw, match", [({"as_of": datetime(2026, 10, 10)}, "timezone aware"), ({"horizon_hours": 0}, "horizon_hours"), ({"horizon_hours": True}, "horizon_hours"),
                                       ({"horizon_hours": 1.5}, "horizon_hours"), ({"top": 0}, "top must be"), ({"window_end": W0}, "at least one day")])
def test_sla_risk_refuses_a_bad_input_and_a_good_one_passes(kw, match):
    sla([])
    with pytest.raises(Exception, match=match):
        sla([], **kw)


# ---------------------------------------------------------------- touch time

def test_time_is_spread_over_the_category_of_its_ticket_with_the_technicians_under_it_and_what_cannot_be_placed_is_said():
    tickets = [t(1, at(2), category="A"), t(2, at(2), category="A"), t(3, at(2), category="B"), t(1, at(2), category="Z", source="autotask")]
    times = [e(1, 1, at(2), 3600, "Kim"), e(2, 1, at(3), 1800, "Lee"), e(3, 2, at(3), 600, "Kim"), e(4, 3, at(4), 7200, "Lee"), e(5, None, at(4), 900), e(6, 99, at(4), 300),
             e(7, 1, at(2), 100, "Kim", source="autotask"),                                # ticket 1 of ANOTHER system
             e(8, 1, at(9), 5000)]                                                         # outside the window
    out = touch_time(tickets, times, window_start=W0, window_end=W1)
    assert (out["entries_in_window"], out["total_seconds"], out["placed_seconds"], out["non_ticket_seconds"], out["unmatched_seconds"]) == (7, 3600 + 1800 + 600 + 7200 + 900 + 300 + 100, 13300, 900, 300)
    b, a_, z = out["by_category"]
    assert (b["category"], b["seconds"], b["entries"], b["tickets_touched"], b["mean_seconds_per_ticket"]) == ("B", 7200, 1, 1, 7200)
    assert (a_["category"], a_["seconds"], a_["entries"], a_["tickets_touched"], a_["mean_seconds_per_ticket"], a_["most_time_ticket"], a_["most_time_seconds"]) == ("A", 6000, 3, 2, 3000, "1", 5400)
    assert a_["technicians"] == [{"technician": "Kim", "seconds": 4200, "tickets": 2}, {"technician": "Lee", "seconds": 1800, "tickets": 1}]
    assert (z["category"], z["seconds"]) == ("Z", 100), "ticket 1 of Autotask is not ticket 1 of ConnectWise"


def test_the_mean_per_ticket_rounds_down_and_the_most_time_tie_goes_to_the_lowest_ticket_number():
    tickets = [t(1, at(2)), t(2, at(2)), t(3, at(2))]
    out = touch_time(tickets, [e(1, 1, at(2), 100), e(3, 3, at(2), 101), e(2, 2, at(2), 101)], window_start=W0, window_end=W1)
    [row] = out["by_category"]
    assert row["mean_seconds_per_ticket"] == 100 and row["most_time_ticket"] == "2" and row["most_time_seconds"] == 101


def test_time_is_judged_by_the_utc_day_it_was_worked_and_the_window_ends_before_its_last_day():
    tickets = [t(1, at(2))]
    times = [e(1, 1, datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc), 10), e(2, 1, datetime(2026, 10, 7, 23, 59, 59, tzinfo=timezone.utc), 20),
             e(3, 1, datetime(2026, 10, 8, 0, 0, 0, tzinfo=timezone.utc), 40), e(4, 1, datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc), 80)]
    assert touch_time(tickets, times, window_start=W0, window_end=W1)["total_seconds"] == 30


def test_no_time_is_an_empty_ranking():
    out = touch_time([t(1, at(2))], [], window_start=W0, window_end=W1)
    assert out["by_category"] == [] and out["total_seconds"] == 0


def test_touch_time_refuses_a_bad_window_and_top_and_a_good_one_passes():
    touch_time([], [], window_start=W0, window_end=W1)
    with pytest.raises(AnalysisError, match="at least one day"):
        touch_time([], [], window_start=W1, window_end=W0)
    with pytest.raises(AnalysisError, match="top must be"):
        touch_time([], [], window_start=W0, window_end=W1, top=0)


# ---------------------------------------------------------------- seeded worlds against a brute force oracle

TITLES = ["Disk space low on SRV-%d", "Password reset for user %d", "Printer offline at site %d", "VPN down %d", "Backup failed on SRV-%d", "disk space low on srv-%d"]
CATEGORIES = ["Service Desk", "Network", "Alerts"]


def random_world(rng):
    tickets, times, alerts, devices = [], [], [], []
    base = datetime.combine(W0, datetime.min.time(), tzinfo=timezone.utc)
    for i in range(rng.randint(0, 60)):
        opened = base + timedelta(days=rng.randint(-5, 11), minutes=rng.randint(0, 1439), seconds=rng.choice([0, 0, 30]))
        done = None
        if rng.random() < 0.5:
            done = opened + timedelta(minutes=rng.randint(0, 60 * 24 * 6))
        resolved = None
        if rng.random() < 0.4:
            resolved = opened + timedelta(minutes=rng.randint(0, 60 * 24 * 4))
            if done is not None and resolved > done:
                resolved = done
        goal = opened + timedelta(minutes=rng.randint(0, 60 * 24 * 8), seconds=rng.choice([0, 0, 1])) if rng.random() < 0.8 else None
        if rng.random() < 0.15:
            goal = AS_OF + timedelta(hours=rng.choice([-1, 0, 4, 4, 5]), seconds=rng.choice([0, -1]))
        response_due = opened + timedelta(minutes=rng.randint(0, 600)) if rng.random() < 0.7 else None
        response = opened + timedelta(minutes=rng.randint(0, 900)) if rng.random() < 0.5 else None
        zone = timezone(timedelta(hours=rng.choice([0, 8, -7])))
        tickets.append(t(i, opened.astimezone(zone), title=rng.choice(TITLES) % rng.randint(1, 30), category=rng.choice(CATEGORIES), subcategory=rng.choice([None, "Hardware", "Software"]),
                         priority=rng.choice([None, "P1", "P2", "P3"]), company=rng.choice([None, "A", "B", "C", "D"]), closed_at=done, resolved_at=resolved, resolution_due_at=goal,
                         first_response_due_at=response_due, first_response_at=response, source=rng.choice(["connectwise", "autotask"])))
    for i in range(rng.randint(0, 60)):
        ticket = rng.choice(tickets) if tickets and rng.random() < 0.8 else None
        times.append(e(i, None if ticket is None else (ticket.ticket_id if rng.random() < 0.9 else "9999"), base + timedelta(days=rng.randint(-3, 10), minutes=rng.randint(0, 1439)),
                       rng.randint(0, 20000), rng.choice(["Kim", "Lee", "Ola"]), source=ticket.source if ticket is not None else "connectwise"))
    for i in range(rng.randint(0, 40)):
        alerts.append(a(i, rng.randint(1, 8), source_type=rng.choice(["CPU", "DISK", "PING"]), source_name=rng.choice([None, "x", "y"]), device_name=rng.choice([None, "dn"])))
    for d in range(1, rng.randint(1, 10)):
        devices.append(DeviceRecord("ninjaone", d, "DEV-%d" % d, None, 1, rng.random() < 0.3))
    return tickets, times, alerts, devices


def o_norm(title):
    out, run = [], False
    for ch in title.lower():
        if ch in "0123456789":
            if not run:
                out.append("#")
            run = True
            continue
        run = False
        out.append(ch if ("a" <= ch <= "z" or ch == "#") else " ")
    text = " ".join("".join(out).split())
    return text or "(none)"


def o_drivers(tickets, ws, we):
    groups = {}
    for tk in tickets:
        if ws <= tk.opened_at.date() < we:
            groups.setdefault((tk.category, tk.subcategory or "(none)", o_norm(tk.title)), []).append(tk)
    rows = []
    for (cat, sub, title), ts in groups.items():
        per = {}
        for tk in ts:
            if tk.company is not None:
                per[tk.company] = per.get(tk.company, 0) + 1
        rep = sum(n - 1 for n in per.values())
        first, last = min(ts, key=lambda x: (x.opened_at, x.ticket_id)), max(ts, key=lambda x: (x.opened_at, x.ticket_id))
        rows.append(dict(category=cat, subcategory=sub, title=title, tickets=len(ts), companies=len(per), repeats=rep, repeat_percent=rep * 100 // len(ts),
                         first_opened=first.opened_at.strftime("%Y-%m-%dT%H:%M:%SZ"), last_opened=last.opened_at.strftime("%Y-%m-%dT%H:%M:%SZ"), example_ticket=first.ticket_id))
    rows.sort(key=lambda r: (-r["repeats"], -r["tickets"], r["category"], r["subcategory"], r["title"]))
    total = sum(len(v) for v in groups.values())
    cats = {}
    for tk in [x for v in groups.values() for x in v]:
        cats.setdefault((tk.category, tk.subcategory or "(none)"), []).append(tk)
    shares = []
    for (cat, sub), ts in cats.items():
        per = {}
        for tk in ts:
            if tk.company is not None:
                per[tk.company] = per.get(tk.company, 0) + 1
        shares.append(dict(category=cat, subcategory=sub, tickets=len(ts), share_percent=len(ts) * 100 // total, repeats=sum(n - 1 for n in per.values())))
    shares.sort(key=lambda r: (-r["tickets"], -r["repeats"], r["category"], r["subcategory"]))
    return total, rows, shares


def o_sla(tickets, as_of, ws, we, horizon):
    limit = as_of + timedelta(hours=horizon)
    groups, tot = {}, dict(open=0, breached=0, at_risk=0, ok=0, no_goal=0, response_overdue=0)
    for tk in tickets:
        finished = tk.resolved_at if tk.resolved_at is not None else tk.closed_at
        if tk.opened_at > as_of or (finished is not None and finished <= as_of):
            continue
        g = groups.setdefault((tk.category, tk.priority or "(none)"), dict(open=0, breached=0, at_risk=0, ok=0, no_goal=0, response_overdue=0, oldest=-1))
        if tk.resolution_due_at is None:
            state = "no_goal"
        elif tk.resolution_due_at < as_of:
            state = "breached"
        elif tk.resolution_due_at < limit:
            state = "at_risk"
        else:
            state = "ok"
        late_response = tk.first_response_due_at is not None and tk.first_response_due_at < as_of and not (tk.first_response_at is not None and tk.first_response_at <= as_of)
        for bucket in (g, tot):
            bucket["open"] += 1
            bucket[state] += 1
            bucket["response_overdue"] += 1 if late_response else 0
        g["oldest"] = max(g["oldest"], int((as_of - tk.opened_at).total_seconds() // 3600))
    rows = [dict(category=c, priority=p, open=g["open"], breached=g["breached"], at_risk=g["at_risk"], ok=g["ok"], no_goal=g["no_goal"], response_overdue=g["response_overdue"],
                 oldest_open_hours=g["oldest"]) for (c, p), g in groups.items()]
    rows.sort(key=lambda r: (-r["breached"], -r["at_risk"], -r["open"], r["category"], r["priority"]))
    flow, with_goal, late = {}, 0, 0
    for tk in tickets:
        f = flow.setdefault(tk.category, [0, 0])
        if ws <= tk.opened_at.date() < we:
            f[0] += 1
        finished = tk.resolved_at if tk.resolved_at is not None else tk.closed_at
        if finished is not None and ws <= finished.date() < we:
            f[1] += 1
            if tk.resolution_due_at is not None:
                with_goal += 1
                late += 1 if finished > tk.resolution_due_at else 0
    flows = [dict(category=c, opened=o, finished=f_, net=o - f_) for c, (o, f_) in flow.items() if o or f_]
    flows.sort(key=lambda r: (-r["net"], -r["opened"], r["category"]))
    return tot, rows, with_goal, late, flows


def o_touch(tickets, times, ws, we):
    cat = {(tk.source, tk.ticket_id): tk.category for tk in tickets}
    placed, non_ticket, unmatched, entries = {}, 0, 0, 0
    for en in times:
        if not (ws <= en.worked_on.date() < we):
            continue
        entries += 1
        if en.ticket_id is None:
            non_ticket += en.seconds
        elif (en.source, en.ticket_id) not in cat:
            unmatched += en.seconds
        else:
            c = placed.setdefault(cat[(en.source, en.ticket_id)], {"entries": 0, "per_ticket": {}, "techs": {}})
            c["entries"] += 1
            c["per_ticket"][en.ticket_id] = c["per_ticket"].get(en.ticket_id, 0) + en.seconds
            tc = c["techs"].setdefault(en.technician, {"seconds": 0, "tickets": set()})
            tc["seconds"] += en.seconds
            tc["tickets"].add(en.ticket_id)
    rows = []
    for name, c in placed.items():
        secs = sum(c["per_ticket"].values())
        top_ticket = sorted(c["per_ticket"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        techs = sorted(({"technician": k, "seconds": v["seconds"], "tickets": len(v["tickets"])} for k, v in c["techs"].items()), key=lambda r: (-r["seconds"], r["technician"]))
        rows.append(dict(category=name, entries=c["entries"], seconds=secs, tickets_touched=len(c["per_ticket"]), mean_seconds_per_ticket=secs // len(c["per_ticket"]),
                         most_time_ticket=top_ticket[0], most_time_seconds=top_ticket[1], technicians=techs))
    rows.sort(key=lambda r: (-r["seconds"], r["category"]))
    return entries, sum(r["seconds"] for r in rows), non_ticket, unmatched, rows


def o_alerts(alerts, devices):
    total = len(alerts)
    by_source, by_device = {}, {}
    for al in alerts:
        by_source.setdefault((al.source_type, al.source_name or "(none)"), []).append(al)
        by_device.setdefault(al.device_id, []).append(al)
    rows = []
    for (stype, sname), group in by_source.items():
        distinct = len(set(x.device_id for x in group))
        rows.append(dict(source_type=stype, source_name=sname, alerts=len(group), devices=distinct, repeats=len(group) - distinct, share_percent=len(group) * 100 // total))
    rows.sort(key=lambda r: (-r["alerts"], -r["repeats"], r["source_type"], r["source_name"]))
    listed = {d.device_id: d.name for d in devices}
    noisy = []
    for did, group in by_device.items():
        label = listed.get(did)
        if not label:
            label = next((x.device_name for x in group if x.device_name), None) or "device %d" % did
        noisy.append(dict(device_id=did, device_name=label, alerts=len(group), sources=len(set((x.source_type, x.source_name) for x in group))))
    noisy.sort(key=lambda r: (-r["alerts"], -r["sources"], r["device_id"]))
    return rows, noisy


@pytest.mark.parametrize("seed_value", range(60))
def test_seeded_worlds_match_the_brute_force_oracle_for_every_ranking(seed_value):
    rng = random.Random(seed_value)
    tickets, times, alerts, devices = random_world(rng)
    horizon, top = rng.choice([1, 4, 9]), 1000
    total, rows, shares = o_drivers(tickets, W0, W1)
    got = ticket_drivers(tickets, window_start=W0, window_end=W1, top=top)
    assert (got["opened_tickets"], got["driver_groups"], got["by_title"], got["by_category"]) == (total, len(rows), rows, shares), "drivers, seed %d" % seed_value
    tot, grows, with_goal, late, flows = o_sla(tickets, AS_OF, W0, W1, horizon)
    s = sla_risk(tickets, as_of=AS_OF, window_start=W0, window_end=W1, horizon_hours=horizon, top=top)
    assert {k: s[k] for k in tot} == tot and s["by_category_and_priority"] == grows and s["flow_by_category"] == flows, "sla, seed %d" % seed_value
    assert (s["finished_in_window_with_goal"], s["finished_late"], s["finished_late_percent"]) == (with_goal, late, None if with_goal == 0 else late * 100 // with_goal)
    srows, noisy = o_alerts(alerts, devices) if alerts else ([], [])
    ad = alert_drivers(alerts, devices, top=top)
    assert (ad["active_alerts"], ad["devices_with_alerts"], ad["devices"], ad["devices_offline"], ad["by_source"], ad["noisiest_devices"]) == (
        len(alerts), len(set(x.device_id for x in alerts)), len(devices), sum(1 for d in devices if d.offline), srows, noisy), "alerts, seed %d" % seed_value
    entries, placed, non_ticket, unmatched, trows = o_touch(tickets, times, W0, W1)
    tt = touch_time(tickets, times, window_start=W0, window_end=W1, top=top)
    assert (tt["entries_in_window"], tt["placed_seconds"], tt["non_ticket_seconds"], tt["unmatched_seconds"], tt["by_category"]) == (entries, placed, non_ticket, unmatched, trows), "touch, seed %d" % seed_value
    assert tt["total_seconds"] == placed + non_ticket + unmatched == sum(x.seconds for x in times if W0 <= x.worked_on.date() < W1), "every second is accounted for once"


@pytest.mark.parametrize("seed_value", range(25))
def test_the_rankings_do_not_depend_on_the_order_of_the_input_and_are_cut_from_the_full_ranking(seed_value):
    rng = random.Random(1000 + seed_value)
    tickets, times, alerts, devices = random_world(rng)
    shuffled = lambda xs: rng.sample(xs, len(xs))  # noqa: E731
    full = ticket_drivers(tickets, window_start=W0, window_end=W1, top=1000)
    assert ticket_drivers(shuffled(tickets), window_start=W0, window_end=W1, top=1000) == full
    assert ticket_drivers(tickets, window_start=W0, window_end=W1, top=3)["by_title"] == full["by_title"][:3]
    assert sla_risk(shuffled(tickets), as_of=AS_OF, window_start=W0, window_end=W1, top=1000) == sla_risk(tickets, as_of=AS_OF, window_start=W0, window_end=W1, top=1000)
    assert touch_time(shuffled(tickets), shuffled(times), window_start=W0, window_end=W1, top=1000) == touch_time(tickets, times, window_start=W0, window_end=W1, top=1000)
    assert alert_drivers(shuffled(alerts), shuffled(devices), top=1000) == alert_drivers(alerts, devices, top=1000)


@pytest.mark.parametrize("seed_value", range(25))
def test_a_ticket_opened_outside_the_window_changes_no_driver_and_one_opened_after_as_of_changes_no_risk(seed_value):
    rng = random.Random(2000 + seed_value)
    tickets, times, alerts, devices = random_world(rng)
    before = ticket_drivers(tickets, window_start=W0, window_end=W1, top=1000)
    noise = t(90001, datetime(2026, 9, 1, tzinfo=timezone.utc), title="Something else entirely 1", category="Zed")
    assert ticket_drivers([*tickets, noise], window_start=W0, window_end=W1, top=1000) == before
    risk = sla_risk(tickets, as_of=AS_OF, window_start=W0, window_end=W1, top=1000)
    future = t(90002, AS_OF + timedelta(seconds=1), resolution_due_at=AS_OF + timedelta(hours=1), category="Zed")
    after = sla_risk([*tickets, future], as_of=AS_OF, window_start=W0, window_end=W1, top=1000)
    assert {k: after[k] for k in ("open", "breached", "at_risk", "ok", "no_goal", "response_overdue", "by_category_and_priority")} == {k: risk[k] for k in ("open", "breached", "at_risk", "ok", "no_goal", "response_overdue", "by_category_and_priority")}


# ---------------------------------------------------------------- the report and its text

def test_a_report_holds_every_ranking_what_was_read_and_the_notes_for_the_sources_present():
    ledger = generate_world(3, as_of=AS_OF, tickets=60, devices=8, alerts=12)
    report = build_report(ledger, as_of=AS_OF, window_start=date(2026, 9, 10), window_end=date(2026, 10, 11))
    assert set(report) == {"as_of", "window", "inputs", "ticket_drivers", "alert_drivers", "sla_risk", "touch_time", "notes"}
    assert report["inputs"]["sources"] == ["autotask", "connectwise", "ninjaone"] and report["inputs"]["tickets"] == 60
    joined = " ".join(report["notes"])
    assert "ConnectWise:" in joined and "Autotask:" in joined and "NinjaOne:" in joined
    empty = build_report(Ledger(), as_of=AS_OF, window_start=W0, window_end=W1)
    assert empty["inputs"]["sources"] == [] and "ConnectWise:" not in " ".join(empty["notes"])
    json.dumps(report)                                                                            # every figure is plain data


def test_the_same_ledger_gives_the_same_report_every_time():
    ledger = generate_world(5, as_of=AS_OF, tickets=80)
    one = build_report(ledger, as_of=AS_OF, window_start=date(2026, 9, 10), window_end=date(2026, 10, 11))
    two = build_report(generate_world(5, as_of=AS_OF, tickets=80), as_of=AS_OF, window_start=date(2026, 9, 10), window_end=date(2026, 10, 11))
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)


def test_the_markdown_carries_the_reports_own_figures_and_says_what_it_does_not_see():
    ledger = Ledger()
    for tk in drivers_world():
        ledger.add(tk)
    ledger.add(e(1, 1, at(2), 5400))
    ledger.add(a(1, 1))
    text = render_markdown(build_report(ledger, as_of=AS_OF, window_start=W0, window_end=W1))
    for needle in ("# MSP backlog analysis", "## Ticket drivers", "## Alert drivers", "## SLA risk", "## Technician touch time", "## What this does not see",
                   "| Alerts | Storage | disk space low on srv # | 4 | 2 | 1 | 25 |", "1.50 hours in 1 entries", "5 tickets opened in the window", "CPU high"):
        assert needle in text, needle
    assert text.endswith("\n") and "\r" not in text


def test_the_markdown_for_an_empty_ledger_says_none_and_does_not_fail():
    text = render_markdown(build_report(Ledger(), as_of=AS_OF, window_start=W0, window_end=W1))
    assert "_none_" in text and "from nothing" in text and "0.00 hours in 0 entries" in text and "no ticket finished in the window had a goal" in text


def test_hours_are_shown_as_hours_and_hundredths_rounded_down():
    ledger = Ledger()
    ledger.add(t(1, at(2)))
    ledger.add(e(1, 1, at(2), 3599))
    ledger.add(e(2, 1, at(2), 1))
    assert "1.00 hours" in render_markdown(build_report(ledger, as_of=AS_OF, window_start=W0, window_end=W1))
    ledger2 = Ledger()
    ledger2.add(t(1, at(2)))
    ledger2.add(e(1, 1, at(2), 3599))
    assert "0.99 hours" in render_markdown(build_report(ledger2, as_of=AS_OF, window_start=W0, window_end=W1))
