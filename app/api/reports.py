"""The monthly report as an HTTP call, so a scheduler that cannot run a command can still run it.

`POST /api/reports/monthly` builds the month's deck for the caller's tenant (the tenant comes from the API key, never
from the request, as everywhere in this service) and answers with a short description of it. `GET
/api/reports/monthly/{month}/deck` returns the deck itself, and `POST .../delivered` records that the message carrying it
went out. This is the call an n8n HTTP Request node makes (n8n/monthly_report.workflow.json), and the same call is what a
Make scenario or a Zap would make; nothing here knows or says which of them is calling.

IDEMPOTENT BY KEY. The caller sends an `Idempotency-Key` header, one key for one tenant and one month. The first call
with a key builds the deck and keeps it; a second call with the same key, a minute or a week later, answers with the
stored deck and `"status": "replayed"` and builds nothing, and says whether the message was already delivered, so a caller
that checks that flag sends nothing twice. Two calls at the same moment are arbitrated by the database: one builds, the
other gets 409 with `Retry-After`. The key for another month is refused with 422, so a key can never mean two reports.

STATUSES. 200 built or replayed. 400 a month or key that is not well formed (and 422 for a body that is not an object). 401 and 403 as
everywhere. 404 no deck for that month. 409 the same key is being built right now. 422 the report cannot honestly be produced, with the
reason in `detail`: no store connection, two ad accounts and none named, two currencies in a month. 500 anything else, which says
nothing about the cause: the log has it.
"""
from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.auth import Principal, requires
from app.report.monthly import ReportError
from app.report.runs import KEY_RE, KeyReused, RunInProgress, StoredRun, build, claim, latest_deck, mark_delivered, parse_month

log = logging.getLogger("hub")
router = APIRouter()

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class MonthlyRequest(BaseModel):
    month: str = Field(description="YYYY-MM, the month to report")
    #: Treats this date as today, for a backfill or a demo. Omitted, today is the UTC date, as for the command line.
    as_of: date | None = None
    #: Name a connection when the tenant has more than one of that kind; the key decides the tenant, never these.
    connection_id: str | None = None
    web_connection_id: str | None = None
    paid_connection_id: str | None = None
    search_connection_id: str | None = None


def _body(run: StoredRun, key: str, state: str) -> dict:
    return {
        "status": state,
        "idempotency_key": key,
        "month": run.month,
        "slides": run.slides,
        "sources": run.sources,
        "sha256": run.sha256,
        "bytes": len(run.deck or b""),
        "insights": run.insights_source,
        "narrative": run.narrative,
        "delivered": run.delivered,
    }


@router.post("/api/reports/monthly")
async def monthly_report(
    body: MonthlyRequest,
    request: Request,
    principal: Principal = Depends(requires("write:report")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if not idempotency_key or not KEY_RE.match(idempotency_key):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="send an Idempotency-Key header of 8 to 128 letters, digits and : _ . @ -")
    try:
        parse_month(body.month)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    db = request.app.state.db
    try:
        state, run = await claim(db, principal.tenant_id, idempotency_key, body.month)
    except RunInProgress as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc), headers={"Retry-After": "5"}) from None
    except KeyReused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if state == "done":
        return _body(run, idempotency_key, "replayed")
    named = {k: v for k, v in (("connection_id", body.connection_id), ("web_connection_id", body.web_connection_id),
                               ("paid_connection_id", body.paid_connection_id), ("search_connection_id", body.search_connection_id)) if v}
    try:
        run = await build(db, principal.tenant_id, idempotency_key, body.month, as_of=body.as_of, **named)
    except ReportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:  # noqa: BLE001 - the run is already marked failed; say nothing about the cause to the caller
        log.exception("monthly report failed", extra={"event": "report_failed", "tenant_id": principal.tenant_id})
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="the report could not be built") from None
    return _body(run, idempotency_key, "created")


@router.get("/api/reports/monthly/{month}/deck")
async def monthly_deck(month: str, request: Request, principal: Principal = Depends(requires("read:report"))):
    try:
        parse_month(month)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    run = await latest_deck(request.app.state.db, principal.tenant_id, month)
    if run is None or run.deck is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no report has been built for that month")
    return Response(run.deck, media_type=PPTX, headers={
        "Content-Disposition": 'attachment; filename="monthly_report_%s.pptx"' % month, "X-Report-Sha256": run.sha256 or ""})


@router.post("/api/reports/monthly/{month}/delivered")
async def monthly_delivered(month: str, request: Request, principal: Principal = Depends(requires("write:report"))):
    try:
        parse_month(month)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    found, already = await mark_delivered(request.app.state.db, principal.tenant_id, month)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no report has been built for that month")
    return {"month": month, "delivered": True, "already_delivered": already}
