"""Human in the loop for the application filler: a review queue in Postgres, pause and resume that survive a restart, RabbitMQ messages between the
parts, and a callback that wakes an n8n Wait node.

THE STATE MACHINE (application_run.state): queued -> filling -> waiting_review -> filling -> submitted | failed | manual.
  * `run_application` fills the form. If any field is under the confidence threshold it writes one review_task per field, moves the run to
    `waiting_review` and stores a one-time resume token (the table's CHECK ties the token to that state, so a run that is not waiting has none).
  * A person answers a task (`resolve_task`). The answer goes to the knowledge base under the exact question (`answer.<slug>`), so the same
    question on the next form fills itself at 0.99. When the last pending task of a run is answered, a message goes to `apply.resume`.
  * The consumer calls `resume_run`, which takes the token in one UPDATE (`... where resume_token = $2 and state = 'waiting_review'`), so a message
    delivered twice resumes the run once, and a stale token resumes nothing. It then opens a NEW browser session and fills again, now with the answers.
  * A task nobody can answer is sent to manual review (`send_to_manual`): the run ends in `manual` and nothing is submitted.

MESSAGING. RabbitMQ carries `apply.review` (a run needs a person) and `apply.resume` (a run may continue). Both queues are durable, messages are
persistent, the consumer acknowledges by hand and a message that fails three times goes to the dead letter queue `apply.resume.dead` rather than
looping. The n8n side: a run created with a `resume_url` (n8n's `$execution.resumeUrl`) gets a POST there when it stops waiting, which is what lets
a Wait node continue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import asdict
from typing import Any, Awaitable, Callable

import aio_pika
import httpx

from app.db import Database

from . import formfill
from .browser import open_page

log = logging.getLogger("hub.jobs")

REVIEW_QUEUE = "apply.review"
RESUME_QUEUE = "apply.resume"
DEAD_QUEUE = "apply.resume.dead"
MAX_ATTEMPTS = 3


class Broker:
    """The three queues and the two operations the pipeline needs: publish, and consume with a retry limit."""

    def __init__(self, url: str):
        self._url = url
        self._conn: aio_pika.abc.AbstractRobustConnection | None = None
        self._ch: aio_pika.abc.AbstractChannel | None = None

    async def connect(self) -> "Broker":
        self._conn = await aio_pika.connect_robust(self._url, timeout=10)
        self._ch = await self._conn.channel()
        await self._ch.set_qos(prefetch_count=1)
        dlx = await self._ch.declare_exchange("apply.dlx", aio_pika.ExchangeType.DIRECT, durable=True)
        dead = await self._ch.declare_queue(DEAD_QUEUE, durable=True)
        await dead.bind(dlx, routing_key=RESUME_QUEUE)
        await self._ch.declare_queue(REVIEW_QUEUE, durable=True)
        await self._ch.declare_queue(RESUME_QUEUE, durable=True, arguments={"x-dead-letter-exchange": "apply.dlx", "x-dead-letter-routing-key": RESUME_QUEUE})
        return self

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    async def publish(self, queue: str, body: dict[str, Any], *, message_id: str | None = None, attempts: int = 0) -> None:
        assert self._ch is not None
        msg = aio_pika.Message(json.dumps(body).encode("utf-8"), delivery_mode=aio_pika.DeliveryMode.PERSISTENT, content_type="application/json",
                               message_id=message_id, headers={"attempts": attempts})
        await self._ch.default_exchange.publish(msg, routing_key=queue)

    async def consume_one(self, queue: str, handler: Callable[[dict[str, Any]], Awaitable[None]], *, timeout: float = 5.0) -> bool:
        """Take one message if there is one, run the handler, acknowledge by hand. A failure republishes with attempts + 1, and at the limit
        the message is rejected without requeue, which sends it to the dead letter queue. Returns whether a message was taken."""
        assert self._ch is not None
        q = await self._ch.declare_queue(queue, passive=True)
        msg = await q.get(timeout=timeout, fail=False)
        if msg is None:
            return False
        attempts = int((msg.headers or {}).get("attempts", 0))
        try:
            await handler(json.loads(msg.body))
            await msg.ack()
        except Exception as exc:
            log.warning("a queued message failed", extra={"event": "jobs_message_failed", "queue": queue, "attempt": attempts + 1, "error": str(exc)[:200]})
            if attempts + 1 >= MAX_ATTEMPTS:
                await msg.reject(requeue=False)
            else:
                await self.publish(queue, json.loads(msg.body), message_id=msg.message_id, attempts=attempts + 1)
                await msg.ack()
        return True

    async def depth(self, queue: str) -> int:
        assert self._ch is not None
        # a passive declare through the robust channel returns the object it cached at the first declare, with a stale count, so ask the
        # underlying channel, which asks the broker every time
        underlay = await self._ch.get_underlay_channel()
        return int((await underlay.queue_declare(queue, passive=True)).message_count)


async def load_kb(conn, applicant: str = "default") -> dict[str, str]:
    rows = await conn.fetch("select fact_key, fact_value from applicant_fact where applicant = $1", applicant)
    return {r["fact_key"]: r["fact_value"] for r in rows}


async def set_fact(conn, tenant_id: str, key: str, value: str, *, source: str, applicant: str = "default") -> None:
    await conn.execute(
        """insert into applicant_fact (tenant_id, applicant, fact_key, fact_value, source) values ($1::uuid, $2, $3, $4, $5)
           on conflict (tenant_id, applicant, fact_key) do update set fact_value = excluded.fact_value, source = excluded.source, updated_at = now()""",
        tenant_id, applicant, key, value, source)


async def create_run(db: Database, tenant_id: str, apply_url: str, *, job_id: str | None = None, resume_url: str | None = None) -> str:
    async with db.tenant(tenant_id) as conn:
        return str(await conn.fetchval("insert into application_run (tenant_id, job_id, apply_url, resume_url) values ($1::uuid, $2::uuid, $3, $4) returning id",
                                       tenant_id, job_id, apply_url, resume_url))


async def notify_resume_url(url: str | None, payload: dict[str, Any]) -> bool:
    """Wake an n8n Wait node (or any waiting caller). Failure is logged and reported, never raised: the run's state is already saved."""
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=payload)
        return r.status_code < 300
    except httpx.HTTPError as exc:
        log.warning("resume callback failed", extra={"event": "jobs_resume_callback_failed", "error": str(exc)[:200]})
        return False


async def run_application(db: Database, tenant_id: str, run_id: str, *, browser: dict[str, Any], files: dict[str, str] | None = None, submit: bool = False,
                          broker: Broker | None = None, suggest=None, discover_factory=None, board=None,
                          typing_delay: tuple[int, int] | None = None) -> formfill.Outcome:
    """Open a fresh browser session, fill the form for `run_id`, and move the run to where the outcome says. `browser` is open_page's keyword arguments
    plus `mode`; `files` maps resume_file and cover_letter_file to paths."""
    async with db.tenant(tenant_id) as conn:
        run = await conn.fetchrow("select apply_url, resume_url from application_run where id = $1::uuid", run_id)
        if run is None:
            raise LookupError("no such run")
        await conn.execute("update application_run set state='filling', updated_at=now() where id=$1::uuid", run_id)
        kb = await load_kb(conn)
    kb.update(files or {})
    mode = browser.get("mode", "local")
    opts = {k: v for k, v in browser.items() if k != "mode"}
    async with open_page(mode, **opts) as op:
        await op.page.goto(run["apply_url"])
        discover = discover_factory(op) if discover_factory else None
        outcome = await formfill.fill_form(op.page, kb, submit=submit, suggest=suggest, discover=discover, typing_delay=typing_delay)
    token = secrets.token_urlsafe(24) if outcome.state == "waiting_review" else None
    async with db.tenant(tenant_id) as conn:
        for r in outcome.reviews:
            page_id = None
            if board is not None:
                try:  # a board that is down never stops the queue: the task is stored without a page and the person finds it in the hub
                    page_id = await asyncio.to_thread(board.post_review, run_id, r.field_label, r.confidence, r.reason, r.proposed_value)
                except Exception as exc:
                    log.warning("the Notion board refused a review page: %s", str(exc)[:200], extra={"event": "jobs_notion_failed"})
            await conn.execute(
                """insert into review_task (tenant_id, run_id, field_label, field_selector, proposed_key, proposed_value, confidence, reason, notion_page_id)
                   values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9)""",
                tenant_id, run_id, r.field_label[:300], r.field_selector, r.proposed_key, r.proposed_value, r.confidence, r.reason[:300], page_id)
        await conn.execute(
            "update application_run set state=$2, resume_token=$3, filled=$4, needs_review=$5, detail=$6, updated_at=now() where id=$1::uuid",
            run_id, outcome.state, token, len(outcome.filled), len(outcome.reviews), outcome.detail[:500] or None)
    if outcome.state == "waiting_review" and broker is not None:
        await broker.publish(REVIEW_QUEUE, {"tenant_id": tenant_id, "run_id": run_id, "needs_review": len(outcome.reviews)}, message_id="review:%s:%s" % (run_id, token))
    if outcome.state in ("submitted", "failed"):
        await notify_resume_url(run["resume_url"], {"run_id": run_id, "state": outcome.state})
    return outcome


async def pending_tasks(db: Database, tenant_id: str, run_id: str | None = None) -> list[dict[str, Any]]:
    async with db.tenant(tenant_id) as conn:
        rows = await conn.fetch(
            "select id::text, run_id::text, field_label, proposed_key, proposed_value, confidence::float, reason, created_at from review_task "
            "where status = 'pending' and ($1::uuid is null or run_id = $1::uuid) order by created_at", run_id)
    return [dict(r) for r in rows]


async def resolve_task(db: Database, tenant_id: str, task_id: str, answer: str, who: str, *, broker: Broker | None = None, promote_to_key: str | None = None,
                       board=None) -> dict[str, Any]:
    """Record a person's answer: it becomes a fact (a correction), the task closes, and when it was the last pending task of its run the run
    is queued to resume. A task already resolved is refused, so a double click cannot write twice."""
    if not answer.strip():
        raise ValueError("an empty answer is not an answer")
    async with db.tenant(tenant_id) as conn:
        row = await conn.fetchrow(
            """update review_task set status='resolved', answer=$2, resolved_by=$3, resolved_at=now()
               where id=$1::uuid and status='pending' returning run_id::text, field_label, notion_page_id""", task_id, answer.strip(), who[:100])
        if row is None:
            raise LookupError("the task does not exist or is already answered")
        await set_fact(conn, tenant_id, "answer." + formfill.slug(row["field_label"]), answer.strip(), source="correction")
        if promote_to_key:
            await set_fact(conn, tenant_id, promote_to_key, answer.strip(), source="correction")
        left = await conn.fetchval("select count(*) from review_task where run_id=$1::uuid and status='pending'", row["run_id"])
        token = await conn.fetchval("select resume_token from application_run where id=$1::uuid", row["run_id"])
    if board is not None and row["notion_page_id"]:
        try:
            await asyncio.to_thread(board.mark_answered, row["notion_page_id"], answer.strip())
        except Exception as exc:
            log.warning("the Notion board could not be updated: %s", str(exc)[:200], extra={"event": "jobs_notion_failed"})
    ready = left == 0 and token is not None
    if ready and broker is not None:
        await broker.publish(RESUME_QUEUE, {"tenant_id": tenant_id, "run_id": row["run_id"], "token": token}, message_id="resume:%s:%s" % (row["run_id"], token))
    return {"run_id": row["run_id"], "pending_left": int(left), "queued_to_resume": bool(ready and broker is not None)}


async def send_to_manual(db: Database, tenant_id: str, run_id: str, who: str, reason: str) -> None:
    async with db.tenant(tenant_id) as conn:
        await conn.execute("update review_task set status='manual', answer=$2, resolved_by=$3, resolved_at=now() where run_id=$1::uuid and status='pending'", run_id, reason[:300], who[:100])
        row = await conn.fetchrow("update application_run set state='manual', resume_token=null, detail=$2, updated_at=now() where id=$1::uuid and state='waiting_review' returning resume_url", run_id, reason[:300])
    if row is not None:
        await notify_resume_url(row["resume_url"], {"run_id": run_id, "state": "manual"})


async def take_resume_token(db: Database, tenant_id: str, run_id: str, token: str) -> bool:
    """Spend the one-time token. True once; False for a stale token or a second delivery of the same message."""
    async with db.tenant(tenant_id) as conn:
        got = await conn.fetchval(
            "update application_run set state='filling', resume_token=null, updated_at=now() where id=$1::uuid and resume_token=$2 and state='waiting_review' returning id",
            run_id, token)
    return got is not None


async def resume_run(db: Database, message: dict[str, Any], **run_kwargs: Any) -> formfill.Outcome | None:
    if not await take_resume_token(db, message["tenant_id"], message["run_id"], message["token"]):
        log.info("resume skipped: the token was already used or is stale", extra={"event": "jobs_resume_skipped", "run": message["run_id"]})
        return None
    return await run_application(db, message["tenant_id"], message["run_id"], **run_kwargs)


def outcome_summary(o: formfill.Outcome) -> dict[str, Any]:
    return {"state": o.state, "filled": len(o.filled), "reviews": [asdict(r) for r in o.reviews], "detail": o.detail, "steps": o.steps}


async def drain(broker: Broker, queue: str, handler, *, limit: int = 50) -> int:
    n = 0
    while n < limit and await broker.consume_one(queue, handler, timeout=1.0):
        n += 1
    return n


__all__ = ["Broker", "REVIEW_QUEUE", "RESUME_QUEUE", "DEAD_QUEUE", "create_run", "run_application", "pending_tasks", "resolve_task", "send_to_manual",
           "take_resume_token", "resume_run", "outcome_summary", "drain", "load_kb", "set_fact", "notify_resume_url"]
