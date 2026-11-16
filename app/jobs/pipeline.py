"""ingest_batch: one batch from one vendor, through the five steps, safe to run twice.

  1. the run row (a batch is recorded once; a retried delivery of the same batch gets the first answer back and does nothing)
  2. the raw JSON to object storage, before anything is changed
  3. the dedupe window in Redis: a job already seen inside the window skips the database
  4. the Postgres upsert for the rest (the unique index makes it one row even when the window missed)
  5. embeddings into Qdrant for the rows that were INSERTED only, so a repeat sighting costs no embedding call

THE INPUT is what the n8n workflow sends: `raw` (the vendor's response as it came) and `jobs` (the output of n8n/jobs/normalize.js). The hub
re-checks the shape of every job and refuses what it cannot store (`invalid`) so a mapping fault shows up as a number on the run, not as a
row of nulls.

WHAT FAILS HOW. Redis down: the window is skipped, the run says `dedupe_unavailable` and the database still dedupes. Embeddings down: the
rows are stored with `embedded_at` null and the run is `partial`; the next call for the same batch id embeds only the rows still missing.
Object storage down: the batch is refused before anything is written, because a batch with no archived original is not replayable.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.db import Database

from . import store
from .archive import ArchiveError, RawArchive
from .dedupe import DedupeUnavailable, SlidingWindowDedupe, fingerprint
from .embed import JobIndex

log = logging.getLogger("hub.jobs")

REQUIRED = ("dedupe_key", "title")


@dataclass
class IngestResult:
    batch_id: str
    vendor: str
    replayed: bool = False
    received: int = 0
    invalid: int = 0
    new_jobs: int = 0
    duplicates: int = 0
    embedded: int = 0
    archive_key: str | None = None
    status: str = "ok"
    notes: list[str] = field(default_factory=list)


def _readable(job: Any) -> bool:
    return isinstance(job, dict) and all(isinstance(job.get(k), str) and job[k].strip() for k in REQUIRED)


DEADLOCK_TRIES = 4


class JobPipeline:
    async def _store(self, tenant_id: str, vendor: str, fresh: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, list[Any]]:
        """The upserts for one batch in ONE transaction, in a fixed order, retried when Postgres picks this transaction to break a deadlock.

        Two batches that report some of the same jobs, handled at the same moment (n8n sends the vendors in parallel), would otherwise take the row locks of the
        shared jobs in different orders and each wait for the other: Postgres kills one with a deadlock. The verification run against n8n hit exactly that. Locking
        in fingerprint order makes every transaction queue behind the same row first, so the cycle cannot form, and the retry covers the case where an index or a
        trigger orders things differently (the whole transaction is safe to run again: each statement is an idempotent upsert).
        """
        ordered = sorted(fresh, key=lambda j: fingerprint(j["dedupe_key"]))
        for attempt in range(DEADLOCK_TRIES):
            inserted_rows: list[dict[str, Any]] = []
            repeats = 0
            try:
                async with self._db.tenant(tenant_id) as conn:
                    for j in ordered:
                        job_id, inserted = (await store.upsert_jobs(conn, tenant_id, vendor, [j]))[0]
                        if inserted:
                            inserted_rows.append({**j, "id": job_id})
                        else:
                            repeats += 1
                    # a batch retried after an embedding failure: rows of this vendor stored but not yet embedded
                    pending = await conn.fetch("select id::text as id, title, company_name, location, remote, technologies, description, ats from job_posting "
                                               "where embedded_at is null and first_vendor = $1 limit 500", vendor)
                return inserted_rows, repeats, pending
            except asyncpg.exceptions.DeadlockDetectedError:
                if attempt + 1 == DEADLOCK_TRIES:
                    raise
                log.warning("deadlock on a batch upsert, trying again", extra={"event": "jobs_deadlock_retry", "vendor": vendor})
                await asyncio.sleep(0.05 * (attempt + 1))
        raise AssertionError("unreachable")

    @property
    def archive(self) -> RawArchive:
        return self._archive

    def __init__(self, db: Database, *, archive: RawArchive, dedupe: SlidingWindowDedupe | None, index: JobIndex | None):
        self._db = db
        self._archive = archive
        self._dedupe = dedupe
        self._index = index

    async def ingest_batch(self, tenant_id: str, vendor: str, batch_id: str, raw: Any, jobs: list[dict[str, Any]], *, trigger: str = "schedule",
                           invalid_in_normaliser: int = 0) -> IngestResult:
        res = IngestResult(batch_id=batch_id, vendor=vendor)
        async with self._db.tenant(tenant_id) as conn:
            run_id, created = await store.record_run(conn, tenant_id, vendor, batch_id, trigger)
        if not created:
            async with self._db.tenant(tenant_id) as conn:
                prior = await store.run_summary(conn, vendor, batch_id)
            if prior and prior["finished_at"] is not None and prior["status"] != "partial":
                res.replayed = True
                for k in ("received", "invalid", "new_jobs", "duplicates", "embedded", "archive_key", "status"):
                    setattr(res, k, prior[k])
                return res
            # an unfinished or partial earlier try: carry on, every step below is idempotent

        res.received = len(jobs) + invalid_in_normaliser
        good = [j for j in jobs if _readable(j)]
        res.invalid = res.received - len(good)

        try:
            res.archive_key = self._archive.put_batch(tenant_id, vendor, batch_id, raw)
        except ArchiveError as exc:
            async with self._db.tenant(tenant_id) as conn:
                await store.finish_run(conn, run_id, received=res.received, invalid=res.invalid, new_jobs=0, duplicates=0, embedded=0,
                                       archive_key=None, status="failed", error_detail=str(exc)[:500])
            raise

        # The window member is the fingerprint AND the vendor: the same vendor repeating a job inside the window is skipped, but a second vendor
        # reporting it is new to the window and reaches the upsert, which is what adds it to the row's vendor list and its sighting count.
        members = ["%s:%s" % (fingerprint(j["dedupe_key"]), vendor) for j in good]
        seen = [False] * len(good)
        if self._dedupe is not None and good:
            try:
                seen = self._dedupe.seen_many(tenant_id, members)
            except DedupeUnavailable as exc:
                res.notes.append("dedupe_unavailable")
                log.warning("dedupe window unavailable, the database dedupes alone: %s", str(exc)[:200], extra={"event": "jobs_dedupe_unavailable"})

        fresh = [j for j, s in zip(good, seen) if not s]
        res.duplicates = len(good) - len(fresh)
        inserted_rows, repeats, pending = await self._store(tenant_id, vendor, fresh)
        res.new_jobs = len(inserted_rows)
        res.duplicates += repeats
        have = {r["id"] for r in inserted_rows}
        todo = inserted_rows + [dict(p) for p in pending if p["id"] not in have]

        if self._index is not None and todo:
            try:
                written = self._index.add(tenant_id, todo)
                for attempt in range(DEADLOCK_TRIES):
                    try:
                        async with self._db.tenant(tenant_id) as conn:
                            await store.mark_embedded(conn, [str(r["id"]) for r in todo])
                        break
                    except asyncpg.exceptions.DeadlockDetectedError:
                        if attempt + 1 == DEADLOCK_TRIES:
                            raise
                        await asyncio.sleep(0.05 * (attempt + 1))
                res.embedded = written
            except Exception as exc:  # the embedding service, Qdrant or the network: stored rows stay, the batch is marked partial
                res.status = "partial"
                res.notes.append("embedding_failed")
                log.warning("embedding failed, rows stored without vectors: %s", str(exc)[:200], extra={"event": "jobs_embed_failed"})

        async with self._db.tenant(tenant_id) as conn:
            await store.finish_run(conn, run_id, received=res.received, invalid=res.invalid, new_jobs=res.new_jobs, duplicates=res.duplicates,
                                   embedded=res.embedded, archive_key=res.archive_key, status=res.status,
                                   error_detail=", ".join(res.notes) or None)
        return res
