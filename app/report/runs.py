"""The record of each monthly report a tenant asked for, so a repeated request gets the same deck and nothing is sent twice.

A monthly schedule will run twice sooner or later. This module is what makes that harmless: the first request with an
idempotency key builds the deck and keeps it (migration 009), a later request with the same key gets the stored deck
back byte for byte, and whether the message carrying it was delivered is recorded once, by the caller, after it went.

The database does the arbitration, not this process. The first request inserts the row; `unique (tenant_id,
idempotency_key)` makes a second request at the same moment see that row instead of inserting its own, so exactly one
of two simultaneous requests builds the deck and the other is told to come back (`RunInProgress`). A run that was
started and never finished (the process died) or that failed may be taken over after a wait; the takeover is a single
compare and set, so two takers cannot both win.

Every query runs as the request role inside a tenant scoped transaction, so row level security stops a key from one
tenant ever touching another's runs.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pptx import Presentation

from app.db import Database
from app.report.insights import Insights
from app.report.monthly import MonthlyReport, ReportError
from app.report.service import generate

#: What an idempotency key may look like. The same rule as the table's check, so a bad key is refused with a reason
#: before it reaches the database.
KEY_RE = re.compile(r"^[A-Za-z0-9:_.@-]{8,128}$")
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
#: A run that has said it is running for this long is taken to have died, and a later request may take it over.
STALE_SECONDS = 900


class RunInProgress(Exception):
    """Another request holds this key and has not finished. Retryable: try again in a moment."""


class KeyReused(Exception):
    """The key was used for another month. A key names one tenant's one month, never two."""


@dataclass(frozen=True)
class StoredRun:
    id: str
    month: str
    status: str
    deck: bytes | None
    sha256: str | None
    slides: int | None
    sources: list[str]
    narrative: list[str]
    insights_source: str | None
    delivered: bool


def parse_month(text: str) -> tuple[int, int]:
    m = MONTH_RE.match(text or "")
    if not m:
        raise ValueError("month must be YYYY-MM, for example 2026-10")
    return int(m.group(1)), int(m.group(2))


def _stored(row) -> StoredRun:
    return StoredRun(
        id=str(row["id"]), month=row["month"], status=row["status"], deck=bytes(row["deck"]) if row["deck"] is not None else None,
        sha256=row["deck_sha256"], slides=row["slides"], sources=list(row["sources"] or []), narrative=list(row["narrative"] or []),
        insights_source=row["insights_source"], delivered=row["delivered_at"] is not None,
    )


async def claim(db: Database, tenant_id: str, key: str, month: str) -> tuple[str, StoredRun | None]:
    """("owner", None) when this request must build the deck, or ("done", the stored run) when it already exists.

    Raises RunInProgress when another request is building it and KeyReused when the key belongs to another month.
    """
    async with db.tenant(tenant_id) as conn:
        inserted = await conn.fetchval(
            "insert into report_run (tenant_id, idempotency_key, month, status) values ($1::uuid, $2, $3, 'running') "
            "on conflict (tenant_id, idempotency_key) do nothing returning id", tenant_id, key, month)
        if inserted is not None:
            return "owner", None
        row = await conn.fetchrow("select * from report_run where idempotency_key = $1 for update", key)
        if row["month"] != month:
            raise KeyReused("this key was used for %s" % row["month"])
        if row["status"] == "done":
            return "done", _stored(row)
        taken = await conn.fetchval(
            "update report_run set status = 'running', started_at = now(), error = null, finished_at = null "
            "where id = $1 and (status = 'failed' or (status = 'running' and started_at < now() - make_interval(secs => $2))) "
            "returning id", row["id"], float(STALE_SECONDS))
        if taken is not None:
            return "owner", None
        raise RunInProgress("a request with this key is still building the report")


async def build(db: Database, tenant_id: str, key: str, month: str, *, as_of: date | None, **named) -> StoredRun:
    """Build the deck for a claimed key, keep it, and return the stored run. A failure is recorded and re-raised."""
    year, mon = parse_month(month)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path, report, insights = await generate(db, tenant_id, year, mon, Path(tmp) / "deck.pptx", as_of=as_of, **named)
            deck = Path(path).read_bytes()
            slides = len(Presentation(str(path)).slides)
        sources = _sources(report)
        sha = hashlib.sha256(deck).hexdigest()
        async with db.tenant(tenant_id) as conn:
            row = await conn.fetchrow(
                "update report_run set status = 'done', deck = $3, deck_sha256 = $4, slides = $5, sources = $6, narrative = $7, "
                "insights_source = $8, error = null, finished_at = now() where tenant_id = $1::uuid and idempotency_key = $2 "
                "returning *", tenant_id, key, deck, sha, slides, sources, list(insights.lines), insights.source)
        return _stored(row)
    except ReportError as exc:
        await _fail(db, tenant_id, key, str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 - whatever broke, the run must not stay `running` for a quarter of an hour
        await _fail(db, tenant_id, key, "the report could not be built (%s)" % type(exc).__name__)
        raise


def _sources(report: MonthlyReport) -> list[str]:
    return ["store"] + [name for name, part in (("website", report.web), ("ads", report.paid), ("search", report.search)) if part is not None]


async def _fail(db: Database, tenant_id: str, key: str, reason: str) -> None:
    async with db.tenant(tenant_id) as conn:
        await conn.execute(
            "update report_run set status = 'failed', error = $3, finished_at = now() where tenant_id = $1::uuid and idempotency_key = $2 "
            "and status = 'running'", tenant_id, key, reason[:500])


async def mark_delivered(db: Database, tenant_id: str, month: str) -> tuple[bool, bool]:
    """Record that the latest finished deck for `month` was delivered. Returns (found, was already delivered).

    The time is kept from the first call, so telling it twice changes nothing.
    """
    async with db.tenant(tenant_id) as conn:
        row = await conn.fetchrow(
            "select id, delivered_at from report_run where month = $1 and status = 'done' order by finished_at desc limit 1 for update", month)
        if row is None:
            return False, False
        if row["delivered_at"] is not None:
            return True, True
        await conn.execute("update report_run set delivered_at = now() where id = $1", row["id"])
        return True, False


async def latest_deck(db: Database, tenant_id: str, month: str) -> StoredRun | None:
    """The latest finished run for `month`, or None."""
    async with db.tenant(tenant_id) as conn:
        row = await conn.fetchrow("select * from report_run where month = $1 and status = 'done' order by finished_at desc limit 1", month)
    return _stored(row) if row else None
