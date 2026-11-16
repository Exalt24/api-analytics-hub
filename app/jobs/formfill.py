"""Application form mapping and filling with a confidence for every field, and a person for every field that is not sure enough.

THE RULE THE WHOLE MODULE EXISTS TO KEEP: a field is typed only when its confidence is at or above the threshold (0.95) AND the knowledge base
holds the value. Anything else is left empty and returned as a `Review`, and the run waits (hitl.py). Nothing is ever guessed to get a form
through. Consent boxes and self-identification questions (gender, race, veteran status, disability) are never ticked or answered by a rule; they
are always a person's, unless the knowledge base holds an explicit stored answer for that exact question.

HOW A LABEL BECOMES A KEY, in order, first hit wins (see `match_field`):
  1. memory: the person answered this exact question before (`answer.<slug of the label>`), 0.99. This is how a correction in the review queue
     makes the next form fill itself.
  2. the HTML `autocomplete` token or the input type (email, tel), 0.99.
  3. the label equals a known phrase for a key, 0.98.
  4. the label contains a known phrase, 0.95 when the phrase is at least half of the label, else 0.90 (so a wordy label goes to a person).
  5. no rule hit: an optional embedding similarity proposes a key, capped at 0.90 so it is only ever a suggestion for the reviewer.
A label that names a different thing (a company name, a school, a reference) scores 0 for a personal-name key. A select must have an option that
matches the stored value, else 0.60. A radio or select for yes and no maps the stored word to the option of that word.

TWO WAYS TO FIND THE FIELDS. `inventory` reads the DOM (labels, `for`, wrapping labels, aria, legends, placeholders, options), which is exact and
needs no model. When a page draws its fields with widgets the DOM walk cannot label (`inventory` returns fewer than two), `observe_with_stagehand`
asks Stagehand's `observe` for them (its own browser, the same address) and turns each answer into a field with an XPath selector.
"""
from __future__ import annotations

import asyncio
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from playwright.async_api import Page

THRESHOLD = 0.95

ALIASES: dict[str, list[str]] = {
    "first_name": ["first name", "given name", "forename"],
    "last_name": ["last name", "surname", "family name"],
    "full_name": ["full name", "your name", "name", "legal name"],
    "email": ["email", "email address", "e-mail", "e-mail address"],
    "phone": ["phone", "phone number", "mobile", "mobile number", "telephone", "contact number"],
    "linkedin": ["linkedin", "linkedin profile", "linkedin url", "linkedin profile url"],
    "github": ["github", "github profile", "github url"],
    "website": ["website", "portfolio", "portfolio url", "personal website", "personal site"],
    "location": ["location", "current location", "city", "where are you based"],
    "country": ["country", "country of residence"],
    "work_authorization": ["are you legally authorized to work", "legally authorized to work", "work authorization", "authorized to work in", "right to work"],
    "sponsorship": ["require sponsorship", "visa sponsorship", "need sponsorship", "require visa sponsorship"],
    "salary_expectation": ["salary expectation", "expected salary", "desired compensation", "desired salary", "compensation expectations"],
    "start_date": ["earliest start date", "when can you start", "available to start", "start date", "notice period"],
    "referral_source": ["how did you hear about us", "how did you hear about this job", "referral source", "how did you find us"],
}
#: a label with one of these words is about someone or something else, so it can never be the applicant's own name or contact
NOT_ME = {"company", "employer", "school", "university", "college", "reference", "referee", "manager", "supervisor", "emergency", "spouse", "parent", "previous"}
PERSONAL = {"first_name", "last_name", "full_name", "email", "phone"}
CONSENT = re.compile(r"\b(agree|consent|acknowledge|accept|terms|privacy|policy|certify|gdpr|authorize us|i confirm)\b", re.I)
SELF_ID = re.compile(r"\b(gender|race|ethnic|veteran|disabilit|sexual orientation|pronoun|hispanic|latino|transgender|lgbt)\b", re.I)
RESUME = re.compile(r"\b(resume|résumé|cv|curriculum vitae)\b", re.I)
COVER = re.compile(r"\bcover letter\b", re.I)
SUCCESS = re.compile(r"(thank you for applying|application (has been )?(received|submitted)|we(?:'ve| have) received your application|successfully submitted)", re.I)
NEXT_BUTTON = re.compile(r"^\s*(next|continue|save and continue|next step)\s*$", re.I)
SUBMIT_BUTTON = re.compile(r"^\s*(submit( application)?|apply( now)?|send application|finish)\s*$", re.I)

# The inventory function lives in inventory.js so Playwright (here) and Puppeteer (n8n/jobs/puppeteer_inventory.js) run the very same text in a page.
INVENTORY_JS = Path(__file__).with_name("inventory.js").read_text(encoding="utf-8")


def norm(label: str) -> str:
    s = re.sub(r"[*:?()\[\]]", " ", label.lower())
    return re.sub(r"\s+", " ", s).strip()


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", norm(label)).strip("_")[:60]


@dataclass
class Field:
    label: str
    selector: str | None
    kind: str = "text"
    required: bool = False
    options: list[str] = field(default_factory=list)
    option_selectors: dict[str, str | None] = field(default_factory=dict)
    autocomplete: str = ""
    type: str = "text"
    via: str = "dom"


@dataclass
class Match:
    field: Field
    key: str | None
    value: str | None
    confidence: float
    reason: str


@dataclass
class Review:
    field_label: str
    field_selector: str | None
    proposed_key: str | None
    proposed_value: str | None
    confidence: float
    reason: str


@dataclass
class Outcome:
    state: str  # submitted | waiting_review | failed | filled (submit was off)
    filled: list[Match] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    detail: str = ""
    steps: int = 1


AUTOCOMPLETE = {"given-name": "first_name", "family-name": "last_name", "name": "full_name", "email": "email", "tel": "phone", "url": "website"}


def _option_for(f: Field, value: str) -> str | None:
    v = norm(value)
    if v in ("yes", "y", "true"):
        want = {"yes", "y"}
    elif v in ("no", "n", "false"):
        want = {"no", "n"}
    else:
        want = {v}
    for o in f.options:
        if norm(o) in want:
            return o
    for o in f.options:
        if v and (v in norm(o) or norm(o) in v) and len(norm(o)) > 2:
            return o
    return None


def match_field(f: Field, kb: dict[str, str], *, suggest: Callable[[str], tuple[str, float] | None] | None = None) -> Match:
    label = norm(f.label)
    if f.kind == "file":
        if COVER.search(f.label):
            v = kb.get("cover_letter_file")
            return Match(f, "cover_letter_file", v, 0.99 if v else 0.0, "a file input for the cover letter" if v else "no cover letter file is stored")
        if RESUME.search(f.label) or not label:
            v = kb.get("resume_file")
            return Match(f, "resume_file", v, 0.99 if v else 0.0, "a file input for the resume" if v else "no resume file is stored")
        return Match(f, None, None, 0.0, "a file input that is not the resume or cover letter")
    mem = kb.get("answer." + slug(f.label))
    if f.label and mem is not None:
        return _shape(f, "answer." + slug(f.label), mem, 0.99, "a person answered this exact question before")
    if f.kind == "checkbox" or CONSENT.search(f.label):
        return Match(f, None, None, 0.5, "consent and agreement boxes are a person's decision")
    if SELF_ID.search(f.label):
        key = "eeo." + slug(f.label)
        if key in kb:
            return _shape(f, key, kb[key], 0.98, "an explicit stored answer to this self-identification question")
        return Match(f, None, None, 0.0, "self-identification questions are asked, never guessed")
    key, conf, why = None, 0.0, "no rule matched"
    ac = AUTOCOMPLETE.get(f.autocomplete.lower())
    if ac:
        key, conf, why = ac, 0.99, "autocomplete token %r" % f.autocomplete
    elif f.type == "email":
        key, conf, why = "email", 0.99, "input type email"
    elif f.type == "tel":
        key, conf, why = "phone", 0.99, "input type tel"
    else:
        hits: list[tuple[float, str, str, str]] = []
        for k, phrases in ALIASES.items():
            for p in phrases:
                if label == p:
                    hits.append((0.98, k, p, "the label is %r" % p))
                elif re.search(r"\b%s\b" % re.escape(p), label):
                    ratio = len(p) / max(len(label), 1)
                    hits.append((0.95 if ratio >= 0.5 else 0.90, k, p, "the label contains %r" % p))
        # a short phrase that sits inside a longer one another key matched ("name" inside "last name") belongs to the longer phrase: it is not a rival
        best = [(c, k, why) for c, k, p, why in hits
                if not any(k2 != k and p != p2 and re.search(r"\b%s\b" % re.escape(p), p2) for _, k2, p2, _ in hits)]
        if best:
            best.sort(reverse=True)
            top = best[0]
            rivals = {k for c, k, _ in best if c >= top[0] - 0.05 and k != top[1]}
            if rivals:
                key, conf, why = top[1], 0.80, "the label fits %s and also %s" % (top[1], ", ".join(sorted(rivals)))
            else:
                key, conf, why = top[1], top[0], top[2]
    if key in PERSONAL and NOT_ME & set(label.split()):
        return Match(f, key, None, 0.0, "the label is about someone or something else (%s)" % ", ".join(sorted(NOT_ME & set(label.split()))))
    if key is None and suggest is not None and label:
        s = suggest(f.label)
        if s:
            key, conf, why = s[0], min(s[1], 0.90), "an embedding suggestion, only ever a proposal"
    if key is None:
        return Match(f, None, None, 0.0, why)
    value = kb.get(key)
    if value is None:
        return Match(f, key, None, 0.0, "the key %s has no stored answer" % key)
    return _shape(f, key, value, conf, why)


def _shape(f: Field, key: str, value: str, conf: float, why: str) -> Match:
    if f.kind == "select" or f.kind == "radio":
        opt = _option_for(f, value)
        if opt is None:
            return Match(f, key, value, min(conf, 0.60), "no option matches the stored value %r" % value)
        return Match(f, key, opt, conf, why)
    return Match(f, key, value, conf, why)


def make_suggester(embed_query: Callable[[str], list[float]], keys: dict[str, str]) -> Callable[[str], tuple[str, float] | None]:
    """A suggester over key descriptions: cosine similarity between the label and each key's text. `keys` is {key: description}."""
    vecs = {k: embed_query(d) for k, d in keys.items()}

    def cos(a: list[float], b: list[float]) -> float:
        na, nb = math.sqrt(sum(x * x for x in a)) or 1.0, math.sqrt(sum(x * x for x in b)) or 1.0
        return sum(x * y for x, y in zip(a, b)) / (na * nb)

    def suggest(label: str) -> tuple[str, float] | None:
        q = embed_query(label)
        scored = sorted(((cos(q, v), k) for k, v in vecs.items()), reverse=True)
        return (scored[0][1], scored[0][0]) if scored and scored[0][0] > 0.30 else None

    return suggest


async def inventory(page: Page) -> list[Field]:
    raw = await page.evaluate(INVENTORY_JS)
    return [Field(label=r["label"], selector=r.get("selector"), kind=r["kind"], required=bool(r["required"]), options=list(r.get("options") or []),
                  option_selectors=dict(r.get("option_selectors") or {}), autocomplete=r.get("autocomplete") or "", type=r.get("type") or "text") for r in raw]


async def observe_with_stagehand(url: str, *, executable_path: str, model: str, model_api_key: str,
                                 instruction: str = "Find every input, select and text area in the application form") -> list[Field]:
    """Ask Stagehand to find the fields on the page at `url`. Stagehand starts its own Chromium (it loads an extension at launch, which a browser
    we attach to over CDP cannot take), opens the same address and answers with actions: each description is the label and each XPath is the
    selector, which Playwright can use on the same static page. It is a fallback for the FIRST page of a form; a flow that keeps state between
    steps cannot be re-entered by a second browser."""
    from stagehand import Stagehand, local_browser

    browser = await local_browser.launch(executable_path=executable_path, headless=True)
    try:
        sh = await Stagehand.create(browser=browser, model=model, model_api_key=model_api_key)
        try:
            page = (await browser.context.pages())[0]
            await page.goto(url)
            res = await sh.observe(instruction)
            fields = []
            for a in res.data:
                desc = (a.description or "").strip()
                kind = "select" if "select" in (a.method or "").lower() or "dropdown" in desc.lower() else "text"
                label = re.sub(r"^(textbox|text box|input|select dropdown|dropdown|text area|textarea)( field)?( for| of)?\s*", "", desc, flags=re.I).strip() or desc
                fields.append(Field(label=label, selector=a.selector, kind=kind, via="stagehand"))
            return fields
        finally:
            await sh.close()
    finally:
        await browser.close()


async def _fill(page: Page, m: Match, typing_delay: tuple[int, int] | None = None) -> None:
    f = m.field
    loc = page.locator(f.selector) if f.selector else None
    if f.kind == "file":
        await loc.set_input_files(m.value)
    elif f.kind == "select":
        await loc.select_option(label=m.value)
    elif f.kind == "radio":
        await page.locator(f.option_selectors[m.value]).check()
    elif typing_delay:
        # one key at a time with a random pause of a person's pace, so a page that watches keystrokes sees keystrokes (fill() sets the value with none)
        await loc.click()
        for ch in m.value:
            await loc.press_sequentially(ch)
            await asyncio.sleep(random.uniform(*typing_delay) / 1000.0)
    else:
        await loc.fill(m.value)


async def _poll(page: Page, check: Callable[[], Awaitable[bool]], *, seconds: float, step: float = 0.25) -> bool:
    for _ in range(int(seconds / step)):
        if await check():
            return True
        await asyncio.sleep(step)
    return await check()


async def _errors(page: Page) -> list[str]:
    return await page.evaluate("""() => Array.from(document.querySelectorAll('[role=alert], .error, [aria-invalid=true]'))
        .map((e) => (e.getAttribute('aria-label') || e.innerText || e.id || '').replace(/\\s+/g, ' ').trim()).filter(Boolean).slice(0, 10)""")


async def _button(page: Page, pattern: re.Pattern[str]):
    for b in await page.locator("button, input[type=submit], [role=button]").all():
        text = (await b.inner_text()) if (await b.evaluate("e => e.tagName")) != "INPUT" else (await b.get_attribute("value") or "")
        if pattern.match(text.strip()) and await b.is_visible():
            return b
    return None


async def fill_form(page: Page, kb: dict[str, str], *, threshold: float = THRESHOLD, submit: bool = False, max_steps: int = 6,
                    suggest: Callable[[str], tuple[str, float] | None] | None = None,
                    discover: Callable[[], Awaitable[list[Field]]] | None = None, typing_delay: tuple[int, int] | None = None) -> Outcome:
    """Fill the form on `page` (one or several steps). Returns `waiting_review` with a Review for every field not sure enough, before anything is
    submitted; with none left and `submit` true it clicks the submit button and reads the page for the success state."""
    out = Outcome(state="filled")
    for step in range(1, max_steps + 1):
        out.steps = step
        fields = await inventory(page)
        if len(fields) < 2 and discover is not None:
            fields = await discover()
        if step == 1 and not fields:
            # a page whose form could not be read is never submitted: an empty application is worse than none
            out.state, out.detail = "failed", "no form fields were found on the page"
            return out
        matches = [match_field(f, kb, suggest=suggest) for f in fields]
        for m in matches:
            ok = m.value is not None and m.confidence >= threshold
            if ok:
                try:
                    await _fill(page, m, typing_delay)
                    out.filled.append(m)
                    continue
                except Exception as exc:  # a field that would not take the value is a review, never a silent skip
                    m = Match(m.field, m.key, m.value, 0.0, "the field did not accept the value: %s" % str(exc)[:120])
            # an optional field with nothing stored is left empty and costs nobody a review; a required one, or one we have a value for
            # but are not sure of, is a person's
            if m.field.required or m.value is not None:
                out.reviews.append(Review(m.field.label or "(unlabelled)", m.field.selector, m.key, m.value, round(m.confidence, 3), m.reason))
        if out.reviews:
            out.state = "waiting_review"
            return out
        nxt = await _button(page, NEXT_BUTTON)
        if nxt is not None and await _button(page, SUBMIT_BUTTON) is None:
            before = await page.content()
            await nxt.click()
            await _poll(page, lambda: _changed(page, before), seconds=5)
            continue
        break
    if not submit:
        return out
    btn = await _button(page, SUBMIT_BUTTON)
    if btn is None:
        out.state, out.detail = "failed", "no submit button was found"
        return out
    await btn.click()
    done = await _poll(page, lambda: _success(page), seconds=10)
    if done:
        out.state = "submitted"
        return out
    errs = await _errors(page)
    out.state, out.detail = "failed", "the page did not reach a success state" + ("; it says: " + " | ".join(errs) if errs else "")
    for e in errs:
        out.reviews.append(Review(e[:200], None, None, None, 0.0, "the form rejected the submission"))
    return out


async def _changed(page: Page, before: str) -> bool:
    return (await page.content()) != before


async def _success(page: Page) -> bool:
    if await page.locator("[data-testid=application-success]").count():
        return True
    return bool(SUCCESS.search(await page.inner_text("body")))
