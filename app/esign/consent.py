"""The consent text a signer agrees to, its versions, and the rule that a typed name is the named signer's.

THE TEXT IS VERSIONED. A request stores the version that was current when it was made, the signer is shown that version, and the signature record
holds the version and the SHA-256 of the text, so what was agreed to can be shown later. An edited text gets a new version id and the old one stays
here unchanged. The text covers the four ideas the US ESIGN Act and UETA turn on: consent to use electronic records and signatures, the intent to
sign, the association of the signature with this record, and retention of the record. It is a plain statement and not legal advice.
"""
from __future__ import annotations

import hashlib
import unicodedata

CURRENT_VERSION = "2026-11-14.1"

CONSENT_TEXTS = {
    "2026-11-14.1": (
        "Consent to sign electronically\n"
        "\n"
        "1. Electronic records. I agree to receive, review and sign this document as an electronic record instead of on paper. "
        "I can see the document, and I can download a copy of it, before I sign. I may ask the firm for a paper copy, and I may decline to sign "
        "electronically by choosing Decline, before I sign.\n"
        "2. Intent to sign. I intend to sign this document. Typing my full name and pressing Sign is my signature on it, with the same effect as "
        "a handwritten one.\n"
        "3. This signature belongs to this record. My signature is tied to this document by its SHA-256 fingerprint, shown on this page, and to "
        "this request. If the document changes, the fingerprint changes and the signature no longer matches it.\n"
        "4. Retention. The signed document, my signature record and the audit trail of this request are kept by the firm and by this service. "
        "I can ask the firm for a copy of the signature certificate at any time.\n"
    ),
}


def consent_text(version: str) -> str:
    """The text of one version. An unknown version is an error here, never a default, because signing against text nobody can name proves nothing."""
    try:
        return CONSENT_TEXTS[version]
    except KeyError:
        raise ValueError("unknown consent version %r" % (version,)) from None


def consent_text_sha256(version: str) -> str:
    return hashlib.sha256(consent_text(version).encode("utf-8")).hexdigest()


def normalise_name(value: str) -> str:
    """NFKC, runs of whitespace collapsed to one space, the ends trimmed, case folded."""
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def names_match(typed: str, signer: str) -> bool:
    """THE NAME RULE. The typed name equals the named signer's name after both are normalised (normalise_name): upper and lower case do not matter,
    extra spaces do not matter, and nothing else is forgiven. A different spelling, a missing surname, an initial or an empty name is a mismatch.
    The rule is deliberately strict and simple, so a signer who types the name on the request has signed on purpose; it does not prove who typed it."""
    a = normalise_name(typed)
    return bool(a) and a == normalise_name(signer)
