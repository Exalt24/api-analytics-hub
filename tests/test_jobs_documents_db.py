"""Phases 2 and 3 through the database and storage: a tailored resume and cover letter for a stored job, rendered, stored as files and recorded."""
from __future__ import annotations

import json
import uuid

import pytest

pytest.importorskip("asyncpg")

from app.db import Database  # noqa: E402
from app.jobs import documents, render, store  # noqa: E402
from app.jobs.archive import RawArchive  # noqa: E402
from app.jobs.fake_openai import FakeOpenAI  # noqa: E402
from app.jobs.llm import LLM  # noqa: E402
from jobs_fixtures import DSN, add_tenant, job, make_bucket, needs, reset_schema, run  # noqa: E402
from test_jobs_tailor import PROFILE, UNREACHABLE, doc_from  # noqa: E402

pytestmark = needs("postgres", "s3")


class Rig:
    pass


@pytest.fixture()
def rig():
    reset_schema()
    r = Rig()
    r.a, r.b = add_tenant("tenant-a"), add_tenant("tenant-b")
    r.bucket = "docs-%s" % uuid.uuid4().hex[:8]
    r.s3 = make_bucket(r.bucket)
    r.archive = RawArchive(r.s3, r.bucket)
    r.fake = FakeOpenAI()
    r.fake.__enter__()
    r.llm = LLM(api_key="sk-test", base_url=r.fake.url + "/v1", attempts=1, sleep=lambda s: None)
    yield r
    r.fake.__exit__(None, None, None)


def with_db(fn):
    async def go():
        db = Database(DSN, max_size=3)
        await db.connect()
        try:
            return await fn(db)
        finally:
            await db.close()

    return run(go())


async def add_job(db, tenant, j):
    async with db.tenant(tenant) as conn:
        job_id, _ = await store.upsert_job(conn, tenant, "theirstack", j)
    return job_id


def test_a_tailored_resume_and_letter_are_rendered_stored_and_recorded_and_a_rerun_replaces_them(rig):
    covered = {**job(1, title="Backend Data Engineer"), "technologies": ["python", "postgresql", "redis", "docker", "fastapi", "qdrant", "n8n"],
               "description": "Python, PostgreSQL, Redis, Docker, FastAPI, Qdrant and n8n."}
    rig.fake.chat_script = [json.dumps(doc_from(PROFILE))] * 2

    async def go(db):
        jid = await add_job(db, rig.a, covered)
        first = await documents.tailor_and_store(db, rig.archive, rig.a, jid, PROFILE, rig.llm)
        again = await documents.tailor_and_store(db, rig.archive, rig.a, jid, PROFILE, rig.llm)
        async with db.tenant(rig.a) as conn:
            rows = [dict(r) for r in await conn.fetch("select kind, score, grounded, reached_threshold, object_key from tailored_document order by kind")]
        return jid, first, again, rows

    jid, first, again, rows = with_db(go)
    assert first["grounded"] and first["used_model"] and first["cover_letter"] and first["score"] >= 90 and first["reached_threshold"]
    base = "documents/%s/%s/" % (rig.a, jid)
    assert set(first["keys"].values()) == {base + "resume.docx", base + "resume.pdf", base + "cover_letter.docx"}
    docx = rig.s3.get_object(Bucket=rig.bucket, Key=base + "resume.docx")["Body"].read()
    pdf = rig.s3.get_object(Bucket=rig.bucket, Key=base + "resume.pdf")["Body"].read()
    letter = rig.s3.get_object(Bucket=rig.bucket, Key=base + "cover_letter.docx")["Body"].read()
    assert "Sam Rivera" in render.docx_text(docx) and "Northwind Analytics" in render.pdf_text(pdf) and "Dear Acme" in render.docx_text(letter)
    assert [r["kind"] for r in rows] == ["cover_letter", "resume"] and len(rows) == 2, "a rerun replaces the rows, it does not add a second pair"
    by = {r["kind"]: r for r in rows}
    assert by["resume"]["score"] == again["score"] and by["cover_letter"]["score"] is None
    assert all(r["grounded"] for r in rows)


def test_a_job_that_wants_tools_the_facts_lack_is_stored_below_the_threshold_with_the_gap_named(rig):
    rig.fake.chat_script = [json.dumps(doc_from(PROFILE))] * 4
    hard = {**job(2, title="Platform Data Engineer"), "technologies": UNREACHABLE["technologies"], "description": UNREACHABLE["description"]}

    async def go(db):
        jid = await add_job(db, rig.a, hard)
        out = await documents.tailor_and_store(db, rig.archive, rig.a, jid, PROFILE, rig.llm, pdf=False)
        async with db.tenant(rig.a) as conn:
            row = dict(await conn.fetchrow("select score, reached_threshold, grounded from tailored_document where kind = 'resume'"))
        return out, row

    out, row = with_db(go)
    assert not out["reached_threshold"] and out["unsupported"] == ["dbt", "kafka", "kubernetes"] and out["ceiling"] == 55
    assert row["reached_threshold"] is False and row["grounded"] and row["score"] <= 55
    assert "resume_pdf" not in out["keys"]
    text = render.docx_text(rig.s3.get_object(Bucket=rig.bucket, Key=out["keys"]["resume_docx"])["Body"].read()).lower()
    assert not any(t in text for t in out["unsupported"])


def test_another_tenants_job_is_not_found_and_nothing_is_written(rig):
    async def go(db):
        jid = await add_job(db, rig.a, job(3))
        with pytest.raises(LookupError):
            await documents.tailor_and_store(db, rig.archive, rig.b, jid, PROFILE, None)
        with pytest.raises(LookupError):
            await documents.tailor_and_store(db, rig.archive, rig.a, str(uuid.uuid4()), PROFILE, None)
        return rig.s3.list_objects_v2(Bucket=rig.bucket, Prefix="documents/").get("KeyCount", 0)

    assert with_db(go) == 0


def test_a_cover_letter_row_cannot_carry_a_score_and_a_resume_row_must(rig):
    async def go(db):
        jid = await add_job(db, rig.a, job(4))
        async with db.tenant(rig.a) as conn:
            with pytest.raises(Exception, match="only_a_resume_is_scored"):
                await conn.execute("insert into tailored_document (tenant_id, job_id, kind, score, rounds, grounded, reached_threshold) values ($1::uuid, $2::uuid, 'cover_letter', 90, 1, true, true)", rig.a, jid)

    with_db(go)

