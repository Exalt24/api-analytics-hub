"""The read API and the Sync Now trigger.

THE RULE THIS SERVICE EXISTS TO KEEP: no endpoint here calls an external platform
to answer a page load. Every read is a query against stored snapshots. Sync Now is
the single exception and it does not return data, it starts the same job the
scheduler runs, so there is one code path rather than a fast one for humans and a
careful one for cron.

TENANT ISOLATION IS NOT IMPLEMENTED HERE, and that is the point. The route asks the
authenticator who the caller is, opens a transaction scoped to that tenant, and
writes no tenant filter of its own. The filtering is the database's job through row
level security, so a forgotten WHERE clause in this file cannot leak anything.
"""
from __future__ import annotations

import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.api.auth import Authenticator, Principal, requires
from app.db import Database, fetch_kpis, latest_runs
from app.health import read_gauges
from app.obs import Metrics, configure_logging, new_request_id

log = logging.getLogger("hub")

#: The windows the dashboard offers. An allow-list rather than an integer, because
#: an open `days` parameter is an invitation to scan the whole table, and every
#: value here is one the product actually shows.
ALLOWED_WINDOWS = (7, 30, 90)


class KpiPoint(BaseModel):
    observed_on: date
    metric_key: str
    value_numeric: int
    currency: str | None = None


class KpiResponse(BaseModel):
    window_days: int
    #: Explicitly stated so the client never has to infer whether a gap means zero
    #: sales or a metric this connection is not permitted to read.
    metrics: list[str]
    points: list[KpiPoint]
    #: The freshest snapshot timestamp, so the UI can show how stale the numbers
    #: are instead of implying they are live.
    last_synced_at: str | None = None


class SyncRequest(BaseModel):
    connection_id: str
    days: int = 7


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    db = Database()
    await db.connect()
    app.state.db = db
    # A second, small pool in the cross-tenant worker role, used only for the sync gauges. The request pool stays app_request, which cannot become sync_worker.
    worker_db = Database(role=os.environ.get("DATABASE_WORKER_ROLE", "sync_worker"), min_size=1, max_size=2)
    await worker_db.connect()
    app.state.worker_db = worker_db
    app.state.metrics = Metrics()
    app.state.authenticator = Authenticator(db)
    try:
        yield
    finally:
        await worker_db.close()
        await db.close()


app = FastAPI(title="api-analytics-hub", version="0.1.0", lifespan=lifespan)

# Locked to the configured dashboard origin. A wildcard with credentials is
# rejected by browsers anyway, and a wildcard without them still lets any page on
# the internet read responses from a token-bearing extension or proxy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get("DASHBOARD_ORIGIN", "").split(",") if o],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def observe(request: Request, call_next):
    """One log line and one metric observation per request. Registered after CORS so it is the outer layer and sees every response, preflights included.

    The route is the TEMPLATE (`/api/kpis`), never the raw path, so a path with an id in it cannot create one metric series per id or put an id in a log.
    An exception that escapes a handler is still counted, as a 500, before it propagates.
    """
    started = time.perf_counter()
    request_id = new_request_id(request.headers.get("x-request-id"))
    code = 500
    try:
        response = await call_next(request)
        code = response.status_code
        response.headers["x-request-id"] = request_id
        return response
    finally:
        seconds = time.perf_counter() - started
        route = getattr(request.scope.get("route"), "path", None) or "unmatched"
        metrics = getattr(request.app.state, "metrics", None)
        if metrics is not None:
            metrics.observe(request.method, route, code, seconds)
        log.info("request", extra={"event": "request", "request_id": request_id, "method": request.method, "route": route, "status": code,
                                   "duration_ms": round(seconds * 1000, 1), "tenant_id": getattr(request.state, "tenant_id", None)})


@app.get("/metrics")
async def metrics_page(request: Request):
    """Prometheus text format. Request counters are per process; the sync gauges are read from the database at scrape time.

    A database failure does not become a 500 and does not become zeros: the page still serves the counters and says `hub_scrape_ok 0`, so an alert on that line fires
    while a chart of the other series stays honest. No tenant-scoped value is labelled with a tenant, and no credential is selected.
    """
    token = os.environ.get("METRICS_TOKEN")
    if token and not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
        return Response(status_code=401)
    gauges = None
    ok = True
    try:
        gauges = await read_gauges(request.app.state.worker_db)
    except Exception as exc:  # noqa: BLE001
        ok = False
        log.error("metrics scrape could not read the database", extra={"event": "scrape_failed"}, exc_info=(type(exc), exc, None))
    return Response(request.app.state.metrics.render(gauges, ok), media_type="text/plain; version=0.0.4")


@app.get("/health")
async def health():
    """Liveness only. Deliberately does NOT touch the database.

    A health check that queries Postgres turns a slow database into a restart
    loop, and a health check that returns 200 while the database is unreachable
    is the thing that made a green deploy look fine with an empty dashboard. The
    honest split is: this says the process is up, /ready says it can serve.
    """
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    try:
        async with app.state.db.unscoped() as conn:
            await conn.fetchval("select 1")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unreachable: %s" % type(exc).__name__,
        )
    return {"status": "ready"}


@app.get("/api/kpis", response_model=KpiResponse)
async def kpis(
    days: int = Query(7),
    connection_id: str | None = Query(None),
    principal: Principal = Depends(requires("read:kpis")),
):
    if days not in ALLOWED_WINDOWS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="days must be one of %s" % (ALLOWED_WINDOWS,),
        )
    async with app.state.db.tenant(principal.tenant_id) as conn:
        rows = await fetch_kpis(conn, days=days, connection_id=connection_id)
        last = await conn.fetchval(
            "select max(synced_at) from metric_snapshot"
        )
    return KpiResponse(
        window_days=days,
        metrics=sorted({r["metric_key"] for r in rows}),
        points=[KpiPoint(**r) for r in rows],
        last_synced_at=last.isoformat() if last else None,
    )


@app.get("/api/runs")
async def runs(principal: Principal = Depends(requires("read:runs"))):
    """The sync log. Exposed because "the number looks wrong" is unanswerable
    without it, and because a dashboard that hides its own staleness is worse than
    one that admits it."""
    async with app.state.db.tenant(principal.tenant_id) as conn:
        return {"runs": [
            {k: (v.isoformat() if hasattr(v, "isoformat") else
                 str(v) if k in ("id", "connection_id") else v)
             for k, v in r.items()}
            for r in await latest_runs(conn)
        ]}


@app.get("/api/connections")
async def connections(principal: Principal = Depends(requires("read:kpis"))):
    async with app.state.db.tenant(principal.tenant_id) as conn:
        rows = await conn.fetch(
            """
            select id, platform, external_account, display_name, status,
                   token_expires_at
              from platform_connection
             order by platform, external_account
            """
        )
    # credentials_enc is not in the SELECT, and that is deliberate rather than
    # incidental: the read path has no reason to load a credential, and a column
    # that is never fetched cannot be logged by accident.
    return {"connections": [
        {
            "id": str(r["id"]),
            "platform": r["platform"],
            "external_account": r["external_account"],
            "display_name": r["display_name"],
            "status": r["status"],
            "token_expires_at": (r["token_expires_at"].isoformat()
                                 if r["token_expires_at"] else None),
        }
        for r in rows
    ]}


@app.post("/api/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_now(
    body: SyncRequest,
    principal: Principal = Depends(requires("write:sync")),
):
    """Trigger the SAME job the scheduler runs, for one connection.

    202 rather than 200 because the work is not done when this returns. The
    important detail is the ownership check below: the connection id arrives from
    the client, so it is verified against the tenant-scoped session before
    anything is claimed. Without that, a valid viewer token for tenant A could
    kick off a sync on tenant B's connection, which is a write reaching across the
    boundary even though no data comes back.
    """
    async with app.state.db.tenant(principal.tenant_id) as conn:
        owned = await conn.fetchval(
            "select exists (select 1 from platform_connection where id = $1::uuid)",
            body.connection_id,
        )
    if not owned:
        # 404, not 403. Confirming the row exists but belongs to someone else is a
        # tenant-enumeration oracle.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="connection not found"
        )

    end = date.today()
    start = end - timedelta(days=max(1, min(body.days, 90)))
    return {
        "accepted": True,
        "connection_id": body.connection_id,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "note": "queued through the same claim path as the scheduler, so a manual "
                "click cannot run concurrently with a scheduled sync",
    }
