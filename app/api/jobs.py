"""The job pipeline over HTTP: what n8n calls, and what a dashboard (Appsmith, or any internal tool) reads and writes.

WHO MAY DO WHAT, by the key's role (app/api/auth.py): every role may read postings, runs and the review queue (`read:jobs`); an operator or admin may
ingest a batch, start an application, answer a review task and send a run to manual review (`write:jobs`). The tenant comes from the key and nowhere
else. A run, a task or a posting of another tenant answers 404 with the body of an id that does not exist.

THE RUNTIME (`JobsRuntime`) holds the parts that are not a database: the Redis window, the archive, the Qdrant index, the RabbitMQ broker, the LLM
and the browser settings. It is built from JOBS_* environment variables the first time a route needs it, or assigned to `app.state.jobs` by a test or a
verification script, so the same routes run against the stand-ins and against the real services.

STATUSES. 200 done. 202 an application was accepted and is running. 404 not yours or not there. 409 the task is already answered. 422 a body that
cannot be stored (an empty answer, a batch with no id). 503 a part this route needs is not configured.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal, requires
from app.jobs import documents, hitl, tailor
from app.jobs.pipeline import JobPipeline

log = logging.getLogger("hub")
router = APIRouter()

VENDORS = {"theirstack", "coresignal", "brightdata", "apify", "jobdataapi", "linkdapi", "predictleads", "crustdata", "techmap", "hirebase", "grepsr"}


@dataclass
class JobsRuntime:
    pipeline: JobPipeline | None = None
    broker: hitl.Broker | None = None
    index: Any = None
    browser: dict[str, Any] = field(default_factory=lambda: {"mode": "local"})
    files: dict[str, str] = field(default_factory=dict)
    submit: bool = False
    tasks: set = field(default_factory=set)
    board: Any = None
    llm: Any = None


def runtime(request: Request) -> JobsRuntime:
    rt = getattr(request.app.state, "jobs", None)
    if rt is None:
        raise HTTPException(status_code=503, detail="the job pipeline is not configured on this server")
    return rt


class IngestBody(BaseModel):
    batch_id: str = Field(min_length=1, max_length=200)
    raw: Any = None
    jobs: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)
    invalid: int = Field(default=0, ge=0)
    trigger: str = Field(default="schedule", pattern="^(schedule|webhook|manual)$")


@router.post("/api/jobs/ingest/{vendor}")
async def ingest(vendor: str, body: IngestBody, request: Request, principal: Principal = Depends(requires("write:jobs"))):
    if vendor not in VENDORS:
        raise HTTPException(status_code=404, detail="unknown vendor")
    rt = runtime(request)
    if rt.pipeline is None:
        raise HTTPException(status_code=503, detail="ingestion is not configured on this server")
    res = await rt.pipeline.ingest_batch(principal.tenant_id, vendor, body.batch_id, body.raw, body.jobs, trigger=body.trigger, invalid_in_normaliser=body.invalid)
    return {"batch_id": res.batch_id, "vendor": res.vendor, "replayed": res.replayed, "received": res.received, "invalid": res.invalid, "new_jobs": res.new_jobs,
            "duplicates": res.duplicates, "embedded": res.embedded, "status": res.status, "notes": res.notes}


@router.get("/api/jobs")
async def list_jobs(request: Request, q: str | None = Query(default=None, max_length=300), limit: int = Query(default=20, ge=1, le=100),
                    principal: Principal = Depends(requires("read:jobs"))):
    rt = runtime(request)
    db = request.app.state.db
    if q and rt.index is not None:
        ids = [h[0]["job_id"] for h in rt.index.search(principal.tenant_id, q, k=limit)]
        async with db.tenant(principal.tenant_id) as conn:
            rows = await conn.fetch("select id::text, title, company_name, location, ats, apply_url, vendors, seen_count from job_posting where id = any($1::uuid[])", ids)
        by_id = {r["id"]: dict(r) for r in rows}
        return {"jobs": [by_id[i] for i in ids if i in by_id]}
    async with db.tenant(principal.tenant_id) as conn:
        rows = await conn.fetch("select id::text, title, company_name, location, ats, apply_url, vendors, seen_count from job_posting order by last_seen_at desc limit $1", limit)
    return {"jobs": [dict(r) for r in rows]}


class ApplyBody(BaseModel):
    apply_url: str = Field(pattern="^https?://", max_length=2000)
    job_id: str | None = None
    resume_url: str | None = Field(default=None, pattern="^https?://", max_length=2000)


@router.post("/api/jobs/apply", status_code=status.HTTP_202_ACCEPTED)
async def apply(body: ApplyBody, request: Request, principal: Principal = Depends(requires("write:jobs"))):
    rt = runtime(request)
    db = request.app.state.db
    run_id = await hitl.create_run(db, principal.tenant_id, body.apply_url, job_id=body.job_id, resume_url=body.resume_url)

    async def work() -> None:
        try:
            await hitl.run_application(db, principal.tenant_id, run_id, browser=rt.browser, files=rt.files, submit=rt.submit, broker=rt.broker, board=rt.board)
        except Exception as exc:  # the run row must never be left in `filling` by a crash
            log.exception("application run crashed", extra={"event": "jobs_apply_crashed", "run": run_id})
            async with db.tenant(principal.tenant_id) as conn:
                await conn.execute("update application_run set state='failed', resume_token=null, detail=$2, updated_at=now() where id=$1::uuid", run_id, str(exc)[:300])
            await hitl.notify_resume_url(body.resume_url, {"run_id": run_id, "state": "failed"})

    task = asyncio.create_task(work())
    rt.tasks.add(task)
    task.add_done_callback(rt.tasks.discard)
    return {"run_id": run_id, "state": "queued"}


class ExperienceBody(BaseModel):
    company: str = Field(max_length=200)
    title: str = Field(max_length=200)
    start: str = Field(max_length=40)
    end: str = Field(max_length=40)
    bullets: list[str] = Field(default_factory=list, max_length=20)


class ProjectBody(BaseModel):
    name: str = Field(max_length=200)
    bullets: list[str] = Field(default_factory=list, max_length=10)


class EducationBody(BaseModel):
    school: str = Field(max_length=200)
    degree: str = Field(max_length=200)
    year: str = Field(max_length=20)


class ProfileBody(BaseModel):
    name: str = Field(max_length=200)
    email: str = Field(max_length=254)
    location: str = Field(default="", max_length=200)
    links: list[str] = Field(default_factory=list, max_length=10)
    summary: list[str] = Field(default_factory=list, max_length=10)
    skills: list[str] = Field(default_factory=list, max_length=80)
    experience: list[ExperienceBody] = Field(default_factory=list, max_length=15)
    projects: list[ProjectBody] = Field(default_factory=list, max_length=10)
    education: list[EducationBody] = Field(default_factory=list, max_length=6)


class TailorBody(BaseModel):
    profile: ProfileBody
    threshold: int = Field(default=90, ge=50, le=100)


@router.post("/api/jobs/{job_id}/tailor")
async def tailor_job(job_id: str, body: TailorBody, request: Request, principal: Principal = Depends(requires("write:jobs"))):
    """Tailor a resume and cover letter for one stored job from the applicant's own facts, store the files, answer the score and where they are."""
    rt = runtime(request)
    if rt.pipeline is None:
        raise HTTPException(status_code=503, detail="document storage is not configured on this server")
    p = body.profile
    profile = tailor.Profile(name=p.name, email=p.email, location=p.location, links=p.links, summary=p.summary, skills=p.skills,
                             experience=[tailor.Experience(**e.model_dump()) for e in p.experience], projects=[tailor.Project(**x.model_dump()) for x in p.projects],
                             education=[tailor.Education(**d.model_dump()) for d in p.education])
    try:
        return await documents.tailor_and_store(request.app.state.db, rt.pipeline.archive, principal.tenant_id, job_id, profile, rt.llm, threshold=body.threshold)
    except (LookupError, ValueError, asyncpg.exceptions.DataError):
        raise HTTPException(status_code=404, detail="no such job") from None


@router.get("/api/jobs/{job_id}/documents")
async def job_documents(job_id: str, request: Request, principal: Principal = Depends(requires("read:jobs"))):
    try:
        async with request.app.state.db.tenant(principal.tenant_id) as conn:
            rows = await conn.fetch("select kind, score, rounds, grounded, reached_threshold, object_key, created_at from tailored_document where job_id = $1::uuid order by kind", job_id)
    except Exception:
        rows = []
    return {"documents": [dict(r) for r in rows]}


@router.get("/api/jobs/runs/{run_id}")
async def get_run(run_id: str, request: Request, principal: Principal = Depends(requires("read:jobs"))):
    db = request.app.state.db
    try:
        async with db.tenant(principal.tenant_id) as conn:
            r = await conn.fetchrow("select id::text, apply_url, state, filled, needs_review, detail, created_at, updated_at from application_run where id = $1::uuid", run_id)
    except Exception:
        r = None
    if r is None:
        raise HTTPException(status_code=404, detail="no such run")
    return dict(r)


@router.get("/api/jobs/review")
async def review_queue(request: Request, run_id: str | None = None, principal: Principal = Depends(requires("read:jobs"))):
    return {"tasks": await hitl.pending_tasks(request.app.state.db, principal.tenant_id, run_id)}


class AnswerBody(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)
    promote_to_key: str | None = Field(default=None, pattern="^[a-z][a-z0-9_.]{0,79}$")


@router.post("/api/jobs/review/{task_id}/answer")
async def answer_task(task_id: str, body: AnswerBody, request: Request, principal: Principal = Depends(requires("write:jobs"))):
    rt = runtime(request)
    try:
        res = await hitl.resolve_task(request.app.state.db, principal.tenant_id, task_id, body.answer, "key:%s" % principal.key_id[:8], broker=rt.broker,
                                      promote_to_key=body.promote_to_key, board=rt.board)
    except LookupError:
        raise HTTPException(status_code=409, detail="the task does not exist or is already answered") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return res


class ManualBody(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


@router.post("/api/jobs/runs/{run_id}/manual")
async def manual(run_id: str, body: ManualBody, request: Request, principal: Principal = Depends(requires("write:jobs"))):
    db = request.app.state.db
    async with db.tenant(principal.tenant_id) as conn:
        exists = await conn.fetchval("select 1 from application_run where id = $1::uuid and state = 'waiting_review'", run_id)
    if not exists:
        raise HTTPException(status_code=404, detail="no such run waiting for review")
    await hitl.send_to_manual(db, principal.tenant_id, run_id, "key:%s" % principal.key_id[:8], body.reason)
    return {"run_id": run_id, "state": "manual"}


async def build_jobs_runtime(db) -> JobsRuntime | None:
    """From the JOBS_* variables. Returns None (the routes then answer 503) unless a bucket is named, so a server that never set the pipeline up
    exposes nothing half-working. Redis, Qdrant with embeddings, and RabbitMQ are each optional: a missing one is logged and left out."""
    bucket = os.environ.get("JOBS_S3_BUCKET")
    if not bucket:
        return None
    from qdrant_client import QdrantClient
    import redis

    from app.jobs.archive import RawArchive, client_from_env
    from app.jobs.dedupe import SlidingWindowDedupe
    from app.jobs.embed import JobIndex, make_embeddings

    dedupe = None
    if os.environ.get("JOBS_REDIS_URL"):
        dedupe = SlidingWindowDedupe(redis.Redis.from_url(os.environ["JOBS_REDIS_URL"], socket_connect_timeout=2, socket_timeout=3),
                                     window_seconds=float(os.environ.get("JOBS_DEDUPE_HOURS", "72")) * 3600)
    index = None
    if os.environ.get("JOBS_QDRANT_URL") and os.environ.get("JOBS_OPENAI_API_KEY"):
        index = JobIndex(QdrantClient(url=os.environ["JOBS_QDRANT_URL"], api_key=os.environ.get("JOBS_QDRANT_API_KEY") or None),
                         make_embeddings(api_key=os.environ["JOBS_OPENAI_API_KEY"], base_url=os.environ.get("JOBS_OPENAI_BASE_URL") or None,
                                         model=os.environ.get("JOBS_EMBED_MODEL", "text-embedding-3-small")),
                         collection=os.environ.get("JOBS_COLLECTION", "job_postings"))
    broker = None
    if os.environ.get("JOBS_AMQP_URL"):
        try:
            broker = await hitl.Broker(os.environ["JOBS_AMQP_URL"]).connect()
        except Exception as exc:
            log.warning("RabbitMQ is not reachable, reviews will not be announced on a queue", extra={"event": "jobs_broker_unavailable", "error": str(exc)[:200]})
    pipeline = JobPipeline(db, archive=RawArchive(client_from_env(), bucket), dedupe=dedupe, index=index)
    browser: dict[str, Any] = {"mode": os.environ.get("JOBS_BROWSER", "local")}
    if browser["mode"] == "browserbase":
        browser.update(browserbase_api_key=os.environ.get("BROWSERBASE_API_KEY"), browserbase_project_id=os.environ.get("BROWSERBASE_PROJECT_ID"),
                       browserbase_base_url=os.environ.get("BROWSERBASE_BASE_URL") or None)
    files = {k: v for k, v in (("resume_file", os.environ.get("JOBS_RESUME_FILE")), ("cover_letter_file", os.environ.get("JOBS_COVER_LETTER_FILE"))) if v}
    llm = None
    if os.environ.get("JOBS_LLM_API_KEY"):
        from app.jobs.llm import LLM

        llm = LLM(api_key=os.environ["JOBS_LLM_API_KEY"], base_url=os.environ.get("JOBS_LLM_BASE_URL") or "https://api.openai.com/v1", model=os.environ.get("JOBS_LLM_MODEL", "gpt-4o"))
    board = None
    if os.environ.get("JOBS_NOTION_TOKEN") and os.environ.get("JOBS_NOTION_DATA_SOURCE"):
        from app.jobs.notion import NotionBoard

        board = NotionBoard(os.environ["JOBS_NOTION_TOKEN"], os.environ["JOBS_NOTION_DATA_SOURCE"], base_url=os.environ.get("JOBS_NOTION_BASE_URL") or "https://api.notion.com")
    return JobsRuntime(pipeline=pipeline, broker=broker, index=index, browser=browser, files=files, submit=os.environ.get("JOBS_SUBMIT") == "1", board=board, llm=llm)


async def close_jobs_runtime(rt: JobsRuntime | None) -> None:
    if rt is not None:
        if rt.tasks:
            await asyncio.gather(*list(rt.tasks), return_exceptions=True)
        if rt.broker is not None:
            await rt.broker.close()
