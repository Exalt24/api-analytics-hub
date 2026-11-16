"""Phases 2 and 3 end to end: tailor a resume and cover letter for one stored job, render them, store the files, and record what was made.

`tailor_and_store` reads the job from Postgres (as the tenant, so another tenant's job is a LookupError), runs the loop in tailor.py (a worker thread, because the model
call is a blocking HTTP request), renders the resume as DOCX and PDF and the cover letter as DOCX, writes each file to the same S3 compatible storage as the raw batches
under `documents/<tenant>/<job>/`, and upserts one `tailored_document` row for each kind with the score, the rounds, whether the grounding held and whether the threshold
was reached. Running it again for the same job replaces the rows and the files: one current resume and one current letter for each job.

A document that is not grounded is never stored: `tailor()` only returns a grounded document, and this function refuses to write one that says otherwise.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.db import Database

from . import render, tailor
from .archive import RawArchive
from .llm import LLM

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def tailor_and_store(db: Database, archive: RawArchive, tenant_id: str, job_id: str, profile: tailor.Profile, llm: LLM | None, *, threshold: int = 90,
                           max_rounds: int = 4, pdf: bool = True) -> dict[str, Any]:
    async with db.tenant(tenant_id) as conn:
        row = await conn.fetchrow("select title, company_name, description, technologies from job_posting where id = $1::uuid", job_id)
    if row is None:
        raise LookupError("no such job")
    job = {"title": row["title"], "company_name": row["company_name"], "description": row["description"] or "", "technologies": list(row["technologies"] or [])}
    res = await asyncio.to_thread(tailor.tailor, profile, job, llm, threshold=threshold, max_rounds=max_rounds)
    if not res.grounded:
        raise RuntimeError("the tailoring loop returned a document that is not grounded")
    base = "documents/%s/%s/" % (tenant_id, job_id)
    keys: dict[str, str] = {}
    keys["resume_docx"] = await asyncio.to_thread(archive.put_file, base + "resume.docx", render.to_docx(res.resume_text), DOCX)
    if pdf:
        keys["resume_pdf"] = await asyncio.to_thread(archive.put_file, base + "resume.pdf", await asyncio.to_thread(render.to_pdf, res.resume_text), "application/pdf")
    if res.cover_letter:
        keys["cover_letter_docx"] = await asyncio.to_thread(archive.put_file, base + "cover_letter.docx", render.to_docx("Cover letter\n\n" + res.cover_letter), DOCX)
    async with db.tenant(tenant_id) as conn:
        for kind, key, score in (("resume", keys.get("resume_pdf") or keys["resume_docx"], res.score), ("cover_letter", keys.get("cover_letter_docx"), None)):
            if key is None:
                continue
            await conn.execute(
                """insert into tailored_document (tenant_id, job_id, kind, score, rounds, grounded, reached_threshold, object_key) values ($1::uuid, $2::uuid, $3, $4, $5, true, $6, $7)
                   on conflict (tenant_id, job_id, kind) do update set score = excluded.score, rounds = excluded.rounds, grounded = true,
                   reached_threshold = excluded.reached_threshold, object_key = excluded.object_key, created_at = now()""",
                tenant_id, job_id, kind, score, res.rounds, res.reached_threshold, key)
    return {"score": res.score, "rounds": res.rounds, "grounded": True, "reached_threshold": res.reached_threshold, "ceiling": res.ceiling, "unsupported": res.unsupported,
            "used_model": res.used_model, "keys": keys, "cover_letter": bool(res.cover_letter)}
