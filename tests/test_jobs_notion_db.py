"""The Notion board for the review queue, against the stand-in written from Notion's reference pages, and with the hub's own table as the queue:
a page for each open question, marked when answered, and a board that is down or refusing never loses, answers or blocks a task."""
from __future__ import annotations

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("aio_pika")

from app.connectors.fake_http import Fault  # noqa: E402
from app.db import Database  # noqa: E402
from app.jobs import hitl  # noqa: E402
from app.jobs.fake_ats import FakeATS  # noqa: E402
from app.jobs.fake_notion import FakeNotion  # noqa: E402
from app.jobs.notion import NotionBoard, NotionError  # noqa: E402
from jobs_fixtures import APPLICANT, DSN, add_tenant, needs, reset_schema, run  # noqa: E402

pytestmark = needs("postgres")


def board_for(n: FakeNotion, **kw) -> NotionBoard:
    return NotionBoard("secret_test", "ds-0001", base_url=n.url, sleep=lambda s: None, **kw)


def test_a_page_is_made_with_the_documented_shape_and_marked_when_answered():
    with FakeNotion() as n:
        b = board_for(n)
        pid = b.post_review("run-1", "Why do you want to work here?", 0.0, "no rule matched", None)
        page = n.pages[pid]
        assert page["parent"] == {"type": "data_source_id", "data_source_id": "ds-0001"}
        assert page["properties"]["Name"]["title"][0]["text"]["content"].startswith("Why do you want")
        assert page["properties"]["Status"]["select"]["name"] == "Needs an answer" and page["properties"]["Confidence"]["number"] == 0
        b.mark_answered(pid, "Because of the pipelines.")
        assert n.pages[pid]["properties"]["Status"]["select"]["name"] == "Answered" and n.pages[pid]["properties"]["Answer"]["rich_text"][0]["text"]["content"].startswith("Because")
        req = n.requests[0]
        assert req.headers["authorization"] == "Bearer secret_test" and req.headers["notion-version"] == "2026-03-11"


def test_a_long_text_is_cut_to_what_a_rich_text_block_takes_and_an_empty_label_still_makes_a_page():
    with FakeNotion() as n:
        b = board_for(n)
        pid = b.post_review("run-1", "", 0.5, "x" * 5000, "y" * 5000)
        assert len(n.pages[pid]["properties"]["Reason"]["rich_text"][0]["text"]["content"]) <= 1900
        assert n.pages[pid]["properties"]["Name"]["title"][0]["text"]["content"] == "(unlabelled)"


def test_a_429_is_waited_out_by_the_retry_after_it_carries_and_a_wrong_token_or_data_source_is_an_error():
    slept = []
    with FakeNotion() as n:
        n.faults.append(Fault(429, {"object": "error", "code": "rate_limited"}, {"Retry-After": "7"}))
        b = NotionBoard("secret_test", "ds-0001", base_url=n.url, sleep=slept.append)
        assert b.post_review("r", "A question", 0.1, "why", None) in n.pages and slept == [7.0]
        with pytest.raises(NotionError, match="401"):
            NotionBoard("nope", "ds-0001", base_url=n.url).post_review("r", "A", 0.1, "why", None)
        with pytest.raises(NotionError, match="404"):
            NotionBoard("secret_test", "other", base_url=n.url).post_review("r", "A", 0.1, "why", None)
        with pytest.raises(NotionError, match="every attempt"):
            n.faults.extend([Fault(429, {}, {"Retry-After": "0"})] * 3)
            b.post_review("r", "A", 0.1, "why", None)


def drive(w_board):
    reset_schema()
    a = add_tenant("tenant-a")

    async def go():
        db = Database(DSN, max_size=3)
        await db.connect()
        try:
            async with db.tenant(a) as conn:
                for k, v in APPLICANT.items():
                    await hitl.set_fact(conn, a, k, v, source="profile")
            with FakeATS() as ats:
                rid = await hitl.create_run(db, a, ats.url + "/gh/jobs/2")
                resume = {"resume_file": __file__}
                out = await hitl.run_application(db, a, rid, browser={"mode": "local"}, files=resume, submit=True, board=w_board)
                tasks = await hitl.pending_tasks(db, a, rid)
                res = await hitl.resolve_task(db, a, tasks[0]["id"], "Because of the pipelines.", "daniel", board=w_board)
                async with db.tenant(a) as conn:
                    pages = [r["notion_page_id"] for r in await conn.fetch("select notion_page_id from review_task")]
                return out, tasks, res, pages
        finally:
            await db.close()

    return run(go())


def test_a_review_task_gets_a_notion_page_and_the_answer_marks_it():
    with FakeNotion() as n:
        out, tasks, res, pages = drive(board_for(n))
        assert out.state == "waiting_review" and len(tasks) == 1 and res["pending_left"] == 0
        assert len(pages) == 1 and pages[0] in n.pages
        assert n.pages[pages[0]]["properties"]["Status"]["select"]["name"] == "Answered"


def test_a_board_that_refuses_never_stops_the_queue_or_the_answer():
    with FakeNotion() as n:
        bad = NotionBoard("wrong-token", "ds-0001", base_url=n.url, sleep=lambda s: None)
        out, tasks, res, pages = drive(bad)
        assert out.state == "waiting_review" and len(tasks) == 1 and res["pending_left"] == 0
        assert pages == [None] and n.pages == {}
