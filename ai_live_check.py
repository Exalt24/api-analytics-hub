#!/usr/bin/env python
"""Run the catalogue copywriter against a REAL model and a REAL product.

Two things this proves that the mocked tests cannot. First, that the prompt
actually produces publishable copy from a live model rather than from a fixture we
wrote to pass. Second, and more useful, that the grounding guard fires on a real
hallucination: the second pass deliberately withholds the purity from the facts
while leaving it in the title, which is the exact condition under which a model
restates a number it was not given.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

# A Windows console is cp1252 by default, and the first successful
# generation contained U+202F, so printing the result crashed the script
# that exists to prove the result.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.ai.catalog_copy import CatalogCopyWriter, verify_grounded

SHOPIFY = Path(r"C:\Projects\Professional\Operations\.secrets\shopify_dev_token.txt")

PRODUCTS_QUERY = """
query {
  products(first: 20) {
    nodes {
      id
      title
      variants(first: 1) { nodes { price } }
      metafields(namespace: "specs", first: 20) {
        nodes { key value type }
      }
    }
  }
}
"""


async def real_product() -> tuple[str, dict]:
    text = io.open(SHOPIFY, encoding="utf-8").read()
    token = re.search(r"^ACCESS_TOKEN=(.+)$", text, re.M).group(1).strip()
    shop = re.search(r"^SHOP=(.+)$", text, re.M).group(1).strip()
    async with httpx.AsyncClient(timeout=40.0) as client:
        r = await client.post(
            "https://%s/admin/api/2026-07/graphql.json" % shop,
            headers={"X-Shopify-Access-Token": token,
                     "Content-Type": "application/json"},
            content=json.dumps({"query": PRODUCTS_QUERY}),
        )
        nodes = r.json()["data"]["products"]["nodes"]
    # Pick the product with the MOST specs. A grounded generator can only be
    # judged on a product that actually has facts; the first run picked a Gift
    # Card with a title and a price and correctly produced nothing usable.
    def spec_count(p):
        return len(((p.get("metafields") or {}).get("nodes") or []))

    p = sorted(nodes, key=spec_count, reverse=True)[0]
    facts = {"title": p["title"]}
    for mf in ((p.get("metafields") or {}).get("nodes") or []):
        facts[mf["key"]] = mf["value"]
    return p["id"], facts


async def main() -> int:
    key = os.environ.get("GROQ_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        raise SystemExit("set GROQ_API_KEY")

    writer = CatalogCopyWriter(api_key=key)

    pid, facts = await real_product()
    print("product:", facts["title"])
    print("facts given:", json.dumps(facts))
    res = await writer.write_one(pid, facts)
    print()
    print("generated :", res.text)
    print("grounded  :", res.grounded)
    print("publishable:", res.publishable, "|", res.rejected_reason or "no reason")

    # The adversarial half. A rich-sounding fact set with ONE number deliberately
    # absent from the facts but present in the title, so a restated figure is
    # ungrounded by construction.
    print()
    print("=== adversarial pass: NO number anywhere in the facts ===")
    # The first version of this trap left 99.5% in the TITLE, which is a supplied
    # fact, so the model restating it was grounded and the guard was right to
    # allow it. That test could not fail and therefore measured nothing.
    #
    # This version supplies no digits at all, so any figure in the output is
    # fabricated by construction. A model asked about a technical product with no
    # specs is strongly inclined to supply the specs it has seen elsewhere, which
    # is exactly the failure mode this guard exists for.
    trap_facts = {
        "title": "High Purity Alumina Crucible",
        "material": "aluminium oxide",
        "shape": "rectangular boat",
        "use": "laboratory furnace work",
    }
    trap = await writer.write_one("gid://trap/1", trap_facts)
    print("generated :", trap.text)
    print("unsupported numbers:", trap.unsupported_numbers or "none")
    print("publishable:", trap.publishable, "|", trap.rejected_reason or "no reason")

    # ------------------------------------------------------------------ layer 2
    print()
    print("=== UNGUARDED PROMPT: grounding rule stripped, verifier must catch it ===")
    # Same model, same facts, but the system prompt no longer forbids invention.
    # This is what a prompt regression or a provider default change looks like, and
    # the point is that the CODE layer still refuses to publish.
    import httpx as _httpx

    from app.ai.catalog_copy import check_shape, clean_text

    loose_prompt = (
        "You write meta descriptions for a technical product catalogue. Write one "
        "or two sentences under 150 characters. Include concrete specifications so "
        "the description is useful to a researcher comparing products."
    )
    async with _httpx.AsyncClient(timeout=60.0) as raw:
        r = await raw.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"},
            json={
                "model": "openai/gpt-oss-120b",
                "temperature": 0.4,
                "max_tokens": 700,
                "reasoning_effort": "low",
                "messages": [
                    {"role": "system", "content": loose_prompt},
                    {"role": "user", "content": "Facts:\n" + "\n".join(
                        "%s: %s" % kv for kv in trap_facts.items())},
                ],
            },
        )
    loose_text = clean_text(
        ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    )
    print("generated :", loose_text or "(empty)")
    bad = verify_grounded(loose_text, trap_facts)
    print("unsupported numbers:", bad or "none")
    print("shape check       :", check_shape(loose_text) or "ok")
    print("WOULD PUBLISH     :", (not bad) and check_shape(loose_text) is None)
    if bad:
        print("  -> the verifier refused output the prompt alone let through")

    # A direct check that the verifier is not vacuous on this very output.
    print()
    print("verifier on a KNOWN fabrication:",
          verify_grounded("Rated to 9999 C and 99.999% pure.", facts))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
