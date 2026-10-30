"""Match a competitor listing to a row of our catalogue, and be careful about it.

A wrong match is worse than a missing one: it puts a price change in front of a person for the wrong product. So the order is
strict and every uncertain case goes to a review list instead of being decided here.

  1. GTIN. Both sides carry a valid GTIN (check digit verified, 8, 12, 13 or 14 digits, compared as 14) and they are equal:
     matched, score 1.0. Two valid GTINs that DIFFER mean different products, whatever the titles say; that candidate is
     dropped. Two catalogue rows with the same GTIN is a catalogue problem and goes to review.
  2. SKU. Equal after upper-casing and dropping punctuation. A SKU is only as unique as the shop that wrote it, so a SKU
     match also needs the titles to share at least SKU_TITLE_FLOOR of their tokens, otherwise it goes to review.
  3. Title. Normalised tokens (lower case, accents folded, a few stop words dropped, "500 ml" glued to "500ml"), scored by
     overlap: the sum of per-token credit over (tokens in A + tokens in B - credit). A token is worth 1.0 if equal and its
     similarity if two spellings are 0.88 or more alike ("moisturising" and "moisturizing"); numbers and sizes only match
     exactly. Then two rules that exist because overlap alone is wrong about size:
       * if both titles carry a number or size token and the sets differ (750ml against 500ml, 48V against 36V), the score is
         capped at SIZE_CONFLICT_CAP, which is below the review line, so a different size is never even a candidate;
       * if we know our brand and no token of it appears in the competitor's title, the score is capped at BRAND_MISSING_CAP.
     AUTO_THRESHOLD or more: matched. REVIEW_THRESHOLD up to that: needs_review. Less: no_match.
  4. Ties. If the best two catalogue rows for one listing score within TIE_MARGIN of each other and both reach the review
     line, nothing is matched: needs_review, with both named.

Nothing here reads the database. The thresholds are module constants so a test can pin them and a reviewer can see them.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Sequence

AUTO_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.60
SKU_TITLE_FLOOR = 0.30
SIZE_CONFLICT_CAP = 0.50
BRAND_MISSING_CAP = 0.59
TIE_MARGIN = 0.03
TOKEN_SIMILARITY = 0.88

_STOP = frozenset({"the", "a", "an", "and", "for", "with", "of", "new", "in", "by", "to"})
_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|l|g|kg|oz|lb|cm|mm|m|v|ah|wh|w|gb|tb|pack|pk|pcs|ct)\b")


@dataclass(frozen=True)
class CatalogueItem:
    id: str
    sku: str
    title: str
    gtin: str | None = None
    brand: str | None = None


@dataclass(frozen=True)
class CompetitorItem:
    title: str | None
    sku: str | None = None
    gtin: str | None = None


@dataclass(frozen=True)
class MatchResult:
    status: str                        # matched, needs_review or no_match
    catalogue_id: str | None
    score: float
    method: str | None                 # gtin, sku or title
    reason: str
    candidates: tuple[tuple[str, float], ...] = field(default_factory=tuple)


def gtin_check_digit(body: str) -> int:
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body)))
    return (10 - total % 10) % 10


def normalise_gtin(raw: str | None) -> str | None:
    """14 digits if `raw` is a GTIN-8, 12, 13 or 14 with a correct check digit, else None."""
    if not raw:
        return None
    digits = re.sub(r"[\s-]", "", str(raw))
    if not digits.isdigit() or len(digits) not in (8, 12, 13, 14):
        return None
    if gtin_check_digit(digits[:-1]) != int(digits[-1]):
        return None
    return digits.zfill(14)


def normalise_sku(raw: str | None) -> str | None:
    s = re.sub(r"[^A-Z0-9]", "", (raw or "").upper())
    return s or None


def tokens(title: str | None) -> list[str]:
    t = unicodedata.normalize("NFKD", (title or "").lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.replace("litre", "l").replace("liter", "l")
    t = _UNIT_RE.sub(lambda m: m.group(1) + m.group(2), t)
    return [w for w in re.findall(r"[a-z0-9.]+", t) if w.strip(".") and w not in _STOP]


def _is_number_token(tok: str) -> bool:
    return any(ch.isdigit() for ch in tok)


def title_score(a_title: str | None, b_title: str | None, *, brand: str | None = None) -> tuple[float, str]:
    """(score, note). The note names the rule that capped the score, or is empty."""
    a, b = tokens(a_title), tokens(b_title)
    if not a or not b:
        return 0.0, "a title is empty"
    unmatched_b = list(b)
    credit = 0.0
    for tok in a:
        if tok in unmatched_b:
            unmatched_b.remove(tok)
            credit += 1.0
            continue
        if _is_number_token(tok):
            continue
        best, best_sim = None, 0.0
        for other in unmatched_b:
            if _is_number_token(other):
                continue
            sim = SequenceMatcher(None, tok, other).ratio()
            if sim > best_sim:
                best, best_sim = other, sim
        if best is not None and best_sim >= TOKEN_SIMILARITY:
            unmatched_b.remove(best)
            credit += best_sim
    score = credit / (len(a) + len(b) - credit)
    note = ""
    na, nb = {t for t in a if _is_number_token(t)}, {t for t in b if _is_number_token(t)}
    if na and nb and na != nb:
        if score > SIZE_CONFLICT_CAP:
            note = "size or model differs (%s against %s)" % (", ".join(sorted(na)), ", ".join(sorted(nb)))
        score = min(score, SIZE_CONFLICT_CAP)
    if brand:
        brand_tokens = tokens(brand)
        if brand_tokens and not all(any(SequenceMatcher(None, bt, t).ratio() >= TOKEN_SIMILARITY for t in b) for bt in brand_tokens):
            if score > BRAND_MISSING_CAP:
                note = "brand %r not in the competitor title" % brand
            score = min(score, BRAND_MISSING_CAP)
    return round(score, 3), note


def match_listing(item: CompetitorItem, catalogue: Sequence[CatalogueItem]) -> MatchResult:
    their_gtin = normalise_gtin(item.gtin)
    their_sku = normalise_sku(item.sku)

    if their_gtin:
        same = [c for c in catalogue if normalise_gtin(c.gtin) == their_gtin]
        if len(same) == 1:
            return MatchResult("matched", same[0].id, 1.0, "gtin", "same GTIN", ((same[0].id, 1.0),))
        if len(same) > 1:
            return MatchResult("needs_review", None, 1.0, "gtin", "%d catalogue rows share this GTIN" % len(same),
                               tuple((c.id, 1.0) for c in same))

    scored: list[tuple[str, float, str, str | None]] = []   # id, score, note, method
    for c in catalogue:
        our_gtin = normalise_gtin(c.gtin)
        if their_gtin and our_gtin and our_gtin != their_gtin:
            continue  # two valid, different GTINs: different products whatever the titles say
        score, note = title_score(c.title, item.title, brand=c.brand)
        method = "title"
        if their_sku and their_sku == normalise_sku(c.sku):
            if score >= SKU_TITLE_FLOOR:
                scored.append((c.id, max(score, 0.95), "same SKU", "sku"))
            else:
                scored.append((c.id, REVIEW_THRESHOLD, "same SKU but the titles share almost nothing", "sku"))
            continue
        scored.append((c.id, score, note, method))

    if not scored:
        return MatchResult("no_match", None, 0.0, None, "no candidate")
    scored.sort(key=lambda r: r[1], reverse=True)
    best_id, best, note, method = scored[0]
    cands = tuple((cid, sc) for cid, sc, _, _ in scored[:3] if sc >= REVIEW_THRESHOLD)

    if best < REVIEW_THRESHOLD:
        return MatchResult("no_match", None, best, None, note or "best title score %.2f is under %.2f" % (best, REVIEW_THRESHOLD), cands)
    if len(scored) > 1 and scored[1][1] >= REVIEW_THRESHOLD and best - scored[1][1] <= TIE_MARGIN:
        return MatchResult("needs_review", None, best, method, "tie between %d catalogue rows" % len([s for s in scored if best - s[1] <= TIE_MARGIN]), cands)
    if best >= AUTO_THRESHOLD:
        return MatchResult("matched", best_id, best, method, note or ("same SKU" if method == "sku" else "title score %.2f" % best), cands)
    return MatchResult("needs_review", best_id, best, method, note or "title score %.2f is under the auto line %.2f" % (best, AUTO_THRESHOLD), cands)
