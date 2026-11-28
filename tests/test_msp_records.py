"""The MSP records and the ledger file they are stored in: what they refuse, and that what is written is what is read back.

Every refusal runs a positive control first (the same record without the defect is accepted).
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.msp.ledger import Ledger, LedgerError, read_ledger, write_ledger  # noqa: E402
from app.msp.records import AlertRecord, DeviceRecord, RecordError, TicketRecord, TimeRecord, hours_between  # noqa: E402

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def ticket(**kw):
    base = dict(source="connectwise", ticket_id="1", title="Printer is offline", category="Service Board", subcategory="Incident", priority="Priority 3", status="New",
                company="Acme Co", opened_at=T0, closed=False)
    base.update(kw)
    return TicketRecord(**base)


def timerec(**kw):
    base = dict(source="connectwise", entry_id="1", ticket_id="1", technician="Kim Allen", worked_on=T0, seconds=3600)
    base.update(kw)
    return TimeRecord(**base)


def alert(**kw):
    base = dict(source="ninjaone", alert_id="a1", device_id=5, device_name="SRV-01", organization_id=2, source_type="CONDITION_AGENT_CPU", source_name="CPU high",
                subject="CPU over 90", severity="MAJOR", created_at=T0)
    base.update(kw)
    return AlertRecord(**base)


def device(**kw):
    base = dict(source="ninjaone", device_id=5, name="SRV-01", node_class="WINDOWS_SERVER", organization_id=2, offline=False, last_contact=T0)
    base.update(kw)
    return DeviceRecord(**base)


def test_a_good_record_of_each_kind_is_accepted():
    assert ticket().is_open and timerec().seconds == 3600 and alert().device_id == 5 and device().offline is False


@pytest.mark.parametrize("kw, match", [
    ({"opened_at": datetime(2026, 10, 1, 9)}, "timezone aware"),
    ({"opened_at": "2026-10-01"}, "timezone aware"),
    ({"closed_at": T0 + timedelta(hours=1)}, "closed date is closed"),
    ({"closed": True, "closed_at": T0 - timedelta(hours=1)}, "closed before it opened"),
    ({"resolved_at": T0 - timedelta(hours=1)}, "resolved before it opened"),
    ({"title": "  "}, "title must be a non empty string"),
    ({"category": None}, "category must be a non empty string"),
    ({"ticket_id": ""}, "ticket_id must be a non empty string"),
    ({"subcategory": ""}, "subcategory must be a non empty string"),
    ({"closed": "yes"}, "closed must be true or false"),
    ({"resolution_due_at": datetime(2026, 10, 2)}, "timezone aware"),
])
def test_a_ticket_record_refuses_each_defect_for_its_own_reason(kw, match):
    ticket()
    with pytest.raises(RecordError, match=match):
        ticket(**kw)


def test_an_instant_is_stored_as_the_same_instant_in_utc():
    manila = timezone(timedelta(hours=8))
    t = ticket(opened_at=datetime(2026, 10, 1, 17, 0, tzinfo=manila))
    assert t.opened_at == T0 and t.opened_at.utcoffset() == timedelta(0)


def test_open_means_neither_closed_nor_resolved_and_finished_prefers_resolved():
    assert ticket().is_open and ticket().finished_at is None
    assert not ticket(resolved_at=T0 + timedelta(hours=1)).is_open
    assert not ticket(closed=True, closed_at=T0 + timedelta(hours=2)).is_open
    both = ticket(closed=True, closed_at=T0 + timedelta(hours=5), resolved_at=T0 + timedelta(hours=2))
    assert both.finished_at == T0 + timedelta(hours=2)
    assert ticket(closed=True).finished_at is None, "closed with no date: nothing to say it was finished late"


@pytest.mark.parametrize("kw, match", [
    ({"seconds": -1}, "whole number of at least 0"),
    ({"seconds": 1.5}, "whole number of at least 0"),
    ({"seconds": True}, "whole number of at least 0"),
    ({"technician": ""}, "technician must be"),
    ({"worked_on": datetime(2026, 10, 1)}, "timezone aware"),
])
def test_a_time_record_refuses_each_defect(kw, match):
    timerec(seconds=0)
    with pytest.raises(RecordError, match=match):
        timerec(**kw)


@pytest.mark.parametrize("kw, match", [({"device_id": True}, "whole number"), ({"device_id": "5"}, "whole number"), ({"source_type": ""}, "source_type"),
                                       ({"created_at": datetime(2026, 10, 1)}, "timezone aware")])
def test_an_alert_record_refuses_each_defect(kw, match):
    alert()
    with pytest.raises(RecordError, match=match):
        alert(**kw)


@pytest.mark.parametrize("kw, match", [({"offline": "no"}, "true or false"), ({"device_id": 1.5}, "whole number"), ({"name": ""}, "name must be")])
def test_a_device_record_refuses_each_defect(kw, match):
    device()
    with pytest.raises(RecordError, match=match):
        device(**kw)


def test_hours_between_rounds_down_and_is_negative_backwards():
    assert hours_between(T0, T0 + timedelta(hours=3, minutes=59)) == 3 and hours_between(T0 + timedelta(hours=1), T0) == -1 and hours_between(T0, T0 + timedelta(minutes=59)) == 0


# ---------------------------------------------------------------- the ledger

def sample():
    return [ticket(ticket_id="2", closed=True, closed_at=T0 + timedelta(hours=4), resolved_at=T0 + timedelta(hours=3), first_response_at=T0 + timedelta(minutes=5),
                   first_response_due_at=T0 + timedelta(hours=1), resolution_due_at=T0 + timedelta(hours=8)),
            ticket(ticket_id="1", subcategory=None, priority=None, company=None), timerec(entry_id="9", ticket_id=None), timerec(entry_id="3"), alert(), device(offline=True, last_contact=None)]


def test_a_ledger_round_trips_every_record_and_writes_the_same_bytes_in_any_order(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    recs = sample()
    assert write_ledger(a, recs, generated_at=T0) == 6
    write_ledger(b, list(reversed(recs)), generated_at=T0)
    assert a.read_bytes() == b.read_bytes(), "records are sorted, so the file does not depend on the order they were read in"
    got = read_ledger(a)
    assert sorted(got.tickets, key=lambda t: t.ticket_id) == sorted([r for r in recs if isinstance(r, TicketRecord)], key=lambda t: t.ticket_id)
    assert sorted(got.times, key=lambda t: t.entry_id) == sorted([r for r in recs if isinstance(r, TimeRecord)], key=lambda t: t.entry_id)
    assert got.alerts == [alert()] and got.devices == [device(offline=True, last_contact=None)]


def test_microseconds_survive_to_the_millisecond_and_dates_are_written_in_utc(tmp_path):
    manila = timezone(timedelta(hours=8))
    p = tmp_path / "l.jsonl"
    write_ledger(p, [ticket(opened_at=datetime(2026, 10, 1, 17, 0, 0, 123000, tzinfo=manila))], generated_at=T0)
    assert '"opened_at": "2026-10-01T09:00:00.123Z"' in p.read_text(encoding="utf-8")
    assert read_ledger(p).tickets[0].opened_at == datetime(2026, 10, 1, 9, 0, 0, 123000, tzinfo=timezone.utc)


def test_a_random_world_of_records_round_trips(tmp_path):
    rng = random.Random(7)
    recs = []
    for i in range(60):
        opened = T0 + timedelta(minutes=rng.randint(0, 100000))
        closed = rng.random() < 0.5
        recs.append(ticket(ticket_id=str(i), opened_at=opened, closed=closed, closed_at=opened + timedelta(minutes=rng.randint(0, 5000)) if closed else None,
                           title=rng.choice(["Disk space low on SRV-1", "Password reset", "  Email é ticket  "]), priority=rng.choice([None, "P1", "P2"])))
        recs.append(timerec(entry_id=str(i), ticket_id=rng.choice([None, str(i)]), seconds=rng.randint(0, 86400)))
    p = tmp_path / "w.jsonl"
    write_ledger(p, recs, generated_at=T0)
    got = read_ledger(p)
    assert len(got.tickets) == 60 and len(got.times) == 60
    assert {t.ticket_id: t for t in got.tickets} == {r.ticket_id: r for r in recs if isinstance(r, TicketRecord)}


def test_a_record_appearing_twice_is_refused_on_write(tmp_path):
    write_ledger(tmp_path / "ok.jsonl", [ticket(), ticket(ticket_id="2")], generated_at=T0)
    with pytest.raises(LedgerError, match="appears twice"):
        write_ledger(tmp_path / "bad.jsonl", [ticket(), ticket()], generated_at=T0)
    write_ledger(tmp_path / "ok2.jsonl", [ticket(source="connectwise"), ticket(source="autotask")], generated_at=T0)    # the same id from two vendors is two tickets


def test_something_that_is_not_a_record_is_refused_on_write(tmp_path):
    with pytest.raises(LedgerError, match="not a ledger record"):
        write_ledger(tmp_path / "x.jsonl", [ticket(), "text"], generated_at=T0)
    with pytest.raises(LedgerError):
        Ledger().add(object())


def _tamper(tmp_path, edit):
    p = tmp_path / "t.jsonl"
    write_ledger(p, sample(), generated_at=T0)
    lines = p.read_text(encoding="utf-8").split("\n")
    edit(lines)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _ticket_line(lines):
    return next(i for i, line in enumerate(lines) if '"kind": "ticket"' in line)


def _edit_ticket(**changes):
    def edit(lines):
        i = _ticket_line(lines)
        row = json.loads(lines[i])
        row.update(changes)
        lines[i] = json.dumps(row)
    return edit


def _drop_ticket_field(name):
    def edit(lines):
        i = _ticket_line(lines)
        lines[i] = json.dumps({k: v for k, v in json.loads(lines[i]).items() if k != name})
    return edit


def _put(lines, i, text):
    lines[i] = text


def test_a_damaged_ledger_is_refused_naming_the_line(tmp_path):
    read_ledger(_tamper(tmp_path, lambda lines: None))                                         # positive control: untouched
    cases = {
        "not JSON": (lambda L: _put(L, 2, "{broken"), "line 3 is not JSON"),
        "no kind": (lambda L: _put(L, 2, json.dumps({"x": 1})), "line 3 has no known kind"),
        "unknown kind": (lambda L: _put(L, 2, json.dumps({"kind": "invoice"})), "no known kind"),
        "extra field": (_edit_ticket(bonus=1), "has fields a"),
        "missing field": (_drop_ticket_field("title"), "is missing"),
        "naive date": (_edit_ticket(opened_at="2026-10-01T09:00:00"), "trailing Z"),
        "not a date": (_edit_ticket(opened_at="yesterdayZ"), "not a datetime"),
        "non text date": (_edit_ticket(opened_at=5), "trailing Z"),
        "bad record": (_edit_ticket(title=""), "title must be a non empty string"),
        "duplicate": (lambda L: L.insert(2, L[1]), "repeats"),
        "wrong count": (lambda L: L.pop(3), "header says"),
        "future version": (lambda L: _put(L, 0, json.dumps({**json.loads(L[0]), "version": 2})), "ledger version"),
        "not a header": (lambda L: _put(L, 0, json.dumps({"hello": 1})), "not a ledger header"),
        "header not JSON": (lambda L: _put(L, 0, "zzz"), "not a ledger header"),
    }
    for label, (edit, match) in cases.items():
        with pytest.raises(LedgerError, match=match):
            read_ledger(_tamper(tmp_path, edit))
    empty = tmp_path / "e.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(LedgerError, match="empty"):
        read_ledger(empty)
