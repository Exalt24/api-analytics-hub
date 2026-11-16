"""A Browserbase session, as the official `browserbase` SDK drives it, against the stand-in in app/jobs/fake_browserbase.py.

The SDK is the client under test, so the stand-in is held to the SDK's own paths and field names, not to anything written here. What is shown:
the SDK creates one session with the API key header, Playwright connects to the `connect_url` it returns and a real form is filled and submitted
through that remote browser, the session is released when the block ends, including after an error inside it, and a wrong key fails before any
browser starts.
"""
from __future__ import annotations

import pytest

pytest.importorskip("browserbase")
pytest.importorskip("playwright")

from app.jobs import formfill  # noqa: E402
from app.jobs.browser import open_page  # noqa: E402
from app.jobs.fake_ats import FakeATS  # noqa: E402
from app.jobs.fake_browserbase import FakeBrowserbase  # noqa: E402
from jobs_fixtures import APPLICANT, run  # noqa: E402


def test_a_form_is_filled_and_submitted_through_a_browserbase_session_and_the_session_is_released(tmp_path):
    resume = tmp_path / "Sam_Rivera_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% stand-in resume\n")
    kb = {**APPLICANT, "resume_file": str(resume)}

    async def go(bb_url, ats_url):
        async with open_page("browserbase", browserbase_api_key="bb_test_key", browserbase_base_url=bb_url) as op:
            assert op.mode == "browserbase" and op.session_id and op.cdp_url.startswith("ws://")
            await op.page.goto(ats_url + "/gh/jobs/1")
            return op.session_id, await formfill.fill_form(op.page, kb, submit=True)

    with FakeBrowserbase() as bb, FakeATS() as ats:
        sid, out = run(go(bb.url, ats.url))
        creates = [r for r in bb.requests if r.method == "POST" and r.path == "/v1/sessions"]
        releases = [r for r in bb.requests if r.path == "/v1/sessions/%s" % sid]
        assert out.state == "submitted" and ats.submissions[0]["fields"]["first_name"] == "Sam"
        assert len(creates) == 1 and creates[0].headers["x-bb-api-key"] == "bb_test_key"
        assert releases and b"REQUEST_RELEASE" in releases[-1].body
        assert bb.sessions[sid]["status"] == "COMPLETED"


def test_the_session_is_released_even_when_the_work_inside_it_raises():
    async def go(bb_url):
        with pytest.raises(RuntimeError, match="boom"):
            async with open_page("browserbase", browserbase_api_key="bb_test_key", browserbase_base_url=bb_url) as op:
                raise RuntimeError("boom")

    with FakeBrowserbase() as bb:
        run(go(bb.url))
        assert [s["status"] for s in bb.sessions.values()] == ["COMPLETED"]


def test_a_wrong_key_fails_before_a_browser_starts():
    async def go(bb_url):
        async with open_page("browserbase", browserbase_api_key="wrong", browserbase_base_url=bb_url):
            raise AssertionError("no session should have opened")

    with FakeBrowserbase() as bb:
        with pytest.raises(Exception) as err:
            run(go(bb.url))
        assert "401" in str(err.value) or "Unauthorized" in str(err.value) or "authentic" in str(err.value).lower()
        assert bb.sessions == {}


def test_an_unknown_mode_and_a_missing_key_are_refused_up_front():
    async def go(**kw):
        async with open_page(**kw):
            pass

    with pytest.raises(ValueError):
        run(go(mode="selenium"))
    with pytest.raises(ValueError):
        run(go(mode="browserbase"))
