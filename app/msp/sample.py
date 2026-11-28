"""A generated MSP world, for showing the backlog analysis without a client's data.

Everything here is made up from a seed: company names are "Company 01" and so on, technicians are "Technician A" and so on, titles come from a short list of the kinds of ticket an
MSP service desk sees all day. The same seed and `as_of` always give the same ledger, so a sample output in the docs can be regenerated and compared. The world is built to have the
shapes the analysis looks for (a few titles that repeat for the same company, two alert sources that are noisy, tickets near and past their goal, time that lands unevenly across
categories), not to look like any real database, and the connectors' tests do not use it.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from .ledger import Ledger
from .records import AlertRecord, DeviceRecord, TicketRecord, TimeRecord

__all__ = ["generate_world"]

#: (category, issue type, title template, weight): the kinds of ticket the generated desk gets, and how often.
KINDS = [
    ("Service Desk", "Password reset", "Password reset for user %d", 30),
    ("Service Desk", "Email", "Outlook keeps asking for the password (user %d)", 12),
    ("Service Desk", "Printing", "Printer offline at site %d", 14),
    ("Network", "Connectivity", "VPN not connecting for user %d", 10),
    ("RMM Alerts", "Storage", "Disk space low on SRV-%02d", 22),
    ("RMM Alerts", "Backup", "Backup failed on SRV-%02d", 9),
    ("Onboarding", "New user", "New user setup for employee %d", 6),
    ("Service Desk", "Hardware", "Laptop will not boot (asset %d)", 5),
]
#: (priority, resolution goal hours, first response goal hours, weight)
PRIORITIES = [("P1 Critical", 4, 1, 5), ("P2 High", 8, 2, 20), ("P3 Normal", 24, 4, 55), ("P4 Low", 72, 8, 20)]
ALERT_SOURCES = [("CONDITION_AGENT_DISK_FREE_SPACE", "Disk free space low", 40), ("AGENT_OFFLINE", "Agent offline", 25), ("CONDITION_AGENT_CPU", "CPU over 90 percent", 15),
                 ("CONDITION_AGENT_MEMORY", "Memory over 90 percent", 10), ("CONDITION_PING", "Ping lost", 10)]
TECHNICIANS = ["Technician %s" % c for c in "ABCDE"]


def _pick(rng: random.Random, rows: list[tuple], weight_index: int):
    return rng.choices(rows, weights=[r[weight_index] for r in rows])[0]


def generate_world(seed: int, *, as_of: datetime, days: int = 30, tickets: int = 400, companies: int = 12, devices: int = 30, alerts: int = 45) -> Ledger:
    """A ledger of generated tickets, time entries, devices and alerts over the `days` before `as_of`."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone aware")
    as_of = as_of.astimezone(timezone.utc)
    rng = random.Random(seed)
    ledger = Ledger()
    ledger.generated_at = as_of
    start = as_of - timedelta(days=days)
    for i in range(tickets):
        category, issue, template, _ = _pick(rng, KINDS, 3)
        priority, goal_hours, response_hours, _ = _pick(rng, PRIORITIES, 3)
        opened = start + timedelta(seconds=rng.randint(0, int((as_of - start).total_seconds()) - 60))
        company = rng.randint(1, companies) if rng.random() < 0.97 else None
        # a few companies have the same problem over and over, which is what makes a repeat
        if company is not None and rng.random() < 0.35:
            company = rng.choice([1, 2, 3])
        number = rng.randint(1, 60) if company is None else (company * 7 + rng.randint(0, 2))
        response_at = opened + timedelta(minutes=rng.randint(1, response_hours * 90)) if rng.random() < 0.9 else None
        resolved = closed = None
        if rng.random() < (0.97 if as_of - opened > timedelta(days=3) else 0.7):
            resolved = opened + timedelta(minutes=rng.randint(10, goal_hours * 75))
            if rng.random() < 0.8:
                closed = resolved + timedelta(minutes=rng.randint(0, 600))
        if resolved is not None and resolved > as_of:
            resolved = closed = None
        if closed is not None and closed > as_of:
            closed = None
        source = "connectwise" if rng.random() < 0.6 else "autotask"
        ledger.add(TicketRecord(
            source=source, ticket_id=str(1000 + i), title=template % number, category=category, subcategory=issue, priority=priority,
            status="Closed" if closed else "Resolved" if resolved else "In Progress", company=None if company is None else "Company %02d" % company, opened_at=opened,
            closed=closed is not None, closed_at=closed, resolved_at=resolved, first_response_at=response_at, first_response_due_at=opened + timedelta(hours=response_hours),
            resolution_due_at=opened + timedelta(hours=goal_hours) if rng.random() < 0.95 else None))
        for j in range(rng.choice([0, 1, 1, 2, 3, 4])):
            worked = opened + timedelta(minutes=rng.randint(0, 60 * goal_hours))
            if worked <= as_of:
                weight = 2 if category == "RMM Alerts" else 1                  # alert tickets take longer each, so categories differ in minutes per ticket
                ledger.add(TimeRecord(source=source, entry_id="%d-%d" % (1000 + i, j), ticket_id=str(1000 + i), technician=rng.choice(TECHNICIANS), worked_on=worked,
                                      seconds=300 * rng.randint(1, 8) * weight))
    for j in range(rng.randint(5, 12)):
        ledger.add(TimeRecord(source="connectwise", entry_id="internal-%d" % j, ticket_id=None, technician=rng.choice(TECHNICIANS),
                              worked_on=start + timedelta(hours=rng.randint(0, days * 24 - 1)), seconds=900 * rng.randint(1, 4)))
    for d in range(1, devices + 1):
        ledger.add(DeviceRecord(source="ninjaone", device_id=d, name="SRV-%02d" % d if d <= 12 else "WS-%02d" % d, node_class="WINDOWS_SERVER" if d <= 12 else "WINDOWS_WORKSTATION",
                                organization_id=1 + d % 4, offline=rng.random() < 0.12, last_contact=as_of - timedelta(minutes=rng.randint(1, 5000))))
    noisy_devices = [1, 2, 3, 4]
    for a in range(alerts):
        stype, sname, _ = _pick(rng, ALERT_SOURCES, 2)
        device = rng.choice(noisy_devices) if rng.random() < 0.55 else rng.randint(1, devices)
        ledger.add(AlertRecord(source="ninjaone", alert_id="alert-%03d" % a, device_id=device, device_name=None, organization_id=1 + device % 4, source_type=stype, source_name=sname,
                               subject=sname, severity=rng.choice(["MAJOR", "MODERATE", "MINOR"]), created_at=as_of - timedelta(minutes=rng.randint(5, days * 24 * 60))))
    return ledger
