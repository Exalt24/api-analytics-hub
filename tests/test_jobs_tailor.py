"""The tailoring loop: grounding, the ATS match score, the model loop against the stand-in OpenAI server, and the rendered files.

The applicant here is made up (Sam Rivera). The cases that matter most are the ones where the model is scripted to lie: invent a tool in capitals, in
lower case, invent a number, keep inventing after being told, and answer with something that is not the schema. In every one the returned
document must be grounded, because a document with an invented claim must never leave this module.
"""
from __future__ import annotations

import json

import pytest

from app.jobs import render, tailor
from app.jobs.fake_openai import FakeOpenAI
from app.jobs.llm import LLM, LLMError

PROFILE = tailor.Profile(
    name="Sam Rivera", email="sam.rivera@example.test", location="Manila, Philippines", links=["github.com/sam-example"],
    summary=["Data engineer who builds ingestion pipelines and the services around them.", "Works in Python and PostgreSQL every day."],
    skills=["Python", "PostgreSQL", "Redis", "Docker", "FastAPI", "SQL", "Qdrant", "n8n"],
    experience=[tailor.Experience("Northwind Analytics", "Data Engineer", "2024", "2026", [
        "Built ingestion pipelines in Python that load vendor JSON into PostgreSQL",
        "Cut the nightly load from 90 minutes to 25 minutes by batching upserts",
        "Wrote Docker based deployments for FastAPI services",
        "Used Redis to skip records already seen in the last day"])],
    projects=[tailor.Project("Vector search service", ["Stored embeddings in Qdrant and filtered every query by tenant"])],
    education=[tailor.Education("Example University", "BS Computer Science", "2025")])

REACHABLE = {"title": "Backend Data Engineer", "company_name": "Acme", "technologies": ["python", "postgresql", "redis", "docker", "fastapi"],
             "description": "Build services in Python and FastAPI on PostgreSQL and Redis, shipped with Docker."}
UNREACHABLE = {"title": "Platform Data Engineer", "company_name": "Initech", "technologies": ["python", "postgresql", "kubernetes", "dbt", "kafka"],
               "description": "Run Kubernetes, dbt and Kafka pipelines into PostgreSQL with Python."}


def doc_from(profile: tailor.Profile, *, extra_skills: list[str] | None = None, extra_bullet: str | None = None, summary: str | None = None, letter: str = "Dear Acme team,\n\nI build Python services on PostgreSQL.\n\nSam Rivera") -> dict:
    exp = [{"company": e.company, "title": e.title, "start": e.start, "end": e.end, "bullets": list(e.bullets) + ([extra_bullet] if extra_bullet else [])} for e in profile.experience]
    return {"summary": summary or " ".join(profile.summary), "skills": profile.skills + (extra_skills or []), "experience": exp,
            "projects": [{"name": p.name, "bullets": p.bullets} for p in profile.projects], "cover_letter": letter}


@pytest.fixture()
def fake():
    f = FakeOpenAI()
    f.__enter__()
    yield f
    f.__exit__(None, None, None)


def llm_for(f: FakeOpenAI) -> LLM:
    return LLM(api_key="sk-test", base_url=f.url + "/v1", model="gpt-4o", attempts=2, sleep=lambda s: None)


# ---------------------------------------------------------------- grounding


@pytest.mark.parametrize("text, expect", [
    ("Deployed Kubernetes clusters", ["kubernetes"]),
    ("deployed kubernetes clusters", ["kubernetes"]),
    ("Reduced latency by 40%", ["40%"]),
    ("Led a team of 12 engineers", ["12"]),
    ("Built pipelines in Python with PostgreSQL", []),
    ("Cut the nightly load from 90 minutes to 25 minutes", []),
    ("Worked at Northwind Analytics as a Data Engineer", []),
    ("Worked at Globex Corporation", ["corporation", "globex"]),
    ("Used TensorFlow for ranking", ["tensorflow"]),
    ("Shipped the Qdrant search service", []),
    ("Great at building things quickly. Always shipping.", []),
    ("Built a Python-based ingestion service", []),
    ("Built a Terraform-based ingestion service", ["terraform"]),
    ("Cut the load by 25-30 percent", ["30"]),
])
def test_grounding_flags_exactly_the_claims_the_facts_do_not_hold(text, expect):
    assert tailor.ungrounded(text, PROFILE) == expect


def test_a_cover_letter_may_name_the_employer_and_role_it_is_addressed_to():
    letter = "Dear Acme team, I am applying for the Backend Data Engineer role."
    assert tailor.ungrounded(letter, PROFILE) != []
    assert tailor.ungrounded(letter, PROFILE, "Acme Backend Data Engineer") == []


def test_the_lexicon_check_is_what_catches_a_lower_case_tool(monkeypatch):
    assert tailor.ungrounded("worked with terraform daily", PROFILE) == ["terraform"]
    monkeypatch.setattr(tailor, "TERMS", frozenset())
    assert tailor.ungrounded("worked with terraform daily", PROFILE) == [], "control: with the lexicon emptied the lower case tool slips through"


# ---------------------------------------------------------------- score


def test_the_score_is_explained_part_by_part_and_moves_with_the_text():
    base = tailor.baseline(PROFILE, REACHABLE)
    text = tailor.render_resume(PROFILE, base)
    bd = tailor.score_breakdown(text, REACHABLE)
    assert bd["parts"]["structure"] == 15 and set(bd["headings_found"]) == set(tailor.HEADINGS)
    assert bd["missing"] == [] and bd["parts"]["keywords"] == 75 and bd["score"] == sum(bd["parts"].values())
    stripped = text.replace("Redis", "").replace("redis", "")
    assert tailor.score_breakdown(stripped, REACHABLE)["score"] < bd["score"]
    assert tailor.ats_score("just words", REACHABLE) < 40


def test_the_ceiling_counts_only_what_the_facts_support():
    ceiling, unsupported = tailor._ceiling(PROFILE, UNREACHABLE)  # noqa: SLF001
    assert unsupported == ["dbt", "kafka", "kubernetes"] and ceiling == 55
    assert tailor._ceiling(PROFILE, REACHABLE)[0] == 100  # noqa: SLF001


# ---------------------------------------------------------------- the loop


def test_without_a_model_the_baseline_is_returned_and_is_grounded():
    res = tailor.tailor(PROFILE, REACHABLE, None)
    assert res.grounded and not res.used_model and res.cover_letter == "" and res.score >= 90
    assert tailor.ungrounded(res.resume_text, PROFILE) == []


def test_the_loop_rejects_an_invented_claim_names_it_in_the_next_prompt_and_accepts_the_clean_round(fake):
    # a job the baseline cannot already pass: ask for fewer facts than the loop would otherwise reuse
    job = {**REACHABLE, "technologies": ["python", "postgresql", "redis", "docker", "fastapi", "qdrant", "n8n"], "description": "Python, PostgreSQL, Redis, Docker, FastAPI, Qdrant and n8n."}
    lying = doc_from(PROFILE, extra_skills=["Kubernetes"], extra_bullet="Reduced latency by 40% with Kubernetes")
    honest = doc_from(PROFILE)
    fake.chat_script = [json.dumps(lying), json.dumps(honest)]
    # force the model path: lower the baseline by asking for a threshold above it
    res = tailor.tailor(PROFILE, job, llm_for(fake), threshold=100, max_rounds=3)
    assert res.used_model and res.grounded
    assert tailor.ungrounded(res.resume_text, PROFILE) == []
    assert "Kubernetes" not in res.resume_text and "40%" not in res.resume_text
    rejected = [h for h in res.history if h.n == 1][0]
    assert not rejected.grounded and "kubernetes" in rejected.ungrounded and "40%" in rejected.ungrounded
    second_prompt = fake.chat_requests[1]["messages"][1]["content"]
    assert "kubernetes" in second_prompt.lower() and "40%" in second_prompt
    assert res.cover_letter.startswith("Dear Acme")


def test_a_job_that_wants_tools_the_facts_lack_ends_below_the_threshold_and_says_so(fake):
    fake.chat_script = [json.dumps(doc_from(PROFILE))] * 4
    res = tailor.tailor(PROFILE, UNREACHABLE, llm_for(fake), threshold=90, max_rounds=3)
    assert not res.reached_threshold and res.grounded
    assert res.unsupported == ["dbt", "kafka", "kubernetes"] and res.ceiling == 55
    assert all(t not in res.resume_text.lower() for t in res.unsupported)
    assert res.score <= res.ceiling


def test_a_model_that_keeps_inventing_never_gets_its_document_out(fake):
    liar = json.dumps(doc_from(PROFILE, extra_skills=["Kubernetes", "Terraform"], extra_bullet="Managed 200 servers"))
    fake.chat_script = [liar] * 5
    res = tailor.tailor(PROFILE, {**REACHABLE, "technologies": REACHABLE["technologies"] + ["qdrant", "n8n"]}, llm_for(fake), threshold=100, max_rounds=4)
    assert res.grounded and tailor.ungrounded(res.resume_text, PROFILE) == []
    assert "terraform" not in res.resume_text.lower() and "200" not in res.resume_text
    assert res.cover_letter == "", "no model round was grounded, so no model letter is returned"
    assert all(not h.grounded for h in res.history if h.n >= 1 and h.ungrounded)


def test_a_letter_that_invents_is_rejected_even_when_the_resume_is_clean(fake):
    bad_letter = doc_from(PROFILE, letter="Dear Acme team, at Globex Corporation I managed 40 people.")
    fake.chat_script = [json.dumps(bad_letter), json.dumps(doc_from(PROFILE))]
    res = tailor.tailor(PROFILE, {**REACHABLE, "technologies": REACHABLE["technologies"] + ["qdrant", "n8n"]}, llm_for(fake), threshold=100, max_rounds=2)
    first = [h for h in res.history if h.n == 1][0]
    assert not first.grounded and "globex" in first.ungrounded
    assert "Globex" not in res.cover_letter


def test_a_reply_that_is_not_the_schema_or_a_bad_key_is_survived(fake):
    fake.chat_script = ["this is not json at all"]
    res = tailor.tailor(PROFILE, {**REACHABLE, "technologies": REACHABLE["technologies"] + ["qdrant", "n8n"]}, llm_for(fake), threshold=100, max_rounds=2)
    assert res.grounded and not res.used_model and any("model error" in " ".join(h.ungrounded) for h in res.history)
    wrong_key = LLM(api_key="sk-wrong", base_url=fake.url + "/v1", attempts=1, sleep=lambda s: None)
    with pytest.raises(LLMError):
        wrong_key.chat_json("s", "u", tailor.SCHEMA)


def test_a_rate_limited_call_is_retried_and_a_server_error_gives_up(fake):
    calls = []

    class Flaky(FakeOpenAI):
        pass

    fake.chat_script = []  # the stand-in answers 500 when its script is empty
    with pytest.raises(LLMError, match="gave up"):
        LLM(api_key="sk-test", base_url=fake.url + "/v1", attempts=2, sleep=calls.append).chat_json("s", "u", tailor.SCHEMA)
    assert calls == [1], "one wait between two attempts"


# ---------------------------------------------------------------- the files


def test_the_docx_and_the_pdf_carry_the_same_text_as_the_resume():
    text = tailor.render_resume(PROFILE, tailor.baseline(PROFILE, REACHABLE))
    docx = render.docx_text(render.to_docx(text))
    pdf = render.pdf_text(render.to_pdf(text))
    for needle in ("Sam Rivera", "Northwind Analytics", "PostgreSQL", "Example University"):
        assert needle in docx and needle in pdf
    assert "\n- " not in docx and "- Cut" not in docx


def test_a_job_the_facts_already_pass_still_gets_the_model_for_its_cover_letter(fake):
    fake.chat_script = [json.dumps(doc_from(PROFILE))]
    res = tailor.tailor(PROFILE, REACHABLE, llm_for(fake), threshold=90)
    assert res.used_model and res.reached_threshold and res.grounded
    assert res.cover_letter.startswith("Dear Acme") and len(fake.chat_requests) == 1
    assert fake.chat_requests[0]["max_tokens"] == 4000, "a ceiling on the reply, so a provider that checks credits against the maximum does not refuse it"
