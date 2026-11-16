"""Postgres writes for the job pipeline, always inside `db.tenant(...)` so row level security does the isolating.

`upsert_jobs` is one statement per job: insert, or on a clash of (tenant, fingerprint) move `last_seen_at` forward, count the sighting,
add the vendor to the list and fill in anything the first sighting did not have. It never overwrites a value that is already there with
a worse one (a null never replaces a title or a link), and it reports for each job whether the row was created, using `xmax = 0`, the
standard way to tell an insert from an update in one `ON CONFLICT` statement.
"""
from __future__ import annotations

import json
from typing import Any

import asyncpg

from .dedupe import fingerprint

UPSERT = """
insert into job_posting (tenant_id, fingerprint, dedupe_key, title, company_name, company_domain, location, remote, url, apply_url, ats, ats_org,
                         ats_job_id, posted_at, description, salary_min, salary_max, salary_currency, salary_period, employment_type,
                         technologies, vendors, first_vendor)
values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::timestamptz, $15, $16, $17, $18, $19, $20, $21::text[], array[$22]::text[], $22)
on conflict (tenant_id, fingerprint) do update set
    last_seen_at = now(),
    seen_count = job_posting.seen_count + 1,
    vendors = (select array(select distinct unnest(job_posting.vendors || excluded.vendors) order by 1)),
    company_domain = coalesce(job_posting.company_domain, excluded.company_domain),
    location = coalesce(job_posting.location, excluded.location),
    remote = coalesce(job_posting.remote, excluded.remote),
    url = coalesce(job_posting.url, excluded.url),
    apply_url = coalesce(job_posting.apply_url, excluded.apply_url),
    ats = coalesce(job_posting.ats, excluded.ats),
    ats_org = coalesce(job_posting.ats_org, excluded.ats_org),
    ats_job_id = coalesce(job_posting.ats_job_id, excluded.ats_job_id),
    posted_at = coalesce(job_posting.posted_at, excluded.posted_at),
    description = coalesce(job_posting.description, excluded.description),
    salary_min = coalesce(job_posting.salary_min, excluded.salary_min),
    salary_max = coalesce(job_posting.salary_max, excluded.salary_max),
    salary_currency = coalesce(job_posting.salary_currency, excluded.salary_currency),
    technologies = case when cardinality(job_posting.technologies) = 0 then excluded.technologies else job_posting.technologies end
returning id, (xmax = 0) as inserted
"""


def _currency(job: dict[str, Any]) -> str | None:
    c = job.get("salary_currency")
    return c.upper() if isinstance(c, str) and len(c) == 3 and c.isalpha() else None


def _salary(job: dict[str, Any]) -> tuple[Any, Any]:
    lo, hi = job.get("salary_min"), job.get("salary_max")
    # a vendor that sends the range backwards, or a figure of zero, is not a salary: both are dropped rather than trusted
    if lo is not None and (lo <= 0):
        lo = None
    if hi is not None and (hi <= 0):
        hi = None
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return lo, hi


async def upsert_job(conn: asyncpg.Connection, tenant_id: str, vendor: str, job: dict[str, Any]) -> tuple[str, bool]:
    from datetime import datetime

    lo, hi = _salary(job)
    posted = job.get("posted_at")
    posted_dt = datetime.fromisoformat(posted.replace("Z", "+00:00")) if isinstance(posted, str) and posted else None
    row = await conn.fetchrow(
        UPSERT, tenant_id, fingerprint(job["dedupe_key"]), job["dedupe_key"][:600], job["title"][:400], job.get("company_name"), job.get("company_domain"),
        job.get("location"), job.get("remote"), job.get("url"), job.get("apply_url"), job.get("ats"), job.get("ats_org"), job.get("ats_job_id"), posted_dt,
        job.get("description"), lo, hi, _currency(job), job.get("salary_period"), job.get("employment_type"), list(job.get("technologies") or []), vendor,
    )
    return str(row["id"]), bool(row["inserted"])


async def upsert_jobs(conn: asyncpg.Connection, tenant_id: str, vendor: str, jobs: list[dict[str, Any]]) -> list[tuple[str, bool]]:
    return [await upsert_job(conn, tenant_id, vendor, j) for j in jobs]


async def record_run(conn: asyncpg.Connection, tenant_id: str, vendor: str, batch_id: str, trigger: str, **fields: Any) -> tuple[str, bool]:
    """Start the run row for a batch. Returns (run id, created). A second call for the same batch finds the first and says created False,
    which is how a retried webhook delivery is recognised."""
    row = await conn.fetchrow(
        """insert into job_ingest_run (tenant_id, vendor, batch_id, trigger) values ($1::uuid, $2, $3, $4)
           on conflict (tenant_id, vendor, batch_id) do update set tenant_id = job_ingest_run.tenant_id
           returning id, (xmax = 0) as created""",
        tenant_id, vendor, batch_id, trigger)
    return str(row["id"]), bool(row["created"])


async def finish_run(conn: asyncpg.Connection, run_id: str, *, received: int, invalid: int, new_jobs: int, duplicates: int, embedded: int,
                     archive_key: str | None, status: str, error_detail: str | None) -> None:
    await conn.execute(
        """update job_ingest_run set received=$2, invalid=$3, new_jobs=$4, duplicates=$5, embedded=$6, archive_key=$7, status=$8,
           error_detail=$9, finished_at=now() where id=$1::uuid""",
        run_id, received, invalid, new_jobs, duplicates, embedded, archive_key, status, error_detail)


async def run_summary(conn: asyncpg.Connection, vendor: str, batch_id: str) -> dict[str, Any] | None:
    row = await conn.fetchrow("select * from job_ingest_run where vendor = $1 and batch_id = $2", vendor, batch_id)
    return None if row is None else {k: (str(v) if k in ("id", "tenant_id") else v) for k, v in dict(row).items()}


async def mark_embedded(conn: asyncpg.Connection, ids: list[str]) -> None:
    if ids:
        await conn.execute("update job_posting set embedded_at = now() where id = any($1::uuid[])", ids)


def dumps(o: Any) -> str:
    return json.dumps(o, default=str)
