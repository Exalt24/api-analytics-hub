"""The form filler: the label to key matcher as a table, and real Chromium runs against the stand-in ATS pages.

What these prove, in the order the risks run: a field is typed only at 0.95 or more; a consent box and a self-identification question are never
answered by a rule; a wrong answer is never given to get a form through (an unknown required question stops the run before anything is sent);
a page whose form cannot be read is never submitted; a submission the page rejects is reported as failed, not as sent; and when everything is
known the values really arrive at the server, with the resume file.
"""
from __future__ import annotations

import os
import pathlib

import pytest

pytest.importorskip("playwright")

from app.jobs import formfill  # noqa: E402
from app.jobs.browser import open_page  # noqa: E402
from app.jobs.fake_ats import FakeATS  # noqa: E402
from jobs_fixtures import APPLICANT, run  # noqa: E402

F = formfill.Field


def kb_with(**extra):
    return {**APPLICANT, **extra}


@pytest.mark.parametrize("field, kb, key, min_conf, max_conf", [
    (F("Email *", "#e", type="email"), {"email": "a@b.c"}, "email", 0.99, 1.0),
    (F("First Name", "#f", autocomplete="given-name"), {"first_name": "Sam"}, "first_name", 0.99, 1.0),
    (F("Phone", "#p", type="tel"), {"phone": "+1"}, "phone", 0.99, 1.0),
    (F("LinkedIn Profile", "#l"), {"linkedin": "https://x"}, "linkedin", 0.98, 0.98),
    (F("Are you legally authorized to work in the United States?", "#w", kind="select", options=["Yes", "No"]), {"work_authorization": "Yes"}, "work_authorization", 0.95, 0.98),
    (F("Do you now or will you in the future require visa sponsorship", "#s", kind="radio", options=["Yes", "No"], option_selectors={"Yes": "#a", "No": "#b"}), {"sponsorship": "no"}, "sponsorship", 0.90, 0.98),
])
def test_a_known_label_maps_to_its_key_with_a_high_confidence(field, kb, key, min_conf, max_conf):
    m = formfill.match_field(field, kb)
    assert m.key == key and m.value is not None and min_conf <= m.confidence <= max_conf, m


def test_a_yes_stored_for_a_select_becomes_that_options_text_and_a_value_with_no_option_drops_the_confidence():
    m = formfill.match_field(F("Are you legally authorized to work in the US?", "#w", kind="select", options=["Yes", "No"]), {"work_authorization": "yes"})
    assert m.value == "Yes"
    odd = formfill.match_field(F("Are you legally authorized to work in the US?", "#w", kind="select", options=["Citizen", "Visa holder"]), {"work_authorization": "Yes"})
    assert odd.confidence == 0.60 and "no option matches" in odd.reason


def test_a_label_about_someone_else_is_never_the_applicants_name():
    for label in ("Company name", "Reference name", "Previous employer", "School name", "Emergency contact name"):
        m = formfill.match_field(F(label, "#x"), kb_with())
        assert m.value is None and m.confidence == 0.0, label


def test_consent_and_self_identification_are_never_answered_by_a_rule():
    consent = formfill.match_field(F("I agree to the privacy policy", "#c", kind="checkbox"), kb_with())
    assert consent.value is None and consent.confidence < formfill.THRESHOLD
    gender = F("Gender", "#g", kind="select", options=["Female", "Male", "Decline to self identify"])
    assert formfill.match_field(gender, kb_with()).confidence == 0.0
    stored = formfill.match_field(gender, kb_with(**{"eeo.gender": "Decline to self identify"}))
    assert stored.value == "Decline to self identify" and stored.confidence >= formfill.THRESHOLD


def test_an_unknown_question_has_no_key_and_an_answered_one_is_remembered_exactly():
    q = F("Why do you want to work here?", "#q", kind="textarea", required=True)
    assert formfill.match_field(q, kb_with()).confidence == 0.0
    m = formfill.match_field(q, kb_with(**{"answer.why_do_you_want_to_work_here": "Because of the data work."}))
    assert m.value == "Because of the data work." and m.confidence == 0.99 and m.key == "answer.why_do_you_want_to_work_here"
    assert formfill.match_field(F("Why do you want to join us?", "#q", kind="textarea"), kb_with(**{"answer.why_do_you_want_to_work_here": "x"})).value is None


def test_a_wordy_label_that_only_contains_a_phrase_and_an_ambiguous_one_are_sent_to_a_person():
    wordy = formfill.match_field(F("Please tell us the best email address where our recruiting team can reach you", "#e"), kb_with())
    assert wordy.key == "email" and wordy.confidence < formfill.THRESHOLD
    ambiguous = formfill.match_field(F("Name and phone number", "#n"), kb_with())
    assert ambiguous.confidence <= 0.80 and "also" in ambiguous.reason


def test_an_embedding_suggestion_is_only_ever_a_proposal():
    suggest = formfill.make_suggester(lambda t: [1.0, 0.0] if "live" in t or "city" in t else [0.0, 1.0], {"location": "city where you live", "email": "mail"})
    m = formfill.match_field(F("Where do you live?", "#l"), kb_with(), suggest=suggest)
    assert m.key == "location" and m.value and m.confidence <= 0.90 < formfill.THRESHOLD


def test_a_file_input_is_the_resume_only_when_it_says_so_and_only_when_a_file_is_stored():
    assert formfill.match_field(F("Resume/CV", "#r", kind="file"), {"resume_file": "/tmp/r.pdf"}).confidence == 0.99
    assert formfill.match_field(F("Resume/CV", "#r", kind="file"), {}).confidence == 0.0
    assert formfill.match_field(F("Portfolio samples", "#r", kind="file"), {"resume_file": "/tmp/r.pdf"}).value is None
    assert formfill.match_field(F("Cover letter", "#c", kind="file"), {"resume_file": "/tmp/r.pdf"}).value is None


# ---------------------------------------------------------------- real Chromium against the stand-in pages


@pytest.fixture()
def resume(tmp_path):
    p = tmp_path / "Sam_Rivera_Resume.pdf"
    p.write_bytes(b"%PDF-1.4\n% a stand-in resume for the form filler tests\n")
    return str(p)


def drive(url, kb, **kw):
    async def go():
        async with open_page("local") as op:
            await op.page.goto(url)
            out = await formfill.fill_form(op.page, kb, **kw)
            return out
    return run(go())


def test_a_form_whose_every_answer_is_known_is_filled_and_submitted_and_the_server_gets_the_values(resume):
    with FakeATS() as ats:
        out = drive(ats.url + "/gh/jobs/1", kb_with(resume_file=resume), submit=True)
    assert out.state == "submitted" and not out.reviews, out
    (sub,) = ats.submissions
    f = sub["fields"]
    assert (f["first_name"], f["last_name"], f["email"], f["phone"], f["linkedin"], f["q_auth"], f["q_hear"]) == (
        "Sam", "Rivera", "sam.rivera@example.test", "+15555550123", "https://linkedin.com/in/sam-rivera-example", "Yes", "LinkedIn")
    assert f["eeo_gender"] == "", "the self-identification select is left alone"
    assert sub["files"]["resume"]["filename"] == "Sam_Rivera_Resume.pdf" and sub["files"]["resume"]["head"].startswith(b"%PDF")


def test_with_submit_off_the_form_is_filled_and_nothing_is_sent(resume):
    with FakeATS() as ats:
        out = drive(ats.url + "/gh/jobs/1", kb_with(resume_file=resume), submit=False)
    assert out.state == "filled" and len(out.filled) >= 7 and ats.submissions == []


def test_an_unknown_required_question_stops_the_run_before_anything_is_sent(resume):
    with FakeATS() as ats:
        out = drive(ats.url + "/gh/jobs/2", kb_with(resume_file=resume), submit=True)
    assert out.state == "waiting_review" and ats.submissions == []
    (rev,) = out.reviews
    assert rev.field_label.startswith("Why do you want to work here") and rev.confidence == 0.0 and "no rule matched" in rev.reason
    assert len(out.filled) >= 7, "everything it was sure of was still filled"


def test_a_required_field_with_no_stored_answer_is_a_review_and_not_a_blank_submission(resume):
    kb = kb_with(resume_file=resume)
    del kb["phone"]
    with FakeATS() as ats:
        out = drive(ats.url + "/gh/jobs/1", kb, submit=True)
    assert out.state == "waiting_review" and ats.submissions == []
    assert [r.proposed_key for r in out.reviews] == ["phone"] and "no stored answer" in out.reviews[0].reason


def test_a_missing_resume_file_is_a_review_too():
    with FakeATS() as ats:
        out = drive(ats.url + "/gh/jobs/1", kb_with(), submit=True)
    assert out.state == "waiting_review" and ats.submissions == [] and any("resume" in r.reason for r in out.reviews)


def test_a_label_that_is_not_sure_enough_is_left_empty_even_though_the_value_is_stored_and_the_form_is_not_sent(resume):
    with FakeATS() as ats:
        out = drive(ats.url + "/gh/jobs/4", kb_with(resume_file=resume), submit=True)
    assert out.state == "waiting_review" and ats.submissions == []
    (rev,) = out.reviews
    assert rev.proposed_key == "email" and rev.proposed_value == "sam.rivera@example.test" and 0.8 < rev.confidence < formfill.THRESHOLD
    assert all(m.key != "email" for m in out.filled), "a field under the threshold is never typed"


def test_a_required_agreement_checkbox_is_never_ticked_by_a_rule(resume):
    with FakeATS() as ats:
        out = drive(ats.url + "/gh/jobs/5", kb_with(resume_file=resume), submit=True)
    assert out.state == "waiting_review" and ats.submissions == []
    assert [r.field_label for r in out.reviews][0].lower().startswith("i agree") and "decision" in out.reviews[0].reason
    assert not any(m.field.kind == "checkbox" for m in out.filled)


def test_a_page_that_answers_with_an_error_banner_is_failed_never_submitted_in_the_report(resume):
    with FakeATS() as ats:
        out = drive(ats.url + "/gh/jobs/3", kb_with(resume_file=resume), submit=True)
    assert out.state == "failed" and "Something went wrong" in out.detail and ats.submissions == []


def test_a_lever_style_form_with_one_full_name_field_and_a_yes_no_radio_group_is_filled_and_submitted(resume):
    with FakeATS() as ats:
        out = drive(ats.url + "/lever/jobs/1", kb_with(resume_file=resume), submit=True)
    assert out.state == "submitted", out
    (sub,) = ats.submissions
    f = sub["fields"]
    assert f["name"] == "Sam Rivera" and f["email"] == "sam.rivera@example.test" and f["auth"] == "yes" and f["urls[LinkedIn]"].startswith("https://linkedin.com/")
    assert f["org"] == "" and f["comments"] == "", "optional fields nobody has an answer for are left empty and cost no review"
    assert sub["files"]["resume"]["filename"] == "Sam_Rivera_Resume.pdf"


def test_a_three_step_flow_is_walked_to_its_submit_button():
    with FakeATS() as ats:
        out = drive(ats.url + "/wd/jobs/1", kb_with(), submit=True)
    assert out.state == "submitted" and out.steps == 3, out
    (sub,) = ats.submissions
    assert sub["fields"]["fn"] == "Sam" and sub["fields"]["em"] == "sam.rivera@example.test" and sub["fields"]["sal"] == "4000 USD a month"
    assert sub["fields"]["li"].startswith("https://linkedin.com/")


def test_a_later_step_with_a_question_it_cannot_answer_stops_there_with_the_earlier_steps_done():
    kb = kb_with()
    del kb["salary_expectation"]
    with FakeATS() as ats:
        out = drive(ats.url + "/wd/jobs/1", kb, submit=True)
    assert out.state == "waiting_review" and out.steps == 2 and ats.submissions == []
    assert out.reviews[0].proposed_key == "salary_expectation"


def test_a_page_whose_form_cannot_be_read_is_never_submitted_blind():
    with FakeATS() as ats:
        out = drive(ats.url + "/custom/jobs/1", kb_with(), submit=True)
    assert out.state == "failed" and "no form fields" in out.detail and ats.submissions == []


GROQ = pathlib.Path(os.environ.get("JOBS_GROQ_KEY_FILE", r"C:\Projects\Professional\Operations\.secrets\groq_test_key.txt"))


@pytest.mark.skipif(not GROQ.exists(), reason="the live Stagehand run needs a Groq key file (JOBS_GROQ_KEY_FILE)")
def test_stagehand_finds_the_fields_the_dom_walk_cannot_and_the_form_is_filled_through_them():
    key = GROQ.read_text(encoding="utf-8").strip().split("=", 1)[-1]

    async def go(url):
        async with open_page("local") as op:
            await op.page.goto(url)

            async def discover():
                from playwright.async_api import async_playwright

                async with async_playwright() as pw:
                    exe = pw.chromium.executable_path
                return await formfill.observe_with_stagehand(op.page.url, executable_path=exe, model="groq/openai/gpt-oss-120b", model_api_key=key,
                                                             instruction="Find every text input in the application form")
            return await formfill.fill_form(op.page, kb_with(), submit=True, discover=discover)

    with FakeATS() as ats:
        out = run(go(ats.url + "/custom/jobs/1"))
        subs = list(ats.submissions)
    assert out.state == "submitted", out
    assert all(m.field.via == "stagehand" for m in out.filled) and len(out.filled) == 3
    assert subs[0]["fields"]["values"] == ["Sam", "Rivera", "sam.rivera@example.test"]
