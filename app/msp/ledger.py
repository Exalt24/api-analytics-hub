"""The ledger: the records a sync read, written to one JSON lines file so the analysis can run later, offline and repeatably.

The first line is a header (`{"ledger": "msp-ledger", "version": 1, ...}`); every other line is one record with a `kind` of ticket, time, alert or device. Datetimes are
ISO 8601 in UTC with a trailing Z. `read_ledger` refuses a line it cannot parse, an unknown kind, an unknown or missing field, a duplicate record, a future version or a
header count that does not match, naming the line number, so a damaged file fails loudly instead of quietly shrinking the analysis.

Nothing is stored beyond a ticket title, a company name, a technician label and a device name: no credential and no ticket body.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .records import AlertRecord, DeviceRecord, RecordError, TicketRecord, TimeRecord

__all__ = ["write_ledger", "read_ledger", "Ledger", "LedgerError", "VERSION"]

VERSION = 1
_KINDS = {"ticket": TicketRecord, "time": TimeRecord, "alert": AlertRecord, "device": DeviceRecord}
_ATTR = {"ticket": "tickets", "time": "times", "alert": "alerts", "device": "devices"}
_ID = {"ticket": "ticket_id", "time": "entry_id", "alert": "alert_id", "device": "device_id"}


class LedgerError(ValueError):
    pass


class Ledger:
    def __init__(self) -> None:
        self.tickets: list[TicketRecord] = []
        self.times: list[TimeRecord] = []
        self.alerts: list[AlertRecord] = []
        self.devices: list[DeviceRecord] = []

    def add(self, record: object) -> None:
        for kind, cls in _KINDS.items():
            if isinstance(record, cls):
                getattr(self, _ATTR[kind]).append(record)
                return
        raise LedgerError("not a ledger record: %s" % type(record).__name__)


def _iso(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if value.microsecond else value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise LedgerError("a datetime must be UTC with a trailing Z, got %r" % (value,))
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise LedgerError("not a datetime: %r" % (value,)) from None


def _kind_of(record: object) -> str:
    for kind, cls in _KINDS.items():
        if isinstance(record, cls):
            return kind
    raise LedgerError("not a ledger record: %s" % type(record).__name__)


def _dump(record: object) -> dict:
    out: dict = {"kind": _kind_of(record)}
    for key, value in dataclasses.asdict(record).items():  # type: ignore[call-overload]
        out[key] = _iso(value) if isinstance(value, datetime) else value
    return out


def write_ledger(path: str | Path, records: Iterable[object], *, generated_at: datetime) -> int:
    """Write the records sorted, so the same records always give the same bytes. A record that appears twice is refused. Returns how many were written."""
    rows = sorted((_dump(r) for r in records), key=lambda r: (r["kind"], str(r["source"]), str(r[_ID[r["kind"]]])))
    seen: set[tuple] = set()
    for r in rows:
        key = (r["kind"], r["source"], r[_ID[r["kind"]]])
        if key in seen:
            raise LedgerError("%s %s appears twice" % (r["kind"], key[1:]))
        seen.add(key)
    header = {"ledger": "msp-ledger", "version": VERSION, "generated_at": _iso(generated_at), "records": len(rows)}
    text = "\n".join(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in [header, *rows]) + "\n"
    Path(path).write_text(text, encoding="utf-8", newline="\n")
    return len(rows)


def read_ledger(path: str | Path) -> Ledger:
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise LedgerError("the ledger is empty")
    try:
        header = json.loads(lines[0])
    except ValueError:
        raise LedgerError("line 1 is not a ledger header") from None
    if not isinstance(header, dict) or header.get("ledger") != "msp-ledger":
        raise LedgerError("line 1 is not a ledger header")
    if header.get("version") != VERSION:
        raise LedgerError("ledger version %r is not the version this reads (%d)" % (header.get("version"), VERSION))
    ledger = Ledger()
    seen: set[tuple] = set()
    for number, line in enumerate(lines[1:], start=2):
        try:
            data = json.loads(line)
        except ValueError:
            raise LedgerError("line %d is not JSON" % number) from None
        if not isinstance(data, dict) or data.get("kind") not in _KINDS:
            raise LedgerError("line %d has no known kind" % number)
        kind = data.pop("kind")
        cls = _KINDS[kind]
        fields = dataclasses.fields(cls)
        names = {f.name for f in fields}
        extra = sorted(set(data) - names)
        if extra:
            raise LedgerError("line %d has fields a %s does not have: %s" % (number, kind, extra))
        missing = [f.name for f in fields if f.name not in data and f.default is dataclasses.MISSING]
        if missing:
            raise LedgerError("line %d is missing %s" % (number, missing))
        for key, value in list(data.items()):
            if value is not None and (key.endswith("_at") or key in ("worked_on", "last_contact")):
                data[key] = _parse(value)
        try:
            record = cls(**data)
        except (RecordError, TypeError) as exc:
            raise LedgerError("line %d: %s" % (number, exc)) from None
        ident = (kind, record.source, getattr(record, _ID[kind]))  # type: ignore[attr-defined]
        if ident in seen:
            raise LedgerError("line %d repeats %s %s" % (number, kind, ident[1:]))
        seen.add(ident)
        ledger.add(record)
    if header.get("records") != len(lines) - 1:
        raise LedgerError("the header says %r records and %d follow" % (header.get("records"), len(lines) - 1))
    return ledger
