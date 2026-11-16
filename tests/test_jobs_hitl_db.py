"""Pause and resume with the real parts: Postgres as the request role, RabbitMQ, Chromium and the stand-in ATS pages.

What is proved: a run stops when a field is not sure enough and nothing is sent; the person's answer lands in the knowledge base and queues the
resume; a resume message delivered twice, or with a stale token, resumes once or not at all; the same question on the next form fills itself; a
message that keeps failing ends up in the dead letter queue; another tenant can see and answer none of it; and the n8n callback is made.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("aio_pika")
pytest.importorskip("playwright")

from app.db import Database  # noqa: E402
from app.jobs import hitl  # noqa: E402
from app.jobs.fake_ats import FakeATS  # noqa: E402
from jobs_fixtures import AMQP_URL, APPLICANT, DSN, add_tenant, needs, reset_schema, run  # noqa: E402

pytestmark = needs("postgres", "rabbitmq")

WHY = "I like data pipelines that other people depend on."


class Callback:
    """Stands in for the URL of an n8n Wait node: records what is posted to it."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                me.posts.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/resume" % self.server.server_address[1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def resume(tmp_path):
    p = tmp_path / "Sam_Rivera_Resume.pdf"
    p.write_bytes(b"%PDF-1.4\n% stand-in resume\n")
    return {"resume_file": str(p)}


class World:
    pass


def world(**seed_overrides) -> World:
    reset_schema()
    w = World()
    w.a, w.b = add_tenant("tenant-a"), add_tenant("tenant-b")
    w.cb = Callback()
    w.ats = FakeATS().__enter__()
    w.seed = {k: v for k, v in {**APPLICANT, **seed_overrides}.items() if v is not None}
    return w


def finish(w: World) -> None:
    w.ats.__exit__(None, None, None)
    w.cb.close()


async def session(w: World, fn):
    db = Database(DSN, max_size=3)
    await db.connect()
    broker = await hitl.Broker(AMQP_URL).connect()
    ch = broker._ch  # noqa: SLF001
    for name in (hitl.REVIEW_QUEUE, hitl.RESUME_QUEUE, hitl.DEAD_QUEUE):
        await (await ch.declare_queue(name, passive=True)).purge()
    try:
        async with db.tenant(w.a) as conn:
            for k, v in w.seed.items():
                await hitl.set_fact(conn, w.a, k, v, source="profile")
        return await fn(db, broker)
    finally:
        await broker.close()
        await db.close()


async def row(db, tenant, sql, *args):
    async with db.tenant(tenant) as conn:
        return [dict(r) for r in await conn.fetch(sql, *args)]


def go(w, fn):
    return run(session(w, fn))


BROWSER = {"mode": "local"}


def test_a_run_waits_for_a_person_then_resumes_once_with_the_answer_and_wakes_the_waiting_workflow(resume):
    w = world()
    try:
        async def flow(db, broker):
            rid = await hitl.create_run(db, w.a, w.ats.url + "/gh/jobs/2", resume_url=w.cb.url)
            out = await hitl.run_application(db, w.a, rid, browser=BROWSER, files=resume, submit=True, broker=broker)
            run_row = (await row(db, w.a, "select state, resume_token, needs_review, filled from application_run"))[0]
            tasks = await hitl.pending_tasks(db, w.a, rid)
            queued = await broker.depth(hitl.REVIEW_QUEUE)
            return rid, out, run_row, tasks, queued

        async def second(db, broker, rid, tasks):
            res = await hitl.resolve_task(db, w.a, tasks[0]["id"], WHY, "daniel", broker=broker)
            handled = []

            async def handler(msg):
                handled.append(await hitl.resume_run(db, msg, browser=BROWSER, files=resume, submit=True, broker=broker))

            n = await hitl.drain(broker, hitl.RESUME_QUEUE, handler)
            state = (await row(db, w.a, "select state, resume_token from application_run where id = $1::uuid", rid))[0]
            fact = (await row(db, w.a, "select fact_value, source from applicant_fact where fact_key like 'answer.%'"))[0]
            return res, n, handled, state, fact

        holder = {}

        async def both(db, broker):
            rid, out, run_row, tasks, queued = await flow(db, broker)
            holder.update(out=out, run_row=run_row, tasks=tasks, queued=queued, submissions_before=len(w.ats.submissions))
            holder["after"] = await second(db, broker, rid, tasks)

        go(w, both)
        assert holder["out"].state == "waiting_review" and holder["submissions_before"] == 0
        assert holder["run_row"]["state"] == "waiting_review" and holder["run_row"]["resume_token"] and holder["run_row"]["needs_review"] == 1
        assert len(holder["tasks"]) == 1 and holder["tasks"][0]["field_label"].startswith("Why do you want")
        assert holder["queued"] == 1
        res, n, handled, state, fact = holder["after"]
        assert res["queued_to_resume"] and res["pending_left"] == 0 and n == 1 and handled[0].state == "submitted"
        assert state["state"] == "submitted" and state["resume_token"] is None
        assert fact == {"fact_value": WHY, "source": "correction"}
        (sub,) = w.ats.submissions
        assert sub["fields"]["q_why"] == WHY and sub["fields"]["first_name"] == "Sam"
        assert w.cb.posts and w.cb.posts[-1]["state"] == "submitted"
    finally:
        finish(w)


def test_the_same_question_on_the_next_form_fills_itself_from_the_correction(resume):
    w = world(**{"answer.why_do_you_want_to_work_here": WHY})
    try:
        async def flow(db, broker):
            rid = await hitl.create_run(db, w.a, w.ats.url + "/gh/jobs/2")
            return await hitl.run_application(db, w.a, rid, browser=BROWSER, files=resume, submit=True, broker=broker)

        out = go(w, flow)
        assert out.state == "submitted" and w.ats.submissions[0]["fields"]["q_why"] == WHY
    finally:
        finish(w)


def test_a_resume_message_delivered_twice_or_with_a_stale_token_resumes_nothing_more(resume):
    w = world()
    try:
        async def flow(db, broker):
            rid = await hitl.create_run(db, w.a, w.ats.url + "/gh/jobs/2")
            await hitl.run_application(db, w.a, rid, browser=BROWSER, files=resume, submit=True, broker=broker)
            token = (await row(db, w.a, "select resume_token from application_run"))[0]["resume_token"]
            (task,) = await hitl.pending_tasks(db, w.a, rid)
            await hitl.resolve_task(db, w.a, task["id"], WHY, "daniel", broker=None)
            stale = await hitl.resume_run(db, {"tenant_id": w.a, "run_id": rid, "token": "not-the-token"}, browser=BROWSER, files=resume, submit=True)
            first = await hitl.resume_run(db, {"tenant_id": w.a, "run_id": rid, "token": token}, browser=BROWSER, files=resume, submit=True)
            again = await hitl.resume_run(db, {"tenant_id": w.a, "run_id": rid, "token": token}, browser=BROWSER, files=resume, submit=True)
            return stale, first, again

        stale, first, again = go(w, flow)
        assert stale is None and again is None and first.state == "submitted" and len(w.ats.submissions) == 1
    finally:
        finish(w)


def test_a_run_with_two_open_questions_resumes_only_after_both_are_answered(resume):
    seed = dict(APPLICANT)
    w = world(phone=None)
    try:
        async def flow(db, broker):
            rid = await hitl.create_run(db, w.a, w.ats.url + "/gh/jobs/2")
            await hitl.run_application(db, w.a, rid, browser=BROWSER, files=resume, submit=True, broker=broker)
            tasks = await hitl.pending_tasks(db, w.a, rid)
            r1 = await hitl.resolve_task(db, w.a, tasks[0]["id"], "answer one", "daniel", broker=broker)
            depth1 = await broker.depth(hitl.RESUME_QUEUE)
            r2 = await hitl.resolve_task(db, w.a, tasks[1]["id"], "+15555550999", "daniel", broker=broker, promote_to_key="phone")
            depth2 = await broker.depth(hitl.RESUME_QUEUE)
            with pytest.raises(LookupError):
                await hitl.resolve_task(db, w.a, tasks[1]["id"], "again", "daniel", broker=broker)
            with pytest.raises(ValueError):
                await hitl.resolve_task(db, w.a, tasks[0]["id"], "   ", "daniel")
            return len(tasks), r1, depth1, r2, depth2, {t["proposed_key"] for t in tasks}

        n, r1, d1, r2, d2, keys = go(w, flow)
        assert n == 2 and r1["pending_left"] == 1 and not r1["queued_to_resume"] and d1 == 0
        assert r2["pending_left"] == 0 and r2["queued_to_resume"] and d2 == 1
        assert "phone" in keys and seed
    finally:
        finish(w)


def test_sending_a_run_to_manual_review_ends_it_without_submitting_and_notifies(resume):
    w = world()
    try:
        async def flow(db, broker):
            rid = await hitl.create_run(db, w.a, w.ats.url + "/gh/jobs/2", resume_url=w.cb.url)
            await hitl.run_application(db, w.a, rid, browser=BROWSER, files=resume, submit=True, broker=broker)
            await hitl.send_to_manual(db, w.a, rid, "daniel", "needs a call with the recruiter")
            token_gone = await hitl.take_resume_token(db, w.a, rid, "anything")
            return (await row(db, w.a, "select state, resume_token, detail from application_run"))[0], (await row(db, w.a, "select status from review_task"))[0], token_gone

        run_row, task, token_gone = go(w, flow)
        assert run_row["state"] == "manual" and run_row["resume_token"] is None and task["status"] == "manual" and not token_gone
        assert w.ats.submissions == [] and w.cb.posts[-1]["state"] == "manual"
    finally:
        finish(w)


def test_another_tenant_can_neither_see_nor_answer_the_review_queue(resume):
    w = world()
    try:
        async def flow(db, broker):
            rid = await hitl.create_run(db, w.a, w.ats.url + "/gh/jobs/2")
            await hitl.run_application(db, w.a, rid, browser=BROWSER, files=resume, submit=True, broker=broker)
            (task,) = await hitl.pending_tasks(db, w.a, rid)
            seen_by_b = await hitl.pending_tasks(db, w.b)
            with pytest.raises(LookupError):
                await hitl.resolve_task(db, w.b, task["id"], "I am tenant B", "mallory")
            return seen_by_b, (await row(db, w.a, "select status from review_task"))[0], await row(db, w.b, "select * from applicant_fact where source = 'correction'")

        seen_by_b, task_row, b_facts = go(w, flow)
        assert seen_by_b == [] and task_row["status"] == "pending" and b_facts == []
    finally:
        finish(w)


def test_a_submission_the_form_rejects_is_a_failed_run_with_a_task_and_a_callback(resume):
    w = world()
    try:
        async def flow(db, broker):
            rid = await hitl.create_run(db, w.a, w.ats.url + "/gh/jobs/3", resume_url=w.cb.url)
            await hitl.run_application(db, w.a, rid, browser=BROWSER, files=resume, submit=True, broker=broker)
            return (await row(db, w.a, "select state, detail from application_run"))[0], await row(db, w.a, "select reason, status from review_task")

        state, tasks = go(w, flow)
        assert state["state"] == "failed" and "Something went wrong" in state["detail"]
        assert tasks and tasks[0]["reason"] == "the form rejected the submission" and w.cb.posts[-1]["state"] == "failed" and w.ats.submissions == []
    finally:
        finish(w)


def test_a_message_that_keeps_failing_goes_to_the_dead_letter_queue_after_three_tries():
    w = world()
    try:
        async def flow(db, broker):
            await broker.publish(hitl.RESUME_QUEUE, {"tenant_id": w.a, "run_id": "x", "token": "t"}, message_id="boom")
            calls = []

            async def handler(msg):
                calls.append(1)
                raise RuntimeError("the browser would not start")

            await hitl.drain(broker, hitl.RESUME_QUEUE, handler)
            return len(calls), await broker.depth(hitl.RESUME_QUEUE), await broker.depth(hitl.DEAD_QUEUE)

        calls, main, dead = go(w, flow)
        assert calls == hitl.MAX_ATTEMPTS and main == 0 and dead == 1
    finally:
        finish(w)


def test_a_token_cannot_exist_on_a_run_that_is_not_waiting():
    w = world()
    try:
        async def flow(db, broker):
            rid = await hitl.create_run(db, w.a, w.ats.url + "/gh/jobs/1")
            async with db.tenant(w.a) as conn:
                with pytest.raises(Exception, match="token_iff_waiting"):
                    await conn.execute("update application_run set resume_token = 'x' where id = $1::uuid", rid)

        go(w, flow)
    finally:
        finish(w)
