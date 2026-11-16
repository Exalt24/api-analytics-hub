"""ingest_batch against the real things: Postgres as the request role, Redis, Qdrant, S3 compatible storage (SeaweedFS), and a stand-in OpenAI server for
the embeddings. Every case drops and recreates the `public` schema of ANALYTICS_TEST_DSN, so aim it at a scratch database."""
from __future__ import annotations

import uuid

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("redis")
pytest.importorskip("qdrant_client")

import redis as redis_lib  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402

from app.db import Database  # noqa: E402
from app.jobs.archive import ArchiveError, RawArchive  # noqa: E402
from app.jobs.dedupe import SlidingWindowDedupe  # noqa: E402
from app.jobs.embed import JobIndex, make_embeddings  # noqa: E402
from app.jobs.fake_openai import FakeOpenAI  # noqa: E402
from app.jobs.pipeline import JobPipeline  # noqa: E402
from jobs_fixtures import DSN, QDRANT_URL, REDIS_URL, add_tenant, job, make_bucket, needs, reset_schema, run  # noqa: E402

pytestmark = needs("postgres", "redis", "qdrant", "s3")


class Rig:
    pass


def rig(*, embed_key: str = "sk-test", redis_url: str = REDIS_URL, bucket: str | None = None, window: float = 3600.0) -> Rig:
    r = Rig()
    reset_schema()
    r.a, r.b = add_tenant("tenant-a"), add_tenant("tenant-b")
    r.bucket = bucket or "jobs-%s" % uuid.uuid4().hex[:8]
    r.s3 = make_bucket(r.bucket)
    r.fake = FakeOpenAI()
    r.fake.__enter__()
    r.collection = "jobs_%s" % uuid.uuid4().hex[:8]
    r.qdrant = QdrantClient(url=QDRANT_URL)
    emb = make_embeddings(api_key=embed_key, base_url=r.fake.url + "/v1", dimensions=64)
    r.index = JobIndex(r.qdrant, emb, collection=r.collection)
    r.prefix = "test:pipe:%s" % uuid.uuid4().hex[:6]
    r.dedupe = SlidingWindowDedupe(redis_lib.Redis.from_url(redis_url, socket_connect_timeout=0.3), window_seconds=window, prefix=r.prefix)
    r.archive = RawArchive(r.s3, r.bucket)
    return r


def teardown(r: Rig) -> None:
    r.fake.__exit__(None, None, None)
    try:
        r.qdrant.delete_collection(r.collection)
    except Exception:
        pass
    c = redis_lib.Redis.from_url(REDIS_URL)
    for k in c.scan_iter(r.prefix + "*"):
        c.delete(k)


def with_pipe(r: Rig, fn):
    async def go():
        db = Database(DSN, max_size=3)
        await db.connect()
        try:
            return await fn(db, JobPipeline(db, archive=r.archive, dedupe=r.dedupe, index=r.index))
        finally:
            await db.close()

    return run(go())


async def rows(db: Database, tenant: str, sql: str = "select * from job_posting order by title") -> list[dict]:
    async with db.tenant(tenant) as conn:
        return [dict(x) for x in await conn.fetch(sql)]


@pytest.fixture()
def r():
    x = rig()
    yield x
    teardown(x)


def test_a_batch_is_archived_stored_deduped_and_embedded(r):
    raw = {"data": [{"id": i} for i in range(5)]}
    jobs = [job(i) for i in range(5)]

    async def go(db, pipe):
        res = await pipe.ingest_batch(r.a, "theirstack", "b1", raw, jobs)
        return res, await rows(db, r.a), await rows(db, r.a, "select * from job_ingest_run")

    res, stored, runs = with_pipe(r, go)
    assert (res.received, res.new_jobs, res.duplicates, res.invalid, res.embedded, res.status) == (5, 5, 0, 0, 5, "ok")
    assert len(stored) == 5 and all(s["embedded_at"] is not None for s in stored)
    assert RawArchive(r.s3, r.bucket).get_batch(res.archive_key) == raw  # the original, byte for byte after the gzip round trip
    assert r.index.count(r.a) == 5
    assert runs[0]["status"] == "ok" and runs[0]["archive_key"] == res.archive_key and runs[0]["finished_at"] is not None


def test_the_same_batch_twice_is_the_same_answer_and_costs_nothing(r):
    async def go(db, pipe):
        first = await pipe.ingest_batch(r.a, "hirebase", "same", {"x": 1}, [job(i) for i in range(3)])
        calls = len(r.fake.embedding_requests)
        second = await pipe.ingest_batch(r.a, "hirebase", "same", {"x": 1}, [job(i) for i in range(3)])
        return first, second, calls, len(r.fake.embedding_requests), await rows(db, r.a)

    first, second, before, after, stored = with_pipe(r, go)
    assert second.replayed and (second.new_jobs, second.received) == (first.new_jobs, first.received)
    assert before == after, "a replayed batch must not call the embedding service"
    assert len(stored) == 3 and all(s["seen_count"] == 1 for s in stored)


def test_the_same_vendor_repeating_a_job_inside_the_window_skips_the_database(r):
    async def go(db, pipe):
        await pipe.ingest_batch(r.a, "theirstack", "w1", {}, [job(1), job(2)])
        res = await pipe.ingest_batch(r.a, "theirstack", "w2", {}, [job(1), job(2), job(3)])
        return res, await rows(db, r.a)

    res, stored = with_pipe(r, go)
    assert (res.new_jobs, res.duplicates) == (1, 2)
    assert len(stored) == 3 and {s["seen_count"] for s in stored} == {1}


def test_a_second_vendor_reporting_the_job_is_recorded_on_the_one_row(r):
    async def go(db, pipe):
        await pipe.ingest_batch(r.a, "theirstack", "v1", {}, [job(1)])
        calls = len(r.fake.embedding_requests)
        res = await pipe.ingest_batch(r.a, "hirebase", "v2", {}, [job(1)])
        return res, calls, len(r.fake.embedding_requests), await rows(db, r.a)

    res, before, after, stored = with_pipe(r, go)
    assert len(stored) == 1 and stored[0]["seen_count"] == 2 and stored[0]["vendors"] == ["hirebase", "theirstack"] and stored[0]["first_vendor"] == "theirstack"
    assert res.new_jobs == 0 and before == after, "a second sighting is not a new job and must not be embedded again"


def test_the_same_job_twice_in_one_batch_is_one_row(r):
    async def go(db, pipe):
        res = await pipe.ingest_batch(r.a, "apify", "dup", {}, [job(1), job(1), job(2)])
        return res, await rows(db, r.a)

    res, stored = with_pipe(r, go)
    assert (res.new_jobs, res.duplicates) == (2, 1) and len(stored) == 2


def test_tenants_never_see_each_other_in_the_database_the_window_or_the_vector_search(r):
    async def go(db, pipe):
        await pipe.ingest_batch(r.a, "theirstack", "a1", {}, [job(1, company="Acme", title="Quantum Cheese Engineer")])
        res_b = await pipe.ingest_batch(r.b, "theirstack", "b1", {}, [job(1, company="Acme", title="Quantum Cheese Engineer")])
        return res_b, await rows(db, r.a), await rows(db, r.b)

    res_b, a_rows, b_rows = with_pipe(r, go)
    assert res_b.new_jobs == 1, "the same job for another tenant is new: windows are per tenant"
    assert len(a_rows) == 1 and len(b_rows) == 1 and a_rows[0]["id"] != b_rows[0]["id"]
    hits_a = r.index.search(r.a, "Quantum Cheese Engineer", k=10)
    assert len(hits_a) == 1 and hits_a[0][0]["job_id"] == str(a_rows[0]["id"])
    assert {h[0]["job_id"] for h in r.index.search(r.b, "Quantum Cheese Engineer", k=10)} == {str(b_rows[0]["id"])}
    with pytest.raises(ValueError):
        r.index.search("", "anything")


def test_unreadable_jobs_are_counted_and_never_stored(r):
    bad = [{"title": "", "dedupe_key": "x"}, {"title": "No key"}, "not a dict", None]

    async def go(db, pipe):
        res = await pipe.ingest_batch(r.a, "apify", "bad", {}, [job(1), *bad], invalid_in_normaliser=2)
        return res, await rows(db, r.a)

    res, stored = with_pipe(r, go)
    assert (res.received, res.invalid, res.new_jobs) == (1 + len(bad) + 2, len(bad) + 2, 1) and len(stored) == 1


def test_salary_and_empty_values_are_cleaned_and_a_later_sighting_never_overwrites_with_less(r):
    odd = job(1)
    odd.update(salary_min=150000, salary_max=100000, salary_currency="usd")
    thin = job(1)
    thin.update(description=None, location=None, salary_min=0, salary_max=None, salary_currency=None, url=None, apply_url=None)

    async def go(db, pipe):
        await pipe.ingest_batch(r.a, "theirstack", "s1", {}, [odd])
        await pipe.ingest_batch(r.a, "hirebase", "s2", {}, [thin])
        return await rows(db, r.a)

    (row,) = with_pipe(r, go)
    assert (float(row["salary_min"]), float(row["salary_max"]), row["salary_currency"]) == (100000.0, 150000.0, "USD")
    assert row["description"] and row["location"] == "Remote" and row["apply_url"]


def test_redis_down_means_no_window_and_the_database_still_makes_one_row(r):
    broken = rig(redis_url="redis://127.0.0.1:1/0")
    try:
        async def go(db, pipe):
            first = await pipe.ingest_batch(broken.a, "theirstack", "d1", {}, [job(1)])
            second = await pipe.ingest_batch(broken.a, "theirstack", "d2", {}, [job(1)])
            return first, second, await rows(db, broken.a)

        first, second, stored = with_pipe(broken, go)
        assert "dedupe_unavailable" in first.notes and "dedupe_unavailable" in second.notes
        assert len(stored) == 1 and stored[0]["seen_count"] == 2
    finally:
        teardown(broken)


def test_an_embedding_failure_stores_the_rows_marks_the_batch_partial_and_the_next_call_finishes_it():
    bad = rig(embed_key="sk-wrong")
    try:
        async def go(db, pipe):
            res = await pipe.ingest_batch(bad.a, "theirstack", "e1", {}, [job(1), job(2)])
            stored = await rows(db, bad.a)
            bad.index._emb.openai_api_key = None  # noqa: SLF001 - swap in the working key through a fresh index below
            return res, stored

        res, stored = with_pipe(bad, go)
        assert res.status == "partial" and "embedding_failed" in res.notes and res.embedded == 0
        assert len(stored) == 2 and all(s["embedded_at"] is None for s in stored)

        good = make_embeddings(api_key="sk-test", base_url=bad.fake.url + "/v1", dimensions=64)
        index2 = JobIndex(bad.qdrant, good, collection=bad.collection)

        async def again(db):
            pipe = JobPipeline(db, archive=bad.archive, dedupe=bad.dedupe, index=index2)
            res2 = await pipe.ingest_batch(bad.a, "theirstack", "e1", {}, [job(1), job(2)])
            return res2, await rows(db, bad.a)

        async def runner():
            db = Database(DSN, max_size=3)
            await db.connect()
            try:
                return await again(db)
            finally:
                await db.close()

        res2, stored2 = run(runner())
        assert res2.status == "ok" and res2.embedded == 2 and all(s["embedded_at"] is not None for s in stored2)
        assert index2.count(bad.a) == 2
    finally:
        teardown(bad)


def test_an_archive_that_refuses_the_write_stops_the_batch_before_any_row_is_written(r):
    from app.jobs.archive import client_from_env
    from jobs_fixtures import S3_ENDPOINT

    refused = client_from_env({"JOBS_S3_ENDPOINT": S3_ENDPOINT, "JOBS_S3_ACCESS_KEY": "webtestaccesskey", "JOBS_S3_SECRET_KEY": "the-wrong-secret", "JOBS_S3_REGION": "us-east-1"})
    r.archive = RawArchive(refused, r.bucket)

    async def go(db, pipe):
        with pytest.raises(ArchiveError):
            await pipe.ingest_batch(r.a, "theirstack", "x1", {}, [job(1)])
        return await rows(db, r.a), await rows(db, r.a, "select status, archive_key from job_ingest_run")

    stored, runs = with_pipe(r, go)
    assert stored == [] and runs[0]["status"] == "failed" and runs[0]["archive_key"] is None


def test_the_archive_key_is_the_same_for_the_same_batch_and_a_tampered_object_is_refused(r):
    arch = RawArchive(r.s3, r.bucket)
    k1 = arch.put_batch(r.a, "theirstack", "k", {"a": 1})
    k2 = arch.put_batch(r.a, "theirstack", "k", {"a": 1})
    assert k1 == k2 and arch.get_batch(k1) == {"a": 1}
    r.s3.put_object(Bucket=r.bucket, Key=k1, Body=b"\x1f\x8b not the original", Metadata={"sha256": "0" * 64})
    with pytest.raises(ArchiveError):
        arch.get_batch(k1)


def test_eight_vendors_reporting_overlapping_jobs_at_the_same_moment_never_deadlock_and_lose_nothing(r):
    """n8n sends the vendors in parallel. Batches that share jobs and are written in different orders used to deadlock on the shared rows (found by the n8n run)."""
    import asyncio
    import random

    rng = random.Random(7)
    universe = list(range(30))
    plans = []
    for v in range(8):
        picks = rng.sample(universe, 20)
        rng.shuffle(picks)
        plans.append(("vendor%d" % v, picks))

    async def go():
        db = Database(DSN, max_size=10)
        await db.connect()
        try:
            pipe = JobPipeline(db, archive=r.archive, dedupe=r.dedupe, index=r.index)
            results = []
            for rnd in range(3):  # three rounds: a deadlock is a matter of timing, so one lucky pass proves little
                again = [(v, rng.sample(picks, len(picks))) for v, picks in plans] if rnd else plans
                results += await asyncio.gather(*[pipe.ingest_batch(r.a, v, "c%d-%s" % (rnd, v), {}, [job(i) for i in picks]) for v, picks in again])
            return results, await rows(db, r.a)
        finally:
            await db.close()

    results, stored = run(go())
    assert all(x.status == "ok" for x in results), [(x.vendor, x.status, x.notes) for x in results if x.status != "ok"]
    want = {i for _, picks in plans for i in picks}
    assert len(stored) == len(want) and sum(x.new_jobs for x in results) == len(want) and all(s['embedded_at'] is not None for s in stored)
    reporters = {i: sum(1 for _, picks in plans if i in picks) for i in want}
    by_title = {s["title"]: s for s in stored}
    assert all(by_title["Data Engineer %d" % i]["seen_count"] == n and len(by_title["Data Engineer %d" % i]["vendors"]) == n for i, n in reporters.items())
