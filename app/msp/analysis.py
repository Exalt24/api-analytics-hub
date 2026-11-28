"""The MSP backlog analysis: what an automation backlog is ranked by, worked out from the records the PSA and RMM connectors read.

An MSP deciding what to automate first looks at three things, and this module answers each from the ledger (`app/msp/ledger.py`) with plain integer arithmetic and a fixed order, so the
same ledger always gives the same report:

  1. DRIVERS. Which kinds of ticket repeat, and which alerts are noisy. `ticket_drivers` groups the tickets opened in a window by category, issue type and a normalised title and counts the
     REPEATS: for one company, every ticket of the same group after its first. `alert_drivers` groups the active alerts by their source and counts the alerts a device raised from the same
     source beyond its first.
  2. SLA RISK. Which open work will breach its goal. `sla_risk` takes the tickets that were open at `as_of` (not closed or resolved by then) and sorts each by its resolution goal: `breached` (the
     goal is before as_of), `at_risk` (it falls inside the horizon), `ok`, or `no_goal` (the vendor gave none, which is never called fine). It groups by category and priority, counts the
     first responses overdue, and reports how many tickets finished in the window did so after their goal.
  3. TOUCH TIME. How long technicians spend per kind of ticket. `touch_time` spreads the time entries of the window over the category of the ticket each was worked on, with the technicians
     under each category, and says how much time it could not place: time on no ticket, and time on a ticket the ledger does not hold.

A WINDOW is half open, [start, end), on UTC days. A ticket is `opened` in it when its opening instant falls on one of them. A percentage is a whole number rounded down.

WHAT THIS DOES NOT DO. It has no model of arrival rates or staffing, so "at risk" is a count of tickets near their goal, not a forecast; it ranks by counts and never weighs a priority above
another (a priority's order is not the same in ConnectWise and Autotask and is on no saved page); it does not read ticket text beyond the title. It describes the records it is given: a
record the connector could not read is not here (docs/MSP_SOURCES.md says what each connector leaves out).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from .ledger import Ledger
from .records import AlertRecord, DeviceRecord, TicketRecord, TimeRecord, hours_between, utc

__all__ = ["normalise_title", "ticket_drivers", "alert_drivers", "sla_risk", "touch_time", "build_report", "render_markdown", "AnalysisError", "DEFAULT_HORIZON_HOURS", "DEFAULT_TOP"]

DEFAULT_HORIZON_HOURS = 4
DEFAULT_TOP = 10
NONE_LABEL = "(none)"


class AnalysisError(ValueError):
    pass


def _check_window(start: date, end: date) -> None:
    if not isinstance(start, date) or not isinstance(end, date) or isinstance(start, datetime) or isinstance(end, datetime):
        raise AnalysisError("the window is two dates")
    if end <= start:
        raise AnalysisError("the window must hold at least one day")


def _check_top(top: int) -> None:
    if isinstance(top, bool) or not isinstance(top, int) or top < 1:
        raise AnalysisError("top must be a whole number of at least 1")


def _in_window(moment: datetime, start: date, end: date) -> bool:
    """Every record instant is already UTC (`records.utc`), so its date is its UTC day."""
    return start <= moment.date() < end


def normalise_title(title: str) -> str:
    """A title with what changes from one ticket to the next taken out: lower case, every run of digits one `#`, every other run of marks one space.

    "Disk space low on SRV-042" and "disk space low on srv-17" are the same driver.
    """
    text = re.sub(r"\d+", "#", title.lower())
    text = re.sub(r"[^a-z#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip() or NONE_LABEL


@dataclass(frozen=True)
class TicketDriver:
    category: str
    subcategory: str
    title: str
    tickets: int
    companies: int
    repeats: int
    repeat_percent: int
    first_opened: str
    last_opened: str
    example_ticket: str


@dataclass(frozen=True)
class CategoryShare:
    category: str
    subcategory: str
    tickets: int
    share_percent: int
    repeats: int


def _repeats(tickets: list[TicketRecord]) -> int:
    """Per company, every ticket after the first. A ticket with no company is not known to repeat anything."""
    per: dict[str, int] = {}
    for t in tickets:
        if t.company is not None:
            per[t.company] = per.get(t.company, 0) + 1
    return sum(n - 1 for n in per.values())


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ticket_drivers(tickets: Iterable[TicketRecord], *, window_start: date, window_end: date, top: int = DEFAULT_TOP) -> dict[str, Any]:
    """The tickets opened in the window, grouped two ways and ranked by repeats, then by count, then by name."""
    _check_window(window_start, window_end)
    _check_top(top)
    opened = [t for t in tickets if _in_window(t.opened_at, window_start, window_end)]
    by_title: dict[tuple[str, str, str], list[TicketRecord]] = {}
    by_category: dict[tuple[str, str], list[TicketRecord]] = {}
    for t in opened:
        sub = t.subcategory or NONE_LABEL
        by_title.setdefault((t.category, sub, normalise_title(t.title)), []).append(t)
        by_category.setdefault((t.category, sub), []).append(t)
    drivers = []
    for (category, sub, title), group in by_title.items():
        repeats = _repeats(group)
        ordered = sorted(group, key=lambda t: (t.opened_at, t.ticket_id))
        drivers.append(TicketDriver(category, sub, title, len(group), len({t.company for t in group if t.company is not None}), repeats, repeats * 100 // len(group),
                                    _iso(ordered[0].opened_at), _iso(ordered[-1].opened_at), ordered[0].ticket_id))
    drivers.sort(key=lambda d: (-d.repeats, -d.tickets, d.category, d.subcategory, d.title))
    shares = [CategoryShare(c, s, len(g), len(g) * 100 // len(opened), _repeats(g)) for (c, s), g in by_category.items()]
    shares.sort(key=lambda c: (-c.tickets, -c.repeats, c.category, c.subcategory))
    return {"opened_tickets": len(opened), "driver_groups": len(drivers), "by_title": [asdict(d) for d in drivers[:top]], "by_category": [asdict(c) for c in shares[:top]]}


@dataclass(frozen=True)
class AlertDriver:
    source_type: str
    source_name: str
    alerts: int
    devices: int
    repeats: int
    share_percent: int


@dataclass(frozen=True)
class NoisyDevice:
    device_id: int
    device_name: str
    alerts: int
    sources: int


def alert_drivers(alerts: Iterable[AlertRecord], devices: Iterable[DeviceRecord] = (), *, top: int = DEFAULT_TOP) -> dict[str, Any]:
    """The active alerts grouped by source and ranked by count. `repeats` is the alerts beyond the first that one device raised from the same source."""
    _check_top(top)
    alerts = list(alerts)
    devices = list(devices)
    by_source: dict[tuple[str, str], list[AlertRecord]] = {}
    by_device: dict[int, list[AlertRecord]] = {}
    for a in alerts:
        by_source.setdefault((a.source_type, a.source_name or NONE_LABEL), []).append(a)
        by_device.setdefault(a.device_id, []).append(a)
    drivers = []
    for (stype, sname), group in by_source.items():
        distinct = len({a.device_id for a in group})
        drivers.append(AlertDriver(stype, sname, len(group), distinct, len(group) - distinct, len(group) * 100 // len(alerts)))
    drivers.sort(key=lambda d: (-d.alerts, -d.repeats, d.source_type, d.source_name))
    names = {d.device_id: d.name for d in devices}
    noisy = []
    for did, group in by_device.items():
        label = names.get(did) or next((a.device_name for a in group if a.device_name), None) or "device %d" % did
        noisy.append(NoisyDevice(did, label, len(group), len({(a.source_type, a.source_name) for a in group})))
    noisy.sort(key=lambda d: (-d.alerts, -d.sources, d.device_id))
    return {"active_alerts": len(alerts), "source_groups": len(drivers), "devices_with_alerts": len(by_device), "devices": len(devices), "devices_offline": sum(1 for d in devices if d.offline),
            "by_source": [asdict(d) for d in drivers[:top]], "noisiest_devices": [asdict(d) for d in noisy[:top]]}


@dataclass(frozen=True)
class SlaGroup:
    category: str
    priority: str
    open: int
    breached: int
    at_risk: int
    ok: int
    no_goal: int
    response_overdue: int
    oldest_open_hours: int


@dataclass(frozen=True)
class Flow:
    category: str
    opened: int
    finished: int
    net: int


def _open_at(t: TicketRecord, as_of: datetime) -> bool:
    done = t.finished_at
    return t.opened_at <= as_of and (done is None or done > as_of)


def sla_risk(tickets: Iterable[TicketRecord], *, as_of: datetime, window_start: date, window_end: date, horizon_hours: int = DEFAULT_HORIZON_HOURS, top: int = DEFAULT_TOP) -> dict[str, Any]:
    """The tickets open at `as_of` sorted by their resolution goal, grouped by category and priority and ranked by breached, then at risk, then open."""
    as_of = utc(as_of, "as_of")
    _check_window(window_start, window_end)
    _check_top(top)
    if isinstance(horizon_hours, bool) or not isinstance(horizon_hours, int) or horizon_hours < 1:
        raise AnalysisError("horizon_hours must be a whole number of at least 1")
    tickets = list(tickets)
    horizon = as_of + timedelta(hours=horizon_hours)
    groups: dict[tuple[str, str], dict[str, int]] = {}
    totals = {"open": 0, "breached": 0, "at_risk": 0, "ok": 0, "no_goal": 0, "response_overdue": 0}
    for t in tickets:
        if not _open_at(t, as_of):
            continue
        g = groups.setdefault((t.category, t.priority or NONE_LABEL), {"open": 0, "breached": 0, "at_risk": 0, "ok": 0, "no_goal": 0, "response_overdue": 0, "oldest": -1})
        due = t.resolution_due_at
        state = "no_goal" if due is None else "breached" if due < as_of else "at_risk" if due < horizon else "ok"
        g["open"] += 1
        g[state] += 1
        totals["open"] += 1
        totals[state] += 1
        answered = t.first_response_at is not None and t.first_response_at <= as_of
        if not answered and t.first_response_due_at is not None and t.first_response_due_at < as_of:
            g["response_overdue"] += 1
            totals["response_overdue"] += 1
        g["oldest"] = max(g["oldest"], hours_between(t.opened_at, as_of))
    rows = [SlaGroup(c, p, g["open"], g["breached"], g["at_risk"], g["ok"], g["no_goal"], g["response_overdue"], g["oldest"]) for (c, p), g in groups.items()]
    rows.sort(key=lambda r: (-r.breached, -r.at_risk, -r.open, r.category, r.priority))
    flow: dict[str, list[int]] = {}
    finished_with_goal = finished_late = 0
    for t in tickets:
        row = flow.setdefault(t.category, [0, 0])
        if _in_window(t.opened_at, window_start, window_end):
            row[0] += 1
        done = t.finished_at
        if done is not None and _in_window(done, window_start, window_end):
            row[1] += 1
            if t.resolution_due_at is not None:
                finished_with_goal += 1
                finished_late += 1 if done > t.resolution_due_at else 0
    flows = [Flow(c, o, f, o - f) for c, (o, f) in flow.items() if o or f]
    flows.sort(key=lambda f: (-f.net, -f.opened, f.category))
    return {"as_of": _iso(as_of), "horizon_hours": horizon_hours, **totals, "likely_breaches": totals["breached"] + totals["at_risk"],
            "by_category_and_priority": [asdict(r) for r in rows[:top]], "groups": len(rows),
            "finished_in_window_with_goal": finished_with_goal, "finished_late": finished_late,
            "finished_late_percent": None if finished_with_goal == 0 else finished_late * 100 // finished_with_goal,
            "flow_by_category": [asdict(f) for f in flows[:top]]}


@dataclass(frozen=True)
class TechnicianTouch:
    technician: str
    seconds: int
    tickets: int


@dataclass(frozen=True)
class CategoryTouch:
    category: str
    entries: int
    seconds: int
    tickets_touched: int
    mean_seconds_per_ticket: int
    most_time_ticket: str
    most_time_seconds: int
    technicians: list[dict[str, Any]]


def touch_time(tickets: Iterable[TicketRecord], times: Iterable[TimeRecord], *, window_start: date, window_end: date, top: int = DEFAULT_TOP) -> dict[str, Any]:
    """The window's time entries spread over the category of the ticket each was worked on, with the technicians under each category."""
    _check_window(window_start, window_end)
    _check_top(top)
    category_of = {(t.source, t.ticket_id): t.category for t in tickets}
    cats: dict[str, dict[str, Any]] = {}
    in_window = [e for e in times if _in_window(e.worked_on, window_start, window_end)]
    non_ticket = unmatched = 0
    for e in in_window:
        if e.ticket_id is None:
            non_ticket += e.seconds
            continue
        category = category_of.get((e.source, e.ticket_id))
        if category is None:
            unmatched += e.seconds
            continue
        c = cats.setdefault(category, {"entries": 0, "seconds": 0, "tickets": {}, "techs": {}})
        c["entries"] += 1
        c["seconds"] += e.seconds
        c["tickets"][e.ticket_id] = c["tickets"].get(e.ticket_id, 0) + e.seconds
        tech = c["techs"].setdefault(e.technician, {"seconds": 0, "tickets": set()})
        tech["seconds"] += e.seconds
        tech["tickets"].add(e.ticket_id)
    rows = []
    for category, c in cats.items():
        most = sorted(c["tickets"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        techs = sorted((TechnicianTouch(name, v["seconds"], len(v["tickets"])) for name, v in c["techs"].items()), key=lambda t: (-t.seconds, t.technician))
        rows.append(CategoryTouch(category, c["entries"], c["seconds"], len(c["tickets"]), c["seconds"] // len(c["tickets"]), most[0], most[1], [asdict(t) for t in techs[:top]]))
    rows.sort(key=lambda r: (-r.seconds, r.category))
    placed = sum(r.seconds for r in rows)
    return {"entries_in_window": len(in_window), "total_seconds": placed + non_ticket + unmatched, "placed_seconds": placed, "non_ticket_seconds": non_ticket, "unmatched_seconds": unmatched,
            "categories": len(rows), "by_category": [asdict(r) for r in rows[:top]]}


def build_report(ledger: Ledger, *, as_of: datetime, window_start: date, window_end: date, horizon_hours: int = DEFAULT_HORIZON_HOURS, top: int = DEFAULT_TOP) -> dict[str, Any]:
    """The three analyses over one ledger, with what was read and what the analysis cannot see."""
    sources = sorted({r.source for r in (*ledger.tickets, *ledger.times, *ledger.alerts, *ledger.devices)})
    notes = ["Ticket drivers count the tickets opened in the window; repeats are counted per company, so a ticket with no company is never a repeat.",
             "At risk is a count of open tickets whose goal falls inside the horizon, not a forecast; priorities are not ranked against each other.",
             "Time that cannot be placed on a ticket in the ledger is reported as non_ticket_seconds and unmatched_seconds, never spread over the categories."]
    if "connectwise" in sources:
        notes.append("ConnectWise: the goals of an open ticket are what the ticket list gave, which its schema says may not be current unless the sync used refresh_open_sla.")
    if "autotask" in sources:
        notes.append("Autotask: a ticket is closed when it has a completed date, and the resolution goal is the field the connector was told to read.")
    if "ninjaone" in sources:
        notes.append("NinjaOne: only ACTIVE alerts are listed, so an alert reset before the sync is not here and the noisy sources describe what is still open.")
    return {"as_of": _iso(utc(as_of, "as_of")), "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
            "inputs": {"sources": sources, "tickets": len(ledger.tickets), "time_entries": len(ledger.times), "alerts": len(ledger.alerts), "devices": len(ledger.devices)},
            "ticket_drivers": ticket_drivers(ledger.tickets, window_start=window_start, window_end=window_end, top=top),
            "alert_drivers": alert_drivers(ledger.alerts, ledger.devices, top=top),
            "sla_risk": sla_risk(ledger.tickets, as_of=as_of, window_start=window_start, window_end=window_end, horizon_hours=horizon_hours, top=top),
            "touch_time": touch_time(ledger.tickets, ledger.times, window_start=window_start, window_end=window_end, top=top),
            "notes": notes}


def _hours(seconds: int) -> str:
    return "%d.%02d" % (seconds // 3600, seconds % 3600 * 100 // 3600)


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    if not rows:
        return ["_none_", ""]
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out + [""]


def render_markdown(report: dict[str, Any]) -> str:
    """The report as readable text. Every figure is the report's own; nothing is recomputed."""
    w, inputs = report["window"], report["inputs"]
    lines = ["# MSP backlog analysis", "",
             "As of %s, window %s to %s (end not included). Read: %d tickets, %d time entries, %d alerts, %d devices, from %s." % (
                 report["as_of"], w["start"], w["end"], inputs["tickets"], inputs["time_entries"], inputs["alerts"], inputs["devices"], ", ".join(inputs["sources"]) or "nothing"), ""]
    td = report["ticket_drivers"]
    lines += ["## Ticket drivers", "", "%d tickets opened in the window, in %d driver groups. Repeats are tickets after the first for the same company." % (td["opened_tickets"], td["driver_groups"]), ""]
    lines += _table(["Category", "Issue type", "Title", "Tickets", "Companies", "Repeats", "Repeat %"], [[d["category"], d["subcategory"], d["title"], d["tickets"], d["companies"], d["repeats"], d["repeat_percent"]] for d in td["by_title"]])
    lines += ["By category", ""] + _table(["Category", "Issue type", "Tickets", "Share %", "Repeats"], [[c["category"], c["subcategory"], c["tickets"], c["share_percent"], c["repeats"]] for c in td["by_category"]])
    ad = report["alert_drivers"]
    lines += ["## Alert drivers", "", "%d active alerts on %d devices (%d devices listed, %d offline)." % (ad["active_alerts"], ad["devices_with_alerts"], ad["devices"], ad["devices_offline"]), ""]
    lines += _table(["Source type", "Source", "Alerts", "Devices", "Repeats", "Share %"], [[d["source_type"], d["source_name"], d["alerts"], d["devices"], d["repeats"], d["share_percent"]] for d in ad["by_source"]])
    lines += ["Noisiest devices", ""] + _table(["Device", "Alerts", "Sources"], [[d["device_name"], d["alerts"], d["sources"]] for d in ad["noisiest_devices"]])
    sr = report["sla_risk"]
    lines += ["## SLA risk", "", "%d open tickets at %s: %d breached, %d at risk within %d hours, %d fine, %d with no goal; %d first responses overdue." % (
        sr["open"], sr["as_of"], sr["breached"], sr["at_risk"], sr["horizon_hours"], sr["ok"], sr["no_goal"], sr["response_overdue"]), ""]
    lines += _table(["Category", "Priority", "Open", "Breached", "At risk", "OK", "No goal", "Response overdue", "Oldest (hours)"],
                    [[g["category"], g["priority"], g["open"], g["breached"], g["at_risk"], g["ok"], g["no_goal"], g["response_overdue"], g["oldest_open_hours"]] for g in sr["by_category_and_priority"]])
    late = "no ticket finished in the window had a goal" if sr["finished_late_percent"] is None else "%d of %d tickets finished in the window with a goal finished late (%d%%)" % (
        sr["finished_late"], sr["finished_in_window_with_goal"], sr["finished_late_percent"])
    lines += ["Flow in the window: " + late + ".", ""] + _table(["Category", "Opened", "Finished", "Net"], [[f["category"], f["opened"], f["finished"], f["net"]] for f in sr["flow_by_category"]])
    tt = report["touch_time"]
    lines += ["## Technician touch time", "", "%s hours in %d entries in the window: %s placed on %d categories, %s on no ticket, %s on a ticket not in the ledger." % (
        _hours(tt["total_seconds"]), tt["entries_in_window"], _hours(tt["placed_seconds"]), tt["categories"], _hours(tt["non_ticket_seconds"]), _hours(tt["unmatched_seconds"])), ""]
    lines += _table(["Category", "Hours", "Entries", "Tickets touched", "Mean hours per ticket", "Most time (ticket, hours)", "Technicians (hours)"],
                    [[c["category"], _hours(c["seconds"]), c["entries"], c["tickets_touched"], _hours(c["mean_seconds_per_ticket"]), "%s, %s" % (c["most_time_ticket"], _hours(c["most_time_seconds"])),
                      "; ".join("%s %s" % (t["technician"], _hours(t["seconds"])) for t in c["technicians"])] for c in tt["by_category"]])
    lines += ["## What this does not see", ""] + ["- " + n for n in report["notes"]]
    return "\n".join(lines) + "\n"
