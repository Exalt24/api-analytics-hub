"""Credential encryption tests.

The one that matters most is the cross-row test. Row-level security stops a query
from reading another tenant's row, and it does nothing at all about a ciphertext
being copied between rows by anything with write access. Binding the AAD to the
row is what closes that, and this suite proves the binding is real rather than
decorative, by moving a blob and asserting it stops working.
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crypto import (
    CredentialCipher,
    CryptoError,
    DecryptionFailed,
    KeyUnavailable,
    Secret,
    generate_key,
)

K1 = base64.urlsafe_b64decode(generate_key() + "==")
K2 = base64.urlsafe_b64decode(generate_key() + "==")

TENANT = "11111111-1111-1111-1111-111111111111"
CONN = "22222222-2222-2222-2222-222222222222"
OTHER_CONN = "33333333-3333-3333-3333-333333333333"


def cipher(keys=None):
    return CredentialCipher(keys or {1: K1})


def aad(tenant=TENANT, conn=CONN, platform="shopify"):
    return CredentialCipher.aad(tenant, conn, platform)


# ------------------------------------------------------------------ round trip

def test_round_trip_returns_the_original_secret():
    c = cipher()
    blob = c.encrypt("shpat_live_token", aad())
    assert c.decrypt(blob, aad()).reveal() == "shpat_live_token"


def test_the_plaintext_is_not_present_in_the_envelope():
    """The test that would catch an accidental no-op encrypt."""
    c = cipher()
    blob = c.encrypt("shpat_live_token", aad())
    assert b"shpat_live_token" not in blob


def test_two_encryptions_of_the_same_value_differ():
    """A repeated GCM nonce breaks confidentiality AND integrity, so identical
    output for identical input is a serious defect, not a curiosity."""
    c = cipher()
    a = c.encrypt("same", aad())
    b = c.encrypt("same", aad())
    assert a != b
    assert a[1:13] != b[1:13], "nonce repeated"


# ------------------------------------------------------- the binding is real

def test_a_ciphertext_moved_to_another_connection_row_will_not_decrypt():
    """The attack RLS cannot stop: copy the blob into a row you control."""
    c = cipher()
    blob = c.encrypt("shpat_tenant_a", aad())
    with pytest.raises(DecryptionFailed):
        c.decrypt(blob, aad(conn=OTHER_CONN))


def test_a_ciphertext_moved_to_another_tenant_will_not_decrypt():
    c = cipher()
    blob = c.encrypt("shpat_tenant_a", aad())
    with pytest.raises(DecryptionFailed):
        c.decrypt(blob, aad(tenant="99999999-9999-9999-9999-999999999999"))


def test_relabelling_the_platform_will_not_decrypt():
    """Otherwise a Shopify token could be presented to the Meta connector."""
    c = cipher()
    blob = c.encrypt("shpat_x", aad())
    with pytest.raises(DecryptionFailed):
        c.decrypt(blob, aad(platform="meta"))


def test_the_binding_control_still_decrypts_with_the_right_aad():
    """Control. Without this, a cipher that ALWAYS failed would pass the three
    tests above and look perfectly secure."""
    c = cipher()
    blob = c.encrypt("shpat_x", aad())
    assert c.decrypt(blob, aad()).reveal() == "shpat_x"


# ---------------------------------------------------------------- tampering

@pytest.mark.parametrize("index", [0, 3, 15, -1])
def test_any_altered_byte_is_rejected(index):
    c = cipher({1: K1, 2: K2})
    blob = bytearray(c.encrypt("shpat_x", aad()))
    blob[index] ^= 0x01
    with pytest.raises((DecryptionFailed, KeyUnavailable)):
        c.decrypt(bytes(blob), aad())


def test_a_truncated_envelope_is_rejected_clearly():
    c = cipher()
    blob = c.encrypt("shpat_x", aad())
    with pytest.raises(DecryptionFailed):
        c.decrypt(blob[:10], aad())


def test_the_wrong_key_is_rejected():
    blob = cipher({1: K1}).encrypt("shpat_x", aad())
    with pytest.raises(DecryptionFailed):
        cipher({1: K2}).decrypt(blob, aad())


# ---------------------------------------------------------------- rotation

def test_rotation_encrypts_with_the_newest_key_and_still_reads_the_old_one():
    old = cipher({1: K1})
    legacy = old.encrypt("written_under_v1", aad())

    rotated = cipher({1: K1, 2: K2})
    assert rotated.current_version == 2
    fresh = rotated.encrypt("written_under_v2", aad())
    assert fresh[0] == 2
    # The whole point: no migration needed to keep reading old rows.
    assert rotated.decrypt(legacy, aad()).reveal() == "written_under_v1"
    assert rotated.decrypt(fresh, aad()).reveal() == "written_under_v2"


def test_a_retired_key_gives_a_distinct_error_from_tampering():
    """Different operational response: one is a misconfigured deploy with intact
    data, the other is data that cannot be trusted."""
    legacy = cipher({1: K1}).encrypt("x", aad())
    with pytest.raises(KeyUnavailable):
        cipher({2: K2}).decrypt(legacy, aad())


# ------------------------------------------------------------- key material

def test_a_wrong_length_key_is_refused_at_construction():
    with pytest.raises(CryptoError):
        CredentialCipher({1: b"tooshort"})


def test_missing_environment_refuses_rather_than_falling_back():
    """A silent plaintext fallback is how unencrypted rows get written for months."""
    saved = os.environ.pop("CREDENTIAL_KEYS", None)
    try:
        with pytest.raises(CryptoError):
            CredentialCipher()
    finally:
        if saved is not None:
            os.environ["CREDENTIAL_KEYS"] = saved


def test_keys_parse_from_the_environment_string():
    os.environ["CREDENTIAL_KEYS"] = "1:%s,2:%s" % (generate_key(), generate_key())
    try:
        c = CredentialCipher()
        assert c.current_version == 2
        assert c.decrypt(c.encrypt("v", aad()), aad()).reveal() == "v"
    finally:
        os.environ.pop("CREDENTIAL_KEYS", None)


def test_a_version_outside_one_byte_is_refused():
    os.environ["CREDENTIAL_KEYS"] = "300:%s" % generate_key()
    try:
        with pytest.raises(CryptoError):
            CredentialCipher()
    finally:
        os.environ.pop("CREDENTIAL_KEYS", None)


# ---------------------------------------------------------------- the wrapper

def test_a_secret_does_not_print_itself():
    s = Secret("shpat_live_token")
    assert "shpat_live_token" not in repr(s)
    assert "shpat_live_token" not in str(s)
    assert "shpat_live_token" not in "%r" % s


def test_formatting_a_secret_raises_instead_of_redacting():
    """Redacting an f-string hides a real mistake; raising forces the fix."""
    s = Secret("shpat_live_token")
    with pytest.raises(TypeError):
        "{}".format(s)
    with pytest.raises(TypeError):
        f"{s}"


def test_reveal_is_the_only_way_out():
    assert Secret("abc").reveal() == "abc"
