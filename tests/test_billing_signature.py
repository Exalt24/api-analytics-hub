"""The Stripe-Signature check, line by line against docs/vendor-contract/stripe/webhooks-signatures.txt: t and v1, an HMAC SHA256 over `timestamp.payload`,
a constant-time compare, several v1 values while a secret is rotated, a recency window that is never zero, and a rejection that never says which check failed.
Pure: no database."""
from __future__ import annotations

import hashlib
import hmac
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.billing import signature  # noqa: E402
from app.billing.errors import WebhookRejected  # noqa: E402

SECRET, OTHER = "whsec-invented-for-tests-one", "whsec-invented-for-tests-two"
NOW = 1_800_000_000
BODY = b'{"id":"evt_1","object":"event","type":"payment_intent.succeeded"}'


def header(payload=BODY, secrets=(SECRET,), ts=NOW):
    return signature.build_header(payload, secrets, ts)


def rejected(payload=BODY, hdr=None, secrets=(SECRET,), now=NOW, **kw):
    with pytest.raises(WebhookRejected) as exc:
        signature.verify(payload, hdr, secrets, now=now, **kw)
    return exc.value


def test_a_header_built_the_documented_way_verifies_and_returns_its_timestamp():
    assert signature.verify(BODY, header(), (SECRET,), now=NOW) == NOW


def test_the_signature_is_an_hmac_sha256_of_timestamp_dot_payload_keyed_with_the_secret():
    expected = hmac.new(SECRET.encode(), b"%d." % NOW + BODY, hashlib.sha256).hexdigest()
    assert signature.expected_signature(BODY, SECRET, NOW) == expected
    assert header() == "t=%d,v1=%s" % (NOW, expected)


def test_the_payload_is_the_raw_bytes_so_a_reserialised_body_does_not_verify():
    pretty = BODY.replace(b",", b", ")
    assert pretty != BODY
    rejected(pretty, header())


@pytest.mark.parametrize("change", ["flip_byte", "append", "truncate", "swap_secret", "other_timestamp"])
def test_a_tampered_payload_secret_or_timestamp_is_rejected(change):
    h = header()
    payload, secrets = BODY, (SECRET,)
    if change == "flip_byte":
        payload = BODY[:10] + bytes([BODY[10] ^ 1]) + BODY[11:]
    elif change == "append":
        payload = BODY + b" "
    elif change == "truncate":
        payload = BODY[:-1]
    elif change == "swap_secret":
        secrets = (OTHER,)
    else:
        h = h.replace("t=%d" % NOW, "t=%d" % (NOW + 1))
    rejected(payload, h, secrets)


def test_every_single_byte_change_of_the_body_is_rejected_over_seeded_random_bodies():
    rng = random.Random(20261030)
    for _ in range(60):
        body = bytes(rng.randrange(32, 127) for _ in range(rng.randrange(5, 120)))
        h = header(body)
        signature.verify(body, h, (SECRET,), now=NOW)
        i = rng.randrange(len(body))
        rejected(body[:i] + bytes([(body[i] + 1) % 256]) + body[i + 1:], h)


def test_a_stale_timestamp_is_rejected_after_the_signature_matches_and_the_edge_of_the_window_is_inclusive():
    tol = signature.DEFAULT_TOLERANCE_SECONDS
    assert tol == 300
    signature.verify(BODY, header(ts=NOW - tol), (SECRET,), now=NOW)
    err = rejected(BODY, header(ts=NOW - tol - 1))
    assert err.reason == "timestamp outside the tolerance"


def test_a_timestamp_in_the_future_is_rejected_as_well_as_one_in_the_past():
    signature.verify(BODY, header(ts=NOW + 300), (SECRET,), now=NOW)
    rejected(BODY, header(ts=NOW + 301))


def test_the_reason_differs_for_our_log_but_what_a_caller_could_echo_back_says_nothing():
    stale = rejected(BODY, header(ts=NOW - 10_000))
    forged = rejected(BODY, "t=%d,v1=%s" % (NOW, "0" * 64))
    assert stale.reason != forged.reason
    assert str(stale) == str(forged) == "webhook rejected"


@pytest.mark.parametrize("tolerance", [0, -1, -300])
def test_a_tolerance_of_zero_or_less_is_refused_because_it_would_switch_the_recency_check_off(tolerance):
    with pytest.raises(ValueError):
        signature.verify(BODY, header(), (SECRET,), now=NOW, tolerance=tolerance)


def test_during_a_secret_rotation_either_secret_verifies_and_a_header_with_both_signatures_matches_each():
    both = header(secrets=(SECRET, OTHER))
    assert both.count("v1=") == 2
    assert signature.verify(BODY, both, (SECRET,), now=NOW) == NOW
    assert signature.verify(BODY, both, (OTHER,), now=NOW) == NOW
    assert signature.verify(BODY, both, (OTHER, SECRET), now=NOW) == NOW
    rejected(BODY, both, ("a-third-secret",))


def test_a_valid_v1_listed_after_an_invalid_one_still_verifies():
    good = signature.expected_signature(BODY, SECRET, NOW)
    assert signature.verify(BODY, "t=%d,v1=%s,v1=%s" % (NOW, "f" * 64, good), (SECRET,), now=NOW) == NOW
    assert signature.verify(BODY, "t=%d,v1=%s,v1=%s" % (NOW, good, "f" * 64), (SECRET,), now=NOW) == NOW


def test_only_scheme_v1_counts_so_a_downgrade_to_v0_is_refused():
    good = signature.expected_signature(BODY, SECRET, NOW)
    rejected(BODY, "t=%d,v0=%s" % (NOW, good))
    assert signature.verify(BODY, "t=%d,v0=%s,v1=%s" % (NOW, "0" * 64, good), (SECRET,), now=NOW) == NOW


HEADERS = [None, "", " ", "garbage", "t=", "v1=", "t=abc,v1=" + "0" * 64, "t=-5,v1=" + "0" * 64, "t=%d" % NOW, "v1=" + "0" * 64, "t=1,t=2", ",,,", "t=%d,v1" % NOW,
           "t=%d,v1=%s" % (NOW, "é" * 64), "t=%d,v1=%s" % (NOW, "0" * 63), "t=%d,v1=%s" % (NOW, "0" * 65), "t=%d, v1 =%s" % (NOW, "0" * 64)]


@pytest.mark.parametrize("hdr", HEADERS)
def test_a_malformed_missing_or_odd_header_is_a_rejection_never_an_exception_of_another_kind(hdr):
    rejected(BODY, hdr)


def test_a_non_ascii_header_is_a_rejection_and_not_a_type_error_from_the_comparison():
    assert rejected(BODY, "t=%d,v1=%s" % (NOW, "é" * 64)).reason == "non-ascii header"


def test_no_signing_secret_configured_rejects_everything():
    assert rejected(BODY, header(), ()).reason == "no signing secret configured"


def test_every_received_signature_is_compared_with_the_constant_time_function(monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(signature.hmac, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    signature.verify(BODY, header(secrets=(SECRET, OTHER)), (SECRET, OTHER), now=NOW)
    assert len(calls) == 4          # two secrets against two signatures, none skipped after a match
