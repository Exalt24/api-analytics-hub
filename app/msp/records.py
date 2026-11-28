"""The records the MSP connectors hand to the analysis step, in one vocabulary.

ConnectWise Manage and Autotask both describe a service ticket, and both describe time a technician spent. NinjaOne describes a device and the alerts raised on it. Each
connector translates its vendor's field names into these dataclasses (that is the whole of its job, as in `app/connectors/base.py`), so `analysis.py` never sees a vendor word.

Every instant is a timezone aware UTC `datetime`. A record that cannot be built (a date that is not a date, a negative duration) raises `RecordError`, never a default: an
analysis over invented values is worse than none.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

__all__ = ["RecordError", "TicketRecord", "TimeRecord", "AlertRecord", "DeviceRecord", "utc", "hours_between"]


class RecordError(ValueError):
    pass


def utc(value: datetime, what: str) -> datetime:
    """The same instant as an aware UTC datetime. A naive datetime is refused: its zone is a guess."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RecordError("%s must be a timezone aware datetime" % what)
    return value.astimezone(timezone.utc)


def _text(value: object, what: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RecordError("%s must be a non empty string" % what)
    return value.strip()


def hours_between(earlier: datetime, later: datetime) -> int:
    """Whole hours from `earlier` to `later`, rounded down. Negative when `earlier` is after `later`."""
    return int((later - earlier) // timedelta(hours=1))


@dataclass(frozen=True)
class TicketRecord:
    """One service ticket. `category` is the board (ConnectWise) or the ticket category (Autotask); `subcategory` the type (ConnectWise) or the issue type (Autotask).

    `resolution_due_at` is the vendor's own resolution goal for the ticket, `first_response_due_at` its first response goal. A ticket with no goal has None, which the
    analysis reports as `no_goal` and never as "fine". `resolved_at` is when the work was marked resolved, which can be before the ticket is closed: an open ticket is one that
    is neither closed nor resolved (`is_open`).
    """

    source: str
    ticket_id: str
    title: str
    category: str
    subcategory: str | None
    priority: str | None
    status: str
    company: str | None
    opened_at: datetime
    closed: bool
    closed_at: datetime | None = None
    resolved_at: datetime | None = None
    first_response_at: datetime | None = None
    first_response_due_at: datetime | None = None
    resolution_due_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("source", "ticket_id", "title", "category", "status"):
            _text(getattr(self, name), name)
        for name in ("subcategory", "priority", "company"):
            _text(getattr(self, name), name, allow_none=True)
        object.__setattr__(self, "opened_at", utc(self.opened_at, "opened_at"))
        for name in ("closed_at", "resolved_at", "first_response_at", "first_response_due_at", "resolution_due_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc(value, name))
        if not isinstance(self.closed, bool):
            raise RecordError("closed must be true or false")
        if self.closed_at is not None and not self.closed:
            raise RecordError("a ticket with a closed date is closed")
        if self.closed_at is not None and self.closed_at < self.opened_at:
            raise RecordError("ticket %s closed before it opened" % self.ticket_id)
        if self.resolved_at is not None and self.resolved_at < self.opened_at:
            raise RecordError("ticket %s resolved before it opened" % self.ticket_id)

    @property
    def is_open(self) -> bool:
        return not self.closed and self.resolved_at is None

    @property
    def finished_at(self) -> datetime | None:
        """When the resolution goal was answered: the resolved time, else the closed time, else None (still open)."""
        return self.resolved_at or self.closed_at


@dataclass(frozen=True)
class TimeRecord:
    """Time a technician worked: whole seconds, on one day, against a ticket or (ticket_id None) against something that is not a ticket."""

    source: str
    entry_id: str
    ticket_id: str | None
    technician: str
    worked_on: datetime
    seconds: int

    def __post_init__(self) -> None:
        _text(self.source, "source")
        _text(self.entry_id, "entry_id")
        _text(self.ticket_id, "ticket_id", allow_none=True)
        _text(self.technician, "technician")
        object.__setattr__(self, "worked_on", utc(self.worked_on, "worked_on"))
        if isinstance(self.seconds, bool) or not isinstance(self.seconds, int) or self.seconds < 0:
            raise RecordError("seconds must be a whole number of at least 0, got %r" % (self.seconds,))


@dataclass(frozen=True)
class AlertRecord:
    """One active alert (a triggered condition) on one device."""

    source: str
    alert_id: str
    device_id: int
    device_name: str | None
    organization_id: int | None
    source_type: str
    source_name: str | None
    subject: str | None
    severity: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        _text(self.source, "source")
        _text(self.alert_id, "alert_id")
        _text(self.source_type, "source_type")
        if isinstance(self.device_id, bool) or not isinstance(self.device_id, int):
            raise RecordError("device_id must be a whole number")
        object.__setattr__(self, "created_at", utc(self.created_at, "created_at"))


@dataclass(frozen=True)
class DeviceRecord:
    source: str
    device_id: int
    name: str
    node_class: str | None
    organization_id: int | None
    offline: bool
    last_contact: datetime | None = None

    def __post_init__(self) -> None:
        _text(self.source, "source")
        _text(self.name, "name")
        if isinstance(self.device_id, bool) or not isinstance(self.device_id, int):
            raise RecordError("device_id must be a whole number")
        if not isinstance(self.offline, bool):
            raise RecordError("offline must be true or false")
        if self.last_contact is not None:
            object.__setattr__(self, "last_contact", utc(self.last_contact, "last_contact"))
