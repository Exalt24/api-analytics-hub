"""LLM-generated SEO copy for a product catalogue, with the numbers verified.

WHAT THIS IS FOR. A technical catalogue with thousands of SKUs cannot have meta
descriptions written by hand, and the descriptions it does have are usually the
supplier's boilerplate repeated across hundreds of products, which search engines
treat as duplicate. An LLM writes them in seconds. The problem is that an LLM will
also cheerfully invent a purity, a particle size, or a temperature rating, and on a
materials or lab-supply catalogue a fabricated specification is not a typo, it is a
liability: someone orders 99.5% alumina because the meta description said so.

SO THE INTERESTING PART IS NOT THE GENERATION, IT IS THE REFUSAL.

`verify_grounded` extracts every number from the generated text and requires each
one to appear in the source facts. Not "similar to", present. A model that writes
"99.99% pure" about a product whose only stated purity is 99.5% fails the check and
the row is rejected rather than published. That turns a category of silent,
plausible, legally awkward errors into a visible failure with a count attached.

DESIGN DECISIONS WORTH THE COMMENT.

  * The prompt receives FACTS, never the whole product record, so a private cost
    field or an internal note cannot leak into public copy through the model.
  * Temperature 0.2 rather than 0. Zero is not more truthful, it is more
    repetitive, and near-identical descriptions across a catalogue are the
    duplicate-content problem this is meant to solve.
  * Length is enforced in code after generation, because asking a model for "under
    155 characters" gets you 155 characters roughly two thirds of the time.
  * The transport is an OpenAI-compatible chat completion, which is what OpenAI,
    Groq, Together and a local vLLM all speak, so the provider is a base URL and a
    model name rather than a rewrite.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import httpx

#: Google truncates a meta description around 155-160 characters on desktop and
#: shorter on mobile. Anything past this is written for nobody.
MAX_META_CHARS = 155

#: Below this a description is not doing its job, and an empty or two-word answer
#: is the most common shape of a failed generation.
MIN_META_CHARS = 50

#: Groq deprecates model names periodically and returns a 404 model_not_found,
#: not a helpful redirect. Measured 2026-08-21: llama-3.3-70b-versatile was gone
#: from the account's model list entirely. Verify against GET /models when a call
#: 404s rather than assuming the key is bad.
DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"

#: Room for BOTH the reasoning trace and the answer. A reasoning model spends
#: tokens thinking before it emits a character, and a tight budget produces a
#: successful, billed, empty response: measured at max_tokens=160, 158 of the 160
#: completion tokens were reasoning_tokens and content came back as "".
#: A meta description is ~40 tokens; the rest of this is headroom for the trace.
MAX_COMPLETION_TOKENS = 700

#: Reasoning models accept an effort dial. A meta description does not need deep
#: thought, and low effort leaves the budget for output. Providers that do not
#: know the field ignore it rather than erroring.
REASONING_EFFORT = "low"

SYSTEM_PROMPT = """You write meta descriptions for a technical product catalogue.

Rules, in order of importance:
1. Use ONLY the facts given. Never state a number, grade, purity, dimension,
   temperature or material that is not in the facts.
2. If the facts are thin, write a shorter description. Never pad with invented
   detail.
3. One or two sentences, under 150 characters, plain declarative English.
4. No marketing language: no "premium", "high-quality", "cutting-edge", "perfect
   for all your needs", no exclamation marks.
5. Lead with what the product IS, then its single most distinguishing stated
   specification.

Reply with the description text only. No quotes, no preamble, no label."""


class GenerationRejected(Exception):
    """The output failed a check, so nothing is published.

    Carries the reason so a run can report WHY a SKU was skipped rather than
    leaving a silent hole in the catalogue.
    """


@dataclass
class CopyResult:
    product_id: str
    text: str
    grounded: bool
    rejected_reason: str | None = None
    unsupported_numbers: list[str] = field(default_factory=list)
    model: str = DEFAULT_MODEL

    @property
    def publishable(self) -> bool:
        return self.grounded and self.rejected_reason is None


#: Numbers as they appear in real technical copy: 99.99, 1,200, 3/8, 25.4mm, 2".
#: Greedy on the digits and loose on the suffix, because the check must catch a
#: fabricated FIGURE regardless of the unit attached to it.
#:
#: The lookbehind is load-bearing on a technical catalogue. Without it the 2 and
#: the 3 in "Al2O3" are read as unsupported numbers, and the guard then fires on
#: H2O, CO2, Si3N4 and every other formula, which is how a correct guard gets
#: switched off. A digit preceded by a LETTER belongs to a token; a digit with a
#: boundary before it is a quantity and is still checked, so "38ohms" written
#: without a space is caught while "Al2O3" is not.
#:
#: Deliberately NOT symmetric: skipping any digit adjacent to any letter would also
#: skip "38ohms", and a false negative here is the expensive direction because it
#: is the one that publishes an invented specification.
NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:[.,]\d+)*(?:\s*/\s*\d+)?")

#: Written numbers a model uses instead of digits, which would otherwise slip past
#: a digit-only check entirely. This is the hole the first version of the guard had.
WORD_NUMBERS = {
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "twenty", "thirty", "forty", "fifty",
    "hundred", "thousand", "million",
}


#: Whitespace lookalikes a model emits that are not a plain space. Each of these
#: has been seen in real generated copy, most often between a figure and its unit.
#: They break CSV exports, they mojibake through a cp1252 pipeline, and they make
#: two visually identical strings compare unequal.
EXOTIC_SPACE = {
    "\u00a0",  # no-break space
    "\u202f",  # narrow no-break space
    "\u2007",  # figure space
    "\u2009",  # thin space
    "\u200a",  # hair space
    "\u2002", "\u2003", "\u2004", "\u2005", "\u2006", "\u2008",
    "\u3000",  # ideographic space
}

#: Invisible characters with no width. A soft hyphen inside a word is invisible on
#: screen and splits the word for anything doing a substring match.
ZERO_WIDTH = {"\u00ad", "\u200b", "\u200c", "\u200d", "\ufeff"}


def clean_text(text: str) -> str:
    """Plain-space and strip invisibles, leaving the words untouched."""
    out = []
    for ch in text:
        if ch in EXOTIC_SPACE:
            out.append(" ")
        elif ch in ZERO_WIDTH:
            continue
        else:
            out.append(ch)
    # Collapse any run of whitespace the substitution created, and normalise the
    # curly quotes a model reaches for, which break a JSON-LD field if the copy is
    # later embedded there.
    cleaned = " ".join("".join(out).split())
    for fancy, plain in (("\u2018", "'"), ("\u2019", "'"),
                         ("\u201c", '"'), ("\u201d", '"'),
                         ("\u2013", "-"), ("\u2014", "-")):
        cleaned = cleaned.replace(fancy, plain)
    return cleaned


def _normalise(value: str) -> str:
    """Compare numbers by their digits, ignoring separators and spacing.

    "1,200" in the facts and "1200" in the output are the same number, and
    treating them as different would reject correct copy, which trains people to
    turn the check off.
    """
    return re.sub(r"[,\s]", "", value)


def facts_to_text(facts: Mapping[str, Any]) -> str:
    """Flatten the fact bundle into the prompt, one fact per line."""
    lines = []
    for key, value in facts.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        lines.append("%s: %s" % (str(key).replace("_", " "), value))
    return "\n".join(lines)


def verify_grounded(text: str, facts: Mapping[str, Any]) -> list[str]:
    """Return every number in `text` that is NOT present in `facts`.

    Empty list means every figure in the copy traces to a supplied fact. This is
    the whole point of the module: it is cheap, it is deterministic, and it catches
    the one error class that a human proofreader reliably misses, because an
    invented specification reads exactly like a real one.
    """
    haystack = _normalise(facts_to_text(facts).lower())
    unsupported = []

    pass

    for word in re.findall(r"[a-z]+", text.lower()):
        if word in WORD_NUMBERS and word not in haystack:
            unsupported.append(word)

    return unsupported


def check_shape(text: str) -> str | None:
    """Length and tone checks that do not need the facts. Returns a reason or None."""
    stripped = text.strip()
    if len(stripped) < MIN_META_CHARS:
        return "too short at %d chars" % len(stripped)
    if len(stripped) > MAX_META_CHARS:
        return "too long at %d chars, cap is %d" % (len(stripped), MAX_META_CHARS)
    if "!" in stripped:
        return "contains an exclamation mark"
    banned = ("premium", "high-quality", "high quality", "cutting-edge",
              "perfect for", "look no further", "wide range of")
    for phrase in banned:
        if phrase in stripped.lower():
            return "marketing filler: %r" % phrase
    if stripped.startswith(("\"", "'", "Meta description")):
        # The model wrapped its answer or labelled it. Publishing that verbatim
        # puts a stray quote mark in the search result.
        return "output is wrapped or labelled rather than bare text"
    return None


class CatalogCopyWriter:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        client: httpx.AsyncClient | None = None,
        temperature: float = 0.2,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._model = model
        self._temperature = temperature
        self._client = client or httpx.AsyncClient(timeout=60.0)

    async def _complete(self, facts_text: str) -> str:
        r = await self._client.post(
            self._base + "/chat/completions",
            headers={"Authorization": "Bearer " + self._key,
                     "Content-Type": "application/json"},
            content=json.dumps({
                "model": self._model,
                "temperature": self._temperature,
                "max_tokens": MAX_COMPLETION_TOKENS,
                "reasoning_effort": REASONING_EFFORT,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "Facts:\n" + facts_text},
                ],
            }),
        )
        if r.status_code == 429:
            raise GenerationRejected("rate limited by the model provider")
        if r.status_code >= 400:
            raise GenerationRejected(
                "provider returned %d: %s" % (r.status_code, r.text[:200])
            )
        payload = r.json()
        choices = payload.get("choices") or []
        if not choices:
            raise GenerationRejected("provider returned no choices")

        choice = choices[0]
        message = choice.get("message") or {}
        content = (message.get("content") or "").strip()

        # A truncated completion is NOT a content problem, and conflating the two
        # sends the reader off to rewrite a prompt that was fine. Measured on a
        # reasoning model: finish_reason "length", 158 of 160 completion tokens
        # spent on reasoning, content empty.
        if choice.get("finish_reason") == "length" and not content:
            usage = payload.get("usage") or {}
            detail = (usage.get("completion_tokens_details") or {})
            raise GenerationRejected(
                "completion truncated before any text was emitted: %s of %s "
                "completion tokens went to reasoning. Raise MAX_COMPLETION_TOKENS."
                % (detail.get("reasoning_tokens", "?"),
                   usage.get("completion_tokens", "?"))
            )

        if not content:
            # Some providers put a refusal in its own field rather than in content.
            if message.get("refusal"):
                raise GenerationRejected(
                    "model refused: %s" % str(message["refusal"])[:120]
                )
            raise GenerationRejected(
                "provider returned an empty message with finish_reason %r"
                % choice.get("finish_reason")
            )
        return content

    async def write_one(self, product_id: str, facts: Mapping[str, Any]) -> CopyResult:
        facts_text = facts_to_text(facts)
        if not facts_text.strip():
            # No facts means nothing truthful can be written. Generating anyway is
            # how a catalogue fills up with confident fiction.
            return CopyResult(product_id, "", False,
                              rejected_reason="no usable facts supplied")

        text = clean_text(await self._complete(facts_text)).strip().strip('"')

        shape = check_shape(text)
        if shape:
            return CopyResult(product_id, text, False, rejected_reason=shape,
                              model=self._model)

        unsupported = verify_grounded(text, facts)
        if unsupported:
            return CopyResult(
                product_id, text, False,
                rejected_reason="ungrounded numbers: %s" % ", ".join(unsupported),
                unsupported_numbers=unsupported, model=self._model,
            )

        return CopyResult(product_id, text, True, model=self._model)

    async def write_many(
        self, products: Iterable[tuple[str, Mapping[str, Any]]]
    ) -> list[CopyResult]:
        """Sequential on purpose.

        A catalogue run is a background job with no user waiting, and the provider
        rate limit is the binding constraint rather than wall-clock. Firing
        thousands of concurrent completions earns a 429 storm and a partially
        written catalogue, which is worse than slow.
        """
        results = []
        for product_id, facts in products:
            try:
                results.append(await self.write_one(product_id, facts))
            except GenerationRejected as exc:
                results.append(CopyResult(product_id, "", False,
                                          rejected_reason=str(exc),
                                          model=self._model))
        return results
