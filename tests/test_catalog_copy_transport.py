"""Transport-level failures of the catalogue copywriter.

Split from test_catalog_copy.py because these are about how a PROVIDER answers
rather than about whether the copy is any good, and the mutation harness flagged
that nothing was testing the most interesting one.

The truncation case is real and was measured against a live provider, not
imagined: a reasoning model spent 158 of 160 completion tokens on its reasoning
trace, returned finish_reason "length" with empty content, and the module reported
"too short at 0 chars". That message sends a reader off to rewrite a prompt that
was fine, when the actual fix is a bigger token budget. Two failures with opposite
remedies must not share a message.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.catalog_copy import CatalogCopyWriter, GenerationRejected, clean_text

FACTS = {
    "title": "MSE PRO High Purity Alumina Crucible, Rectangular Boat",
    "purity": "99.5%",
    "max_temperature": "1750 C",
}


def writer(handler):
    return CatalogCopyWriter(
        api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def test_a_truncated_completion_is_not_blamed_on_the_copy():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "length",
                "message": {"role": "assistant", "content": "", "reasoning": "..."},
            }],
            "usage": {
                "completion_tokens": 160,
                "completion_tokens_details": {"reasoning_tokens": 158},
            },
        })

    with pytest.raises(GenerationRejected) as exc:
        asyncio.run(writer(handler).write_one("gid://p/1", FACTS))
    message = str(exc.value)
    assert "truncated" in message, message
    assert "158" in message and "160" in message, "the token split must be reported"
    assert "too short" not in message, "a truncation is not a short-copy problem"


def test_a_truncated_completion_that_DID_emit_text_is_kept():
    """Control. finish_reason length with usable text is a long answer that got
    cut off, not a failed one, and discarding it throws away good copy."""
    text = "Rectangular alumina boat crucible, 99.5% Al2O3, rated to 1750 C."

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "length", "message": {"content": text}}],
            "usage": {"completion_tokens": 700},
        })

    res = asyncio.run(writer(handler).write_one("gid://p/1", FACTS))
    assert res.publishable, res.rejected_reason


def test_a_provider_refusal_is_reported_as_a_refusal():
    """Some providers put a refusal in its own field and leave content empty."""
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop",
                         "message": {"content": "", "refusal": "I cannot help"}}],
        })

    with pytest.raises(GenerationRejected) as exc:
        asyncio.run(writer(handler).write_one("gid://p/1", FACTS))
    assert "refused" in str(exc.value)


def test_an_empty_message_with_a_normal_finish_reason_still_errors():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": ""}}],
        })

    with pytest.raises(GenerationRejected) as exc:
        asyncio.run(writer(handler).write_one("gid://p/1", FACTS))
    assert "empty message" in str(exc.value)


def test_exotic_whitespace_is_normalised_before_any_check_runs():
    """A narrow no-break space came back from a real generation, between a figure
    and its unit. It breaks CSV exports, mojibakes through a cp1252 pipeline, and
    makes two visually identical strings compare unequal, which is how a
    de-duplication pass misses a duplicate."""
    dirty = (
        "Studio" + chr(0x202F) + "Monitor" + chr(0x00A0) + "Headphones "
        + chr(0x2013) + " 38" + chr(0x2009) + "ohms" + chr(0x200B) + "."
    )
    assert clean_text(dirty) == "Studio Monitor Headphones - 38 ohms."


def test_curly_quotes_become_plain_ones():
    """Smart quotes break a JSON-LD field if the copy is later embedded there."""
    dirty = chr(0x201C) + "boat" + chr(0x201D) + " crucible" + chr(0x2019) + "s"
    assert clean_text(dirty) == '"boat" crucible\'s'


def test_clean_text_leaves_ordinary_copy_untouched():
    """The control: normalisation must not rewrite words."""
    plain = "Rectangular alumina boat crucible, 99.5% Al2O3, rated to 1750 C."
    assert clean_text(plain) == plain


# --------------------------------------------- chemical formulas are not numbers

def test_a_chemical_formula_is_not_read_as_a_fabricated_number():
    """Al2O3 has digits in it. Reading them as quantities makes the guard fire on
    every formula in a materials catalogue, which is how a correct guard gets
    switched off."""
    from app.ai.catalog_copy import verify_grounded

    facts = {"title": "Alumina crucible", "material": "aluminium oxide"}
    assert verify_grounded("Alumina crucible in Al2O3 for furnace work.", facts) == []
    assert verify_grounded("Contains H2O and CO2 and Si3N4.", facts) == []


def test_a_real_quantity_is_still_caught_next_to_a_formula():
    """The control that keeps the exemption honest: the formula passes and the
    invented purity beside it does not."""
    from app.ai.catalog_copy import verify_grounded

    facts = {"title": "Alumina crucible", "material": "aluminium oxide"}
    out = verify_grounded("99.99% pure Al2O3 crucible.", facts)
    assert out == ["99.99"], out


def test_a_unit_written_without_a_space_is_still_checked():
    """38ohms must not slip through just because a letter follows the digits. The
    lookbehind is deliberately one-sided for exactly this reason."""
    from app.ai.catalog_copy import verify_grounded

    facts = {"title": "Headphones", "impedance": "32 ohms"}
    assert verify_grounded("Rated at 38ohms nominal.", facts) == ["38"]
