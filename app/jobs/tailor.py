"""Resume and cover letter tailoring: an LLM loop with an ATS match score and a grounding check that no round can talk its way past.

THE LOOP. Round 0 is built from the applicant's own facts with no model (the best-matching skills and bullets, in a fixed layout), so there is
always a grounded document to fall back on. Then up to `max_rounds` model rounds: the model gets the facts as JSON, the job and what the last
round got wrong, and returns structured JSON (a schema, strict). Each answer is rendered, GROUNDED, then SCORED. It stops when a grounded
document reaches the threshold (90) or when nothing addressable is left, and it keeps the best grounded document it saw.

GROUNDING IS THE RULE, THE SCORE IS ONLY A SCORE. `grounding` finds every claim-bearing token in the generated text (a number, a technology
name from the lexicon, anything capitalised in the middle of a sentence, camel case, an acronym) and requires each one to appear in the
applicant's facts. One that does not is "ungrounded": the round is rejected and the next prompt names it. A document is never returned as
grounded while it holds one. A document is also never improved by claiming a skill the facts lack: the keywords the job wants and the facts
lack are returned as `unsupported`, and the score's ceiling is computed from the others, so a job that wants a tool the applicant has not
used ends below the threshold, says why, and stays true.

THE SCORE (0 to 100) is an explainable proxy, not any vendor's scorer: 75 points for the share of the job's terms (its technologies, the lexicon
terms in its description, the lexicon terms in its title) that the document contains, 15 for the four headings an ATS parser looks for, 10 for a
plain length of 250 to 900 words. `score_breakdown` shows every part. It exists to drive the loop and to be read; it is not a prediction of
how any employer's system ranks a candidate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .llm import LLM, LLMError
from .tech_lexicon import TERMS

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#.\-/]*")
NUMBER = re.compile(r"\d[\d,.]*%?")
HEADINGS = ("SUMMARY", "SKILLS", "EXPERIENCE", "EDUCATION")
MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
          "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "present"}
STOP = frozenset("""a an the and or but if then than that this these those with without for from into onto over under about across after before
during while of on in at by to as is are was were be been being it its i me my we our you your he she they them their his her not no yes
can could would should will may might must do does did done have has had having also more most very much many some any all each other such
so up out off per via using used use built build built led lead leading own owned ran run running worked work working made make making
helped help helping across within between through new first one two three""".split())
ALLOWED = set(HEADINGS) | {"PROJECTS", "COVER", "LETTER", "DEAR", "SINCERELY", "REGARDS", "HIRING", "MANAGER", "TEAM"}

SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "skills", "experience", "projects", "cover_letter"],
    "properties": {
        "summary": {"type": "string"},
        "skills": {"type": "array", "items": {"type": "string"}},
        "experience": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["company", "title", "start", "end", "bullets"],
            "properties": {"company": {"type": "string"}, "title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
                           "bullets": {"type": "array", "items": {"type": "string"}}}}},
        "projects": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "bullets"],
            "properties": {"name": {"type": "string"}, "bullets": {"type": "array", "items": {"type": "string"}}}}},
        "cover_letter": {"type": "string"},
    },
}


@dataclass
class Experience:
    company: str
    title: str
    start: str
    end: str
    bullets: list[str]


@dataclass
class Project:
    name: str
    bullets: list[str]


@dataclass
class Education:
    school: str
    degree: str
    year: str


@dataclass
class Profile:
    name: str
    email: str
    location: str = ""
    links: list[str] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    experience: list[Experience] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)
    education: list[Education] = field(default_factory=list)

    def facts_text(self) -> str:
        parts = [self.name, self.email, self.location, *self.links, *self.summary, *self.skills]
        for e in self.experience:
            parts += [e.company, e.title, e.start, e.end, *e.bullets]
        for p in self.projects:
            parts += [p.name, *p.bullets]
        for d in self.education:
            parts += [d.school, d.degree, d.year]
        return "\n".join(x for x in parts if x)

    def as_json(self) -> dict[str, Any]:
        return {"summary": self.summary, "skills": self.skills,
                "experience": [e.__dict__ for e in self.experience], "projects": [p.__dict__ for p in self.projects],
                "education": [d.__dict__ for d in self.education]}


def words(text: str) -> list[str]:
    return [t.rstrip(".-/") for t in TOKEN.findall(text) if t.rstrip(".-/")]


def vocabulary(text: str) -> set[str]:
    """Every lower-cased token of `text`, and the parts of the ones with a slash or hyphen, so "CI/CD" grounds "CI" and "CD"."""
    out: set[str] = set()
    for w in words(text):
        lw = w.lower()
        out.add(lw)
        out.update(p for p in re.split(r"[-/]", lw) if p)
    return out


def entity_tokens(text: str) -> set[str]:
    """Tokens that carry a claim: a technology from the lexicon, a token with a digit, camel case or an acronym, and a capitalised word
    that is not the first word of its line or sentence."""
    out: set[str] = set()
    for line in text.splitlines():
        line = line.strip().lstrip("-*•").strip()
        # "Python-based" is two words, each judged on its own: the plain one is nothing, the technology must be one of the applicant's
        line = re.sub(r"(?<=[A-Za-z0-9])[-/](?=[A-Za-z0-9])", " ", line)
        sentence_start = True
        for raw in TOKEN.finditer(line):
            tok = raw.group(0).rstrip(".-/")
            if not tok:
                continue
            low = tok.lower()
            ends_sentence = raw.group(0).endswith((".", "!", "?")) or line[raw.end():raw.end() + 1] in ("!", "?")
            is_entity = (low in TERMS or any(c.isdigit() for c in tok) or any(c.isupper() for c in tok[1:]) or (tok.isupper() and len(tok) >= 2)
                         or (tok[0].isupper() and not sentence_start))
            numeric_only = not any(c.isalpha() for c in tok)
            if is_entity and not numeric_only and tok.upper() not in ALLOWED and low not in MONTHS and low not in STOP:
                out.add(low)
            sentence_start = ends_sentence
    return out


def ungrounded(text: str, profile: Profile, extra_allowed: str = "") -> list[str]:
    """Claim-bearing tokens of `text` that are in neither the applicant's facts nor `extra_allowed` (the employer and role, for a letter)."""
    vocab = vocabulary(profile.facts_text() + "\n" + extra_allowed)
    facts_numbers = set(NUMBER.findall(profile.facts_text().replace(",", "")))
    bad = {t for t in entity_tokens(text) if t not in vocab and not (t.strip(".-/") in vocab)}
    # a number in the text must be a number in the facts (a made-up "40%" has a digit and so is an entity above; a plain "40" is caught here)
    for n in NUMBER.findall(text.replace(",", "")):
        if n.rstrip(".") not in facts_numbers and n.rstrip(".") not in vocab:
            bad.add(n.rstrip("."))
    return sorted(bad)


def job_terms(job: dict[str, Any]) -> list[str]:
    desc = vocabulary(job.get("description") or "")
    title = vocabulary(job.get("title") or "")
    techs = {t.lower() for t in (job.get("technologies") or [])}
    terms = {t for t in techs | (desc & TERMS) | (title & TERMS)}
    return sorted(terms)


def score_breakdown(text: str, job: dict[str, Any]) -> dict[str, Any]:
    terms = job_terms(job)
    have = vocabulary(text)
    hit = [t for t in terms if t in have]
    coverage = (len(hit) / len(terms)) if terms else 1.0
    present = [h for h in HEADINGS if re.search(r"^\s*%s\s*$" % h, text, re.M)]
    n = len(words(text))
    if n < 250:
        length_points = round(10 * n / 250)
    elif n <= 900:
        length_points = 10
    else:
        length_points = max(0, 10 - round((n - 900) / 100))
    parts = {"keywords": round(75 * coverage), "structure": round(15 * len(present) / len(HEADINGS)), "length": length_points}
    return {"score": max(0, min(100, sum(parts.values()))), "parts": parts, "terms": terms, "missing": [t for t in terms if t not in have],
            "headings_found": present, "words": n}


def ats_score(text: str, job: dict[str, Any]) -> int:
    return int(score_breakdown(text, job)["score"])


def render_resume(profile: Profile, doc: dict[str, Any]) -> str:
    lines = [profile.name, " | ".join(x for x in [profile.email, profile.location, *profile.links] if x), "", "SUMMARY", doc["summary"].strip(), "", "SKILLS",
             ", ".join(doc["skills"]), "", "EXPERIENCE"]
    for e in doc["experience"]:
        lines += ["%s, %s (%s to %s)" % (e["title"], e["company"], e["start"], e["end"])] + ["- " + b.strip() for b in e["bullets"]] + [""]
    if doc.get("projects"):
        lines += ["PROJECTS"]
        for p in doc["projects"]:
            lines += [p["name"]] + ["- " + b.strip() for b in p["bullets"]]
        lines.append("")
    lines += ["EDUCATION"] + ["%s, %s (%s)" % (d.degree, d.school, d.year) for d in profile.education]
    return "\n".join(lines).strip() + "\n"


def baseline(profile: Profile, job: dict[str, Any]) -> dict[str, Any]:
    """Round 0, no model: the facts ordered by how much of the job's vocabulary they use. Grounded by construction."""
    want = set(job_terms(job)) | vocabulary(job.get("title") or "")

    def weight(s: str) -> int:
        return len(vocabulary(s) & want)

    skills = sorted(profile.skills, key=lambda s: (-weight(s), profile.skills.index(s)))
    return {
        "summary": " ".join(profile.summary[:2]),
        "skills": skills,
        "experience": [{"company": e.company, "title": e.title, "start": e.start, "end": e.end,
                        "bullets": sorted(e.bullets, key=lambda b: (-weight(b), e.bullets.index(b)))[:5]} for e in profile.experience],
        "projects": [{"name": p.name, "bullets": sorted(p.bullets, key=lambda b: (-weight(b), p.bullets.index(b)))[:3]} for p in profile.projects[:3]],
        "cover_letter": "",
    }


SYSTEM = (
    "You tailor a resume and a cover letter to one job using ONLY the applicant facts you are given. Rules, all strict: "
    "(1) Use only technologies, employers, titles, dates, numbers and outcomes that appear in the facts. If the job wants something the facts do not "
    "contain, leave it out; never imply it. (2) Reword and reorder facts to match the job's vocabulary where the facts support it. "
    "(3) Keep each experience entry's company, title, start and end exactly as in the facts. (4) The cover letter is three short paragraphs, plain, "
    "first person, no flattery, and it names the company and role. (5) Return only the JSON object asked for.")


@dataclass
class Round:
    n: int
    score: int
    grounded: bool
    ungrounded: list[str]
    missing: list[str]


@dataclass
class TailorResult:
    resume_text: str
    cover_letter: str
    score: int
    rounds: int
    grounded: bool
    reached_threshold: bool
    unsupported: list[str]
    ceiling: int
    history: list[Round]
    used_model: bool


def _ceiling(profile: Profile, job: dict[str, Any]) -> tuple[int, list[str]]:
    """The best score a truthful document can reach: the job's terms the facts support count, the others cannot."""
    terms = job_terms(job)
    have = vocabulary(profile.facts_text())
    supported = [t for t in terms if t in have]
    coverage = (len(supported) / len(terms)) if terms else 1.0
    return min(100, round(75 * coverage) + 15 + 10), [t for t in terms if t not in have]


def tailor(profile: Profile, job: dict[str, Any], llm: LLM | None, *, threshold: int = 90, max_rounds: int = 4) -> TailorResult:
    ceiling, unsupported = _ceiling(profile, job)
    base = baseline(profile, job)
    base_text = render_resume(profile, base)
    best = {"doc": base, "text": base_text, "letter": "", "score": ats_score(base_text, job)}
    history = [Round(0, best["score"], True, [], score_breakdown(base_text, job)["missing"])]
    used_model = False
    if llm is None:
        return TailorResult(best["text"], "", best["score"], 1, True, best["score"] >= threshold, unsupported, ceiling, history, False)
    # With a model the loop runs at least once even when the facts alone already pass: the cover letter only the model writes is still wanted.

    feedback = ""
    target = min(threshold, ceiling)
    for n in range(1, max_rounds + 1):
        user = ("APPLICANT FACTS (the only source of truth):\n%s\n\nJOB:\n%s at %s\n%s\nTechnologies named: %s\n\n%s"
                % (_json(profile.as_json()), job.get("title"), job.get("company_name"), (job.get("description") or "")[:3000],
                   ", ".join(job.get("technologies") or []) or "none", feedback or "First round."))
        try:
            doc = llm.chat_json(SYSTEM, user, SCHEMA, name="tailored_resume")
        except LLMError as exc:
            history.append(Round(n, 0, False, ["model error: %s" % str(exc)[:120]], []))
            break
        used_model = True
        text = render_resume(profile, doc)
        bad = ungrounded(text, profile) + ungrounded(doc["cover_letter"], profile, "%s %s" % (job.get("company_name") or "", job.get("title") or ""))
        bd = score_breakdown(text, job)
        history.append(Round(n, bd["score"], not bad, sorted(set(bad)), bd["missing"]))
        if bad:
            feedback = ("Your last answer contained claims the facts do not support: %s. Remove them or replace them with something the facts say. "
                        "Do not mention them again." % ", ".join(sorted(set(bad))))
            continue
        if bd["score"] > best["score"] or not best["letter"]:
            keep_text = bd["score"] >= best["score"]
            best = {"doc": doc if keep_text else best["doc"], "text": text if keep_text else best["text"], "letter": doc["cover_letter"],
                    "score": bd["score"] if keep_text else best["score"]}
        if best["score"] >= target and best["letter"]:
            break
        addressable = [t for t in bd["missing"] if t in vocabulary(profile.facts_text())]
        if not addressable:
            break
        feedback = ("Your last answer scored %d of 100. These terms are in the facts and the job wants them, so work them in where they are true: %s. "
                    "Do not add any term that is not in the facts." % (bd["score"], ", ".join(addressable)))

    return TailorResult(best["text"], best["letter"], best["score"], len(history), True, best["score"] >= threshold, unsupported, ceiling, history, used_model)


def _json(o: Any) -> str:
    import json
    return json.dumps(o, ensure_ascii=False)
