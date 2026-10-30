"""Verify a Stripe-style `Stripe-Signature` header, the way Stripe documents it for a manual check
(docs/vendor-contract/stripe/webhooks-signatures.txt, fetched 2026-10-30 from https://docs.stripe.com/webhooks?verify=verify-manually).

WHAT THE PAGE SAYS, and where each line is used here:

  "The timestamp has a t= prefix, and each signature has a scheme prefix. Schemes start with v, followed by an integer. Currently, the only valid
  live signature scheme is v1."                                                          -> parse_header keeps t and every v1 and ignores the rest
  "To prevent downgrade attacks, ignore all schemes that aren't v1."                      -> a v0 signature is never accepted
  "You can have multiple signatures with the same scheme-secret pair when you roll an endpoint's secret, and keep the previous secret active for
  up to 24 hours. During this time, your endpoint has multiple active secrets and Stripe generates one signature for each secret."
                                                                                          -> verify takes several v1 values and several secrets
  "Split the header using the , character as the separator to get a list of elements. Then split each element using the = character as the
  separator to get a prefix and value pair."                                              -> parse_header
  "The signed_payload string is created by concatenating: The timestamp (as a string), The character ., The actual JSON payload (that is, the
  request body)"                                                                          -> signed_payload; the payload is the RAW bytes, never re-serialised
  "Compute an HMAC with the SHA256 hash function. Use the endpoint's signing secret as the key, and use the signed_payload string as the message."
                                                                                          -> expected_signature
  "To protect against timing attacks, use a constant-time-string comparison to compare the expected signature to each of the received signatures."
                                                                                          -> hmac.compare_digest against every received value
  "If the signature is valid but the timestamp is too old, you can have your application reject the payload." and "Our libraries have a default
  tolerance of 5 minutes between the timestamp and the current time." and "Don't use a tolerance value of 0. Using a tolerance value of 0 disables
  the recency check entirely."                                                            -> DEFAULT_TOLERANCE_SECONDS = 300; a tolerance of zero or less is refused

The recency check runs AFTER the signature matches, as the page orders it. The window is applied to the absolute difference, so a timestamp far in
the future is refused as well as one far in the past. The reason is kept on the exception for our own log and is never sent back to the caller.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Iterable, Sequence

from .errors import WebhookRejected

DEFAULT_TOLERANCE_SECONDS = 300
SIGNATURE_HEADER = "Stripe-Signature"


def signed_payload(timestamp: int, payload: bytes) -> bytes:
    return str(timestamp).encode("ascii") + b"." + payload


def expected_signature(payload: bytes, secret: str, timestamp: int) -> str:
    return hmac.new(secret.encode("utf-8"), signed_payload(timestamp, payload), hashlib.sha256).hexdigest()


def build_header(payload: bytes, secrets: Iterable[str], timestamp: int) -> str:
    """The header a sender builds: one v1 per active secret. Used by the fake provider and the tests, never by the receiver."""
    return ",".join(["t=%d" % timestamp] + ["v1=%s" % expected_signature(payload, s, timestamp) for s in secrets])


def parse_header(header: str) -> tuple[int, list[str]]:
    """(timestamp, [v1 signatures]) from the header. Anything that is not t or v1 is dropped; a missing or non-numeric t, or no v1, is malformed."""
    timestamp: int | None = None
    sigs: list[str] = []
    for element in header.split(","):
        prefix, sep, value = element.strip().partition("=")
        if not sep:
            continue
        if prefix == "t":
            if not value.isdigit():
                raise WebhookRejected("malformed timestamp")
            timestamp = int(value)
        elif prefix == "v1":
            sigs.append(value)
    if timestamp is None:
        raise WebhookRejected("no timestamp")
    if not sigs:
        raise WebhookRejected("no v1 signature")
    return timestamp, sigs


def verify(payload: bytes, header: str | None, secrets: Sequence[str], *, now: float, tolerance: int = DEFAULT_TOLERANCE_SECONDS) -> int:
    """Return the event's timestamp if the header is a valid, recent signature of `payload` under any of `secrets`; raise WebhookRejected otherwise."""
    if tolerance <= 0:
        raise ValueError("a tolerance of zero or less disables the recency check; use a positive number of seconds")
    if not secrets:
        raise WebhookRejected("no signing secret configured")
    if not header:
        raise WebhookRejected("no signature header")
    # compare_digest raises on non-ASCII text, which would turn a rejection into a 500; a signature is hex, so non-ASCII is simply wrong
    if not header.isascii():
        raise WebhookRejected("non-ascii header")
    timestamp, received = parse_header(header)
    matched = False
    for secret in secrets:
        expected = expected_signature(payload, secret, timestamp)
        for candidate in received:
            # no early exit: every pair is compared, so how many secrets or signatures matched is not visible in the time taken
            if hmac.compare_digest(expected.encode("ascii"), candidate.encode("ascii")):
                matched = True
    if not matched:
        raise WebhookRejected("signature mismatch")
    if abs(now - timestamp) > tolerance:
        raise WebhookRejected("timestamp outside the tolerance")
    return timestamp
