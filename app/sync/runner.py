"""The sync worker: claim a connection, pull a window, write snapshots, finish.

This is the piece that turns a connector into a pipeline, and every decision below
exists because of a specific way scheduled syncs fail quietly.

CLAIM THROUGH THE DATABASE, not through a scheduler's memory. `claim_sync` takes a
non-blocking advisory lock and refuses if a live run already holds the connection,
so two workers, or one worker plus a Sync Now click, cannot double-sync. It is
non-blocking on purpose: a second worker should skip, not queue behind a long sync
and then start a duplicate the instant it finishes.

A LEASE, NOT A FLAG. A `running` row cannot tell "still working" from "died nine
days ago", and a died-nine-days-ago row means the connection never syncs again AND
never alerts, because nothing is technically failed. So the run carries an expiry
that the worker must keep renewing, and the reaper turns a silent death into a
visible failure.

PARTIAL PROGRESS IS COMMITTED. The connector yields pages with a cursor, and each
page is written and the cursor recorded before the next request goes out. A rate
limit halfway through a backfill then resumes instead of discarding the first half.

FAILURE IS RECORDED WITH ITS KIND. `retryable` on the error decides whether the run
is worth retrying, so a revoked token stops instead of hammering an endpoint that
will never succeed, and a 429 does not get treated as a dead integration.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.connectors.base import ConnectorError, ProtectedDataDenied

#: How long a claim is good for before the reaper may take it. Long enough that a
#: slow page does not lose the lease, short enough that a crash is noticed within
#: one schedule interval.
LEASE_SECONDS = 300

#: Renew at a third of the lease, so two consecutive missed heartbeats still leave
#: room before expiry. Renewing at the deadline is the same as not renewing.
HEARTBEAT_SECONDS = LEASE_SECONDS // 3


@dataclass
class RunOutcome:
    run_id: str | None
    claimed: bool
    status: str = "skipped"
    rows_written: int = 0
    api_calls: int = 0
    throttle_waits: int = 0
    degraded_metrics: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_detail: str | None = None


def worker_id() -> str:
    """Identify the process in the run log. A hostname alone is not enough when
    two workers run on one box, which is the normal deployment."""
    return "%s/%d/%s" % (socket.gethostname(), os.getpid(), uuid.uuid4().hex[:6])


class SyncRunner:
    def __init__(self, db, *, lease_seconds: int = LEASE_SECONDS):
        self._db = db
        self._lease = lease_seconds

    async def run_once(
        self,
        *,
        tenant_id: str,
        connection_id: str,
        connector,
        window_start: date | None = None,
        window_end: date | None = None,
        trigger: str = "schedule",
    ) -> RunOutcome:
        end = window_end or date.today()
        start = window_start or (end - timedelta(days=7))
        wid = worker_id()

        async with self._db.unscoped() as conn:
            claim = await conn.fetchrow(
                "select * from claim_sync($1::uuid, $2, $3, $4, $5::date, $6::date)",
                connection_id, wid, self._lease, trigger, start, end,
            )

        if claim is None or claim["run_id"] is None:
            # Not an error. Another worker holds it, which is the lock doing its
            # job, and reporting it as a failure would make a healthy system look
            # broken every time two schedules coincide.
            return RunOutcome(run_id=None, claimed=False)

        run_id = str(claim["run_id"])
        token = str(claim["claim_token"])
        beat = asyncio.create_task(self._heartbeat(run_id, token))

        rows = 0
        calls = 0
        waits = 0
        try:
            async for page in connector.fetch(window_start=start, window_end=end):
                calls += page.api_calls
                waits += page.throttle_waits
                if page.points:
                    rows += await self._write_points(
                        tenant_id, connection_id, page.points
                    )
                # Record the cursor BEFORE the next request, so a failure resumes
                # from here rather than from the top of the window.
                await self._record_cursor(run_id, token, page.cursor, calls, waits, rows)

            degraded = sorted(getattr(connector, "degraded_metrics", set()) or [])
            status = "partial" if degraded else "succeeded"
            await self._finish(run_id, token, status, rows, calls, waits)
            return RunOutcome(run_id, True, status, rows, calls, waits, degraded)

        except ProtectedDataDenied as exc:
            # Separated from the generic path because the remedy is neither a retry
            # nor a reconnect: a human has to complete a data declaration.
            await self._finish(run_id, token, "failed", rows, calls, waits,
                               "protected_data_denied", str(exc))
            return RunOutcome(run_id, True, "failed", rows, calls, waits,
                              error_code="protected_data_denied", error_detail=str(exc))

        except ConnectorError as exc:
            code = getattr(exc, "code", "connector_error")
            await self._finish(run_id, token, "failed", rows, calls, waits,
                               code, str(exc))
            return RunOutcome(run_id, True, "failed", rows, calls, waits,
                              error_code=code, error_detail=str(exc))

        except Exception as exc:  # noqa: BLE001
            # An unexpected error must still close the run. Leaving it 'running'
            # is the exact silent-stall this design exists to prevent, so the
            # bare except is deliberate and it re-raises after recording.
            await self._finish(run_id, token, "failed", rows, calls, waits,
                               "unexpected", repr(exc)[:500])
            raise

        finally:
            beat.cancel()
            # Await the cancellation so a test cannot finish with the task still
            # pending and log "Task was destroyed but it is pending".
            try:
                await beat
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ helpers

    async def _write_points(self, tenant_id, connection_id, points) -> int:
        """Hand the whole page to upsert_snapshots as one statement.

        One call rather than a loop because the function de-duplicates with
        DISTINCT ON: Postgres raises "cannot affect row a second time" when a
        single INSERT ... ON CONFLICT carries two rows with the same conflict key,
        and a connector legitimately can produce that.
        """
        payload = [
            {
                "tenant_id": str(tenant_id),
                "connection_id": str(connection_id),
                "metric_key": p.metric_key,
                "observed_on": p.observed_on.isoformat(),
                "value_numeric": int(p.value_numeric),
                "currency": p.currency,
                "raw": json.dumps(p.raw or {}),
            }
            for p in points
        ]
        async with self._db.unscoped() as conn:
            written = await conn.fetchval(
                "select upsert_snapshots($1::jsonb)", json.dumps(payload)
            )
        return int(written or 0)

    async def _record_cursor(self, run_id, token, cursor, calls, waits, rows) -> None:
        async with self._db.unscoped() as conn:
            await conn.execute(
                """
                update sync_run
                   set resume_cursor = $3,
                       api_calls = $4, throttle_waits = $5, rows_written = $6,
                       heartbeat_at = now(),
                       lease_expires_at = now() + make_interval(secs => $7)
                 where id = $1::uuid and claim_token = $2::uuid
                """,
                run_id, token, cursor, calls, waits, rows, self._lease,
            )

    async def _finish(self, run_id, token, status, rows, calls, waits,
                      code=None, detail=None) -> None:
        """Close the run, but only if we still hold the claim.

        The claim_token in the WHERE clause is the important part: if the reaper
        already expired this lease and another worker took over, this worker must
        not overwrite the new run's result with its own stale one.
        """
        async with self._db.unscoped() as conn:
            await conn.execute(
                """
                update sync_run
                   set status = $3, rows_written = $4, api_calls = $5,
                       throttle_waits = $6, error_code = $7, error_detail = $8,
                       finished_at = now()
                 where id = $1::uuid and claim_token = $2::uuid
                   and status = 'running'
                """,
                run_id, token, status, rows, calls, waits, code,
                (detail or None) and str(detail)[:2000],
            )

    async def _heartbeat(self, run_id: str, token: str) -> None:
        """Extend the lease while work is in progress."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                async with self._db.unscoped() as conn:
                    await conn.execute(
                        """
                        update sync_run
                           set heartbeat_at = now(),
                               lease_expires_at = now() + make_interval(secs => $3)
                         where id = $1::uuid and claim_token = $2::uuid
                           and status = 'running'
                        """,
                        run_id, token, self._lease,
                    )
        except asyncio.CancelledError:
            raise
