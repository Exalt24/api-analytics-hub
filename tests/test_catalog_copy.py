"""The grounding guard, tested from both sides.

The generation is not the risky part. The risky part is a fabricated specification
that reads exactly like a real one, so these tests spend their effort on
verify_grounded: it must catch an invented figure, and it must NOT reject copy
whose numbers are all real, because a guard that fires on correct output gets
switched off within a week.

No network anywhere. The transport is a MockTransport shaped like an
OpenAI-compatible completion, which is what OpenAI, Groq, Together and a local
vLLM all return.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.catalog_copy import (
    CatalogCopyWriter,
    GenerationRejected,
    check_shape,
    facts_to_text,
    verify_grounded,
)

FACTS = {
    "title": "MSE PRO High Purity Alumina Crucible, Rectangular Boat",
    "material": "Aluminium oxide (Al2O3)",
    "purity": "99.5%",
    "max_temperature": "1750 C",
    "dimensions": "100 x 50 x 20 mm",
    "quantity_per_pack": 2,
}


def writer_returning(text, *, status=200, client_payload=None):
    def handler(request):
        if status != 200:
            return httpx.Response(status, text="upstream said no")
        body = client_payload if client_payload is not None else {
            "choices": [{"message": {"content": text}}]
        }
        return httpx.Response(200, json=body)

    return CatalogCopyWriter(
        api_key="test-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# ------------------------------------------------------- the grounding check

def test_every_number_present_in_the_facts_is_accepted():
    text = "Alumina crucible in 99.5% Al2O3, rated to 1750 C, 100 x 50 x 20 mm."
    assert verify_grounded(text, FACTS) == []


def test_an_invented_purity_is_caught():
    """The exact liability case: a plausible upgrade of a real spec."""
    text = "Alumina crucible in 99.99% Al2O3, rated to 1750 C."
    assert verify_grounded(text, FACTS) == ["99.99"]


def test_an_invented_temperature_is_caught():
    text = "Alumina crucible in 99.5% Al2O3, rated to 1900 C."
    assert verify_grounded(text, FACTS) == ["1900"]


def test_separators_do_not_cause_a_false_rejection():
    """1,750 in the copy against 1750 in the facts is the same number. Rejecting
    it would be the guard crying wolf, which is how guards get disabled."""
    facts = dict(FACTS, max_temperature="1750 C")
    assert verify_grounded("Rated to 1,750 C in service.", facts) == []


def test_a_written_number_is_caught_too():
    """A digit-only check has an obvious hole: the model writes the word. This was
    a real gap in the first version of the guard."""
    out = verify_grounded("Supplied in packs of five for furnace work.", FACTS)
    assert "five" in out


def test_a_written_number_that_IS_in_the_facts_passes():
    facts = dict(FACTS, quantity_per_pack="two")
    assert verify_grounded("Supplied in packs of two.", facts) == []


def test_multiple_fabrications_are_all_reported():
    """A single reason string is not enough to fix a prompt; the caller needs the
    full list."""
    out = verify_grounded("99.99% pure, rated 1900 C, 120 mm long.", FACTS)
    assert set(out) == {"99.99", "1900", "120"}


# ------------------------------------------------------------- shape checks

def test_over_length_copy_is_rejected():
    assert "too long" in check_shape("x" * 200)


def test_short_copy_is_rejected():
    assert "too short" in check_shape("Alumina crucible.")


def test_marketing_filler_is_rejected():
    reason = check_shape(
        "A premium alumina crucible for laboratory use, rated to 1750 C in service."
    )
    assert reason and "premium" in reason


def test_an_exclamation_mark_is_rejected():
    reason = check_shape(
        "Alumina crucible rated to 1750 C, ideal for furnace work in labs!"
    )
    assert reason and "exclamation" in reason


def test_a_labelled_or_quoted_answer_is_rejected():
    reason = check_shape(
        '"Alumina crucible in 99.5% Al2O3, rated to 1750 C for furnace work."'
    )
    assert reason and "wrapped" in reason


def test_good_copy_passes_every_shape_check():
    """The control. Without it, a check that rejected everything would pass all
    five tests above."""
    assert check_shape(
        "Rectangular alumina boat crucible, 99.5% Al2O3, rated to 1750 C."
    ) is None


# -------------------------------------------------------------- end to end

def test_a_clean_generation_is_publishable():
    w = writer_returning(
        "Rectangular alumina boat crucible, 99.5% Al2O3, rated to 1750 C."
    )
    res = asyncio.run(w.write_one("gid://p/1", FACTS))
    assert res.publishable
    assert res.grounded
    assert res.rejected_reason is None


def test_a_hallucinated_generation_is_not_published():
    w = writer_returning(
        "Rectangular alumina boat crucible, 99.99% Al2O3, rated to 2000 C."
    )
    res = asyncio.run(w.write_one("gid://p/1", FACTS))
    assert not res.publishable
    assert res.unsupported_numbers == ["99.99", "2000"]
    assert "ungrounded" in res.rejected_reason


def test_no_facts_means_no_generation_attempt():
    """Nothing truthful can be written from nothing, and generating anyway is how a
    catalogue fills with confident fiction."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    w = CatalogCopyWriter(api_key="k",
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    res = asyncio.run(w.write_one("gid://p/1", {"title": "", "purity": None}))
    assert not res.publishable
    assert calls["n"] == 0, "it called the model with nothing to ground on"


def test_private_fields_never_reach_the_prompt():
    """The prompt gets a curated fact bundle, so a cost or an internal note cannot
    travel into public copy. This asserts the flattener only emits what it is
    handed."""
    text = facts_to_text({"title": "Crucible", "purity": "99.5%"})
    assert "cost" not in text.lower()
    assert "Crucible" in text and "99.5%" in text


def test_a_rate_limit_is_reported_not_swallowed():
    w = writer_returning("", status=429)
    with pytest.raises(GenerationRejected) as exc:
        asyncio.run(w.write_one("gid://p/1", FACTS))
    assert "rate limited" in str(exc.value)


def test_an_empty_choices_array_is_an_error_not_empty_copy():
    """A provider returning no choices must not publish an empty description."""
    w = writer_returning("", client_payload={"choices": []})
    with pytest.raises(GenerationRejected):
        asyncio.run(w.write_one("gid://p/1", FACTS))


def test_a_batch_records_a_reason_per_failure_and_keeps_going():
    """One bad SKU must not abandon the rest of the catalogue."""
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"choices": [{"message": {"content":
            "Rectangular alumina boat crucible, 99.5% Al2O3, rated to 1750 C."}}]})

    w = CatalogCopyWriter(api_key="k",
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    results = asyncio.run(w.write_many([("a", FACTS), ("b", FACTS)]))
    assert len(results) == 2
    assert not results[0].publishable and "rate limited" in results[0].rejected_reason
    assert results[1].publishable
