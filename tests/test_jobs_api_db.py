"""The job routes over ASGI against real Postgres and the real RLS, with the other parts as the stand-ins: roles, tenant isolation, the ingest route,
and an application run from the HTTP call through the review queue to a submission."""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from contextlib import AsyncExitStack

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("asyncpg")
httpx = pytest.importorskip("httpx")

import psycopg  # noqa: E402
import redis as redis_lib  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402

from app.api.auth import generate_key, hash_key  # noqa: E402
from app.api.jobs import JobsRuntime  # noqa: E402
from app.jobs import hitl  # noqa: E402
from app.jobs.archive import RawArchive  # noqa: E402
from app.jobs.dedupe import SlidingWindowDedupe  # noqa: E402
from app.jobs.embed import JobIndex, make_embeddings  # noqa: E402
from app.jobs.fake_ats import FakeATS  # noqa: E402
from app.jobs.fake_openai import FakeOpenAI  # noqa: E402
from app.jobs.llm import LLM  # noqa: E402
from app.jobs.pipeline import JobPipeline  # noqa: E402
from jobs_fixtures import AMQP_URL, APPLICANT, DSN, QDRANT_URL, REDIS_URL, add_tenant, job, make_bucket, needs, reset_schema  # noqa: E402

pytestmark = needs("postgres", "redis", "qdrant", "s3", "rabbitmq")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    reset_schema()
    a, b = add_tenant("tenant-a"), add_tenant("tenant-b")
    keys = {"a_viewer": generate_key(), "a_operator": generate_key(), "b_operator": generate_key()}
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        for label, tid, role in (("a_viewer", a, "viewer"), ("a_operator", a, "operator"), ("b_operator", b, "operator")):
            cur.execute("insert into api_key (tenant_id, key_hash, label, role) values (%s,%s,%s,%s)", (tid, hash_key(keys[label]), label, role))
    os.environ["DATABASE_URL"] = DSN
    os.environ.setdefault("DASHBOARD_ORIGIN", "http://localhost:3000")
    for k in ("JOBS_S3_BUCKET",):
        os.environ.pop(k, None)  # the lifespan then leaves the runtime unset, and the test assigns its own

    from app.api.main import app

    loop = asyncio.new_event_loop()
    stack = AsyncExitStack()
    loop.run_until_complete(stack.enter_async_context(app.router.lifespan_context(app)))
    fake = FakeOpenAI()
    fake.__enter__()
    bucket = "jobs-api-%s" % uuid.uuid4().hex[:6]
    s3 = make_bucket(bucket)
    collection = "jobs_api_%s" % uuid.uuid4().hex[:6]
    qdrant = QdrantClient(url=QDRANT_URL)
    index = JobIndex(qdrant, make_embeddings(api_key="sk-test", base_url=fake.url + "/v1", dimensions=64), collection=collection)
    prefix = "test:api:%s" % uuid.uuid4().hex[:6]
    dedupe = SlidingWindowDedupe(redis_lib.Redis.from_url(REDIS_URL), window_seconds=3600, prefix=prefix)
    broker = loop.run_until_complete(hitl.Broker(AMQP_URL).connect())
    for name in (hitl.REVIEW_QUEUE, hitl.RESUME_QUEUE, hitl.DEAD_QUEUE):
        loop.run_until_complete(loop.run_until_complete(broker._ch.declare_queue(name, passive=True)).purge())  # noqa: SLF001
    resume = tmp_path_factory.mktemp("r") / "Sam_Rivera_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% stand-in resume\n")
    ats = FakeATS().__enter__()
    pipeline = JobPipeline(app.state.db, archive=RawArchive(s3, bucket), dedupe=dedupe, index=index)
    app.state.jobs = JobsRuntime(pipeline=pipeline, broker=broker, index=index, files={"resume_file": str(resume)}, submit=True,
                                 llm=LLM(api_key="sk-test", base_url=fake.url + "/v1", attempts=1, sleep=lambda s: None))
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")

    async def facts():
        async with app.state.db.tenant(a) as conn:
            for k, v in APPLICANT.items():
                await hitl.set_fact(conn, a, k, v, source="profile")

    loop.run_until_complete(facts())
    yield {"loop": loop, "c": client, "keys": keys, "a": a, "b": b, "ats": ats, "app": app, "rt": app.state.jobs, "broker": broker, "fake": fake}
    ats.__exit__(None, None, None)
    fake.__exit__(None, None, None)
    loop.run_until_complete(broker.close())
    loop.run_until_complete(client.aclose())
    loop.run_until_complete(stack.aclose())
    loop.close()
    try:
        qdrant.delete_collection(collection)
    except Exception:
        pass


def call(env, method, url, key=None, **kw):
    headers = {"Authorization": "Bearer %s" % key} if key else {}
    return env["loop"].run_until_complete(getattr(env["c"], method)(url, headers=headers, **kw))


BATCH = {"batch_id": "n8n-1", "raw": {"data": [1, 2, 3]}, "jobs": [job(1), job(2), job(3)]}


def test_no_token_is_401_and_a_viewer_may_read_but_not_ingest_or_apply(env):
    assert call(env, "post", "/api/jobs/ingest/theirstack", json=BATCH).status_code == 401
    assert call(env, "post", "/api/jobs/ingest/theirstack", env["keys"]["a_viewer"], json=BATCH).status_code == 403
    assert call(env, "post", "/api/jobs/apply", env["keys"]["a_viewer"], json={"apply_url": "http://x.test/"}).status_code == 403
    assert call(env, "get", "/api/jobs", env["keys"]["a_viewer"]).status_code == 200
    assert call(env, "get", "/api/jobs/review", env["keys"]["a_viewer"]).status_code == 200


def test_an_operator_ingests_a_batch_and_a_retry_of_it_is_the_same_answer(env):
    first = call(env, "post", "/api/jobs/ingest/theirstack", env["keys"]["a_operator"], json=BATCH)
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["new_jobs"], body["received"], body["embedded"], body["status"], body["replayed"]) == (3, 3, 3, "ok", False)
    again = call(env, "post", "/api/jobs/ingest/theirstack", env["keys"]["a_operator"], json=BATCH).json()
    assert again["replayed"] and again["new_jobs"] == 3
    listed = call(env, "get", "/api/jobs?limit=10", env["keys"]["a_viewer"]).json()["jobs"]
    assert len(listed) == 3 and all(j["ats"] == "greenhouse" for j in listed)


def test_an_unknown_vendor_and_a_malformed_body_are_refused(env):
    k = env["keys"]["a_operator"]
    assert call(env, "post", "/api/jobs/ingest/nobody", k, json=BATCH).status_code == 404
    assert call(env, "post", "/api/jobs/ingest/theirstack", k, json={"jobs": []}).status_code == 422
    assert call(env, "post", "/api/jobs/ingest/theirstack", k, json={**BATCH, "trigger": "cron"}).status_code == 422
    assert call(env, "post", "/api/jobs/apply", k, json={"apply_url": "javascript:alert(1)"}).status_code == 422


def test_semantic_search_and_the_list_are_per_tenant(env):
    call(env, "post", "/api/jobs/ingest/hirebase", env["keys"]["b_operator"], json={"batch_id": "b-1", "raw": {}, "jobs": [job(9, company="Zeta", title="Quantum Cheese Engineer")]})
    mine = call(env, "get", "/api/jobs?q=Quantum Cheese Engineer", env["keys"]["a_operator"]).json()["jobs"]
    theirs = call(env, "get", "/api/jobs?q=Quantum Cheese Engineer", env["keys"]["b_operator"]).json()["jobs"]
    assert all(j["company_name"] != "Zeta" for j in mine)
    assert [j["company_name"] for j in theirs] == ["Zeta"]


def poll(env, key, run_id, want, seconds=30):
    end = time.time() + seconds
    state = None
    while time.time() < end:
        state = call(env, "get", "/api/jobs/runs/%s" % run_id, key).json()["state"]
        if state in want:
            return state
        env["loop"].run_until_complete(asyncio.sleep(0.25))
    return state


def test_an_application_run_over_http_pauses_for_an_answer_and_finishes_after_it(env):
    k = env["keys"]["a_operator"]
    before = len(env["ats"].submissions)
    started = call(env, "post", "/api/jobs/apply", k, json={"apply_url": env["ats"].url + "/gh/jobs/2"})
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    assert poll(env, k, run_id, {"waiting_review"}) == "waiting_review"
    assert len(env["ats"].submissions) == before, "nothing is sent while a question is open"
    tasks = call(env, "get", "/api/jobs/review?run_id=%s" % run_id, k).json()["tasks"]
    assert len(tasks) == 1 and tasks[0]["field_label"].startswith("Why do you want")

    other = call(env, "post", "/api/jobs/review/%s/answer" % tasks[0]["id"], env["keys"]["b_operator"], json={"answer": "mine now"})
    assert other.status_code == 409, "another tenant cannot answer it"
    assert call(env, "get", "/api/jobs/runs/%s" % run_id, env["keys"]["b_operator"]).status_code == 404
    assert call(env, "post", "/api/jobs/review/%s/answer" % tasks[0]["id"], env["keys"]["a_viewer"], json={"answer": "x"}).status_code == 403
    assert call(env, "post", "/api/jobs/review/%s/answer" % tasks[0]["id"], k, json={"answer": "  "}).status_code == 422

    done = call(env, "post", "/api/jobs/review/%s/answer" % tasks[0]["id"], k, json={"answer": "I like pipelines other people depend on."})
    assert done.status_code == 200 and done.json()["queued_to_resume"]
    assert call(env, "post", "/api/jobs/review/%s/answer" % tasks[0]["id"], k, json={"answer": "twice"}).status_code == 409

    async def consume():
        async def handler(msg):
            await hitl.resume_run(env["app"].state.db, msg, browser=env["rt"].browser, files=env["rt"].files, submit=True, broker=env["broker"])
        return await hitl.drain(env["broker"], hitl.RESUME_QUEUE, handler)

    assert env["loop"].run_until_complete(consume()) == 1
    assert poll(env, k, run_id, {"submitted"}) == "submitted"
    assert env["ats"].submissions[-1]["fields"]["q_why"] == "I like pipelines other people depend on."


def test_a_waiting_run_can_be_sent_to_manual_review_and_only_by_an_operator_of_its_tenant(env):
    # tenant B has no stored answers, so every required field on the form is a review and the run waits
    k = env["keys"]["b_operator"]
    run_id = call(env, "post", "/api/jobs/apply", k, json={"apply_url": env["ats"].url + "/gh/jobs/2"}).json()["run_id"]
    assert poll(env, k, run_id, {"waiting_review"}) == "waiting_review"
    assert call(env, "post", "/api/jobs/runs/%s/manual" % run_id, env["keys"]["a_viewer"], json={"reason": "needs a call"}).status_code in (403, 404)
    assert call(env, "post", "/api/jobs/runs/%s/manual" % run_id, env["keys"]["a_operator"], json={"reason": "needs a call"}).status_code == 404
    assert call(env, "post", "/api/jobs/runs/%s/manual" % run_id, k, json={"reason": "needs a call"}).json()["state"] == "manual"
    assert call(env, "get", "/api/jobs/runs/%s" % run_id, k).json()["state"] == "manual"


def test_a_server_with_no_pipeline_configured_answers_503(env):
    saved = env["app"].state.jobs
    env["app"].state.jobs = None
    try:
        assert call(env, "post", "/api/jobs/ingest/theirstack", env["keys"]["a_operator"], json=BATCH).status_code == 503
    finally:
        env["app"].state.jobs = saved


def test_a_tailored_resume_and_letter_are_made_stored_and_listed_through_the_routes(env):
    import json

    from test_jobs_tailor import PROFILE, doc_from

    a = env["keys"]["a_operator"]
    call(env, "post", "/api/jobs/ingest/apify", a, json={"batch_id": "t-1", "raw": {}, "jobs": [{**job(21, title="Backend Data Engineer"), "technologies": ["python", "postgresql", "redis"],
                                                                                                    "description": "Python, PostgreSQL and Redis."}]})
    listed = call(env, "get", "/api/jobs?limit=100", env["keys"]["a_viewer"]).json()["jobs"]
    jid = next(j["id"] for j in listed if j["title"] == "Backend Data Engineer")
    env["fake"].chat_script = [json.dumps(doc_from(PROFILE))]
    from dataclasses import asdict

    profile = asdict(PROFILE)
    out = call(env, "post", "/api/jobs/%s/tailor" % jid, a, json={"profile": profile})
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["grounded"] and body["score"] >= 90 and body["cover_letter"] and set(body["keys"]) == {"resume_docx", "resume_pdf", "cover_letter_docx"}
    docs = call(env, "get", "/api/jobs/%s/documents" % jid, env["keys"]["a_viewer"]).json()["documents"]
    assert [d["kind"] for d in docs] == ["cover_letter", "resume"] and docs[1]["score"] == body["score"] and docs[0]["score"] is None
    assert call(env, "post", "/api/jobs/%s/tailor" % jid, env["keys"]["a_viewer"], json={"profile": profile}).status_code == 403
    assert call(env, "post", "/api/jobs/%s/tailor" % jid, env["keys"]["b_operator"], json={"profile": profile}).status_code == 404
    assert call(env, "get", "/api/jobs/%s/documents" % jid, env["keys"]["b_operator"]).json() == {"documents": []}
    assert call(env, "post", "/api/jobs/%s/tailor" % jid, a, json={"profile": {"name": "x"}}).status_code == 422
    assert call(env, "post", "/api/jobs/not-a-uuid/tailor", a, json={"profile": profile}).status_code == 404
