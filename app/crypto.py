"""Encryption for stored third-party credentials.

WHY THIS FILE EXISTS AT ALL, stated plainly because the absence was the bug:
`migrations/001_init.sql` described `platform_connection.credentials_enc` as
"Fernet ciphertext" and `registry.py` promised "secret fields are encrypted at
rest", and no encryption code existed anywhere in the project. A comment is not an
implementation. Anything reading those two lines would have concluded the
credentials were protected.

DESIGN DECISIONS, each one because the obvious alternative fails a specific way.

AES-256-GCM rather than Fernet. Fernet is AES-128-CBC with an HMAC and no support
for additional authenticated data. AAD is the whole point here: the ciphertext is
bound to the row it belongs to, so a blob lifted from tenant A's row and pasted
into tenant B's row fails to decrypt instead of quietly working. Without that, a
database write is enough to steal a working credential, and every RLS policy in
the schema is bypassed by a copy-paste.

A VERSIONED KEY, not a key. Rotation is not a someday feature: the moment a key is
suspected the question is "can we roll it without a downtime migration", and the
answer is only yes if the version travelled with the ciphertext from day one.
Version is a plaintext prefix, which is safe because knowing which key was used
reveals nothing, and it lets old rows decrypt while new rows use the new key.

A RANDOM 96-BIT NONCE PER ENCRYPTION. GCM catastrophically loses confidentiality
and integrity if a nonce repeats under the same key, so it is never derived from
anything reusable like a row id.

THE SECRET WRAPPER IS NOT DECORATION. The realistic leak is not a broken cipher,
it is a plaintext token in a log line, a traceback, or an error report. So the
plaintext lives inside a type whose repr is redacted and which refuses to be
formatted, and getting the real value takes an explicit call that reads like what
it is.
"""
from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: Bytes of a raw AES-256 key.
KEY_BYTES = 32

#: 96 bits is the GCM-recommended nonce size, and the size the construction is
#: analysed for. Longer nonces get hashed internally and buy nothing.
NONCE_BYTES = 12

#: Envelope layout: one version byte, then the nonce, then ciphertext+tag.
#: A fixed prefix rather than JSON, so the stored blob cannot be confused for
#: something readable and cannot grow a field later without a version bump.
VERSION_BYTES = 1

#: Where keys come from: "1:<base64>,2:<base64>". Highest version encrypts, all
#: versions can decrypt.
KEY_ENV = "CREDENTIAL_KEYS"


class CryptoError(Exception):
    """Base for anything wrong with the key material or an envelope."""


class KeyUnavailable(CryptoError):
    """The version an envelope names is not configured.

    Its own type because the operational response is completely different from a
    tampering failure: a missing key means a deployment is misconfigured and the
    data is fine, whereas a bad tag means the data cannot be trusted.
    """


class DecryptionFailed(CryptoError):
    """The envelope did not authenticate: wrong key, wrong row, or altered bytes.

    Deliberately does NOT say which. An attacker probing with modified blobs
    learns nothing from the error, and an operator reads the same message either
    way, so there is no reason to distinguish them here.
    """


@dataclass(frozen=True)
class Secret:
    """A plaintext credential that resists being printed by accident.

    The threat is mundane: `logger.info(f"connecting with {creds}")`, or an
    exception whose repr includes locals. Both are how tokens actually reach log
    aggregators. So repr and str are redacted and format is refused outright,
    which turns a silent leak into a loud failure at the call site.
    """

    _value: str

    def reveal(self) -> str:
        """Return the plaintext. Named to be visible in a code review."""
        return self._value

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "Secret(***)"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "***"

    def __format__(self, spec: str) -> str:
        # f"{secret}" would otherwise call __format__ and print the value on some
        # paths, so refuse rather than redact: a redacted log line hides a real
        # mistake, an exception forces it to be fixed.
        raise TypeError("refusing to format a Secret, call .reveal() explicitly")


def _parse_keys(raw: str) -> dict[int, bytes]:
    keys: dict[int, bytes] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise CryptoError("key entry %r is not version:base64" % chunk[:12])
        version_text, b64 = chunk.split(":", 1)
        try:
            version = int(version_text)
        except ValueError as exc:
            raise CryptoError("key version %r is not an integer" % version_text) from exc
        if not 1 <= version <= 255:
            # One byte in the envelope, so the range is a hard limit rather than
            # a style choice.
            raise CryptoError("key version %d does not fit in one byte" % version)
        key = base64.urlsafe_b64decode(b64.strip() + "=" * (-len(b64.strip()) % 4))
        if len(key) != KEY_BYTES:
            raise CryptoError(
                "key version %d is %d bytes, expected %d" % (version, len(key), KEY_BYTES)
            )
        keys[version] = key
    if not keys:
        raise CryptoError("no usable keys")
    return keys


def generate_key() -> str:
    """A fresh base64 key, for putting in the environment."""
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode().rstrip("=")


class CredentialCipher:
    """Encrypts and decrypts credential blobs, bound to the row they live in."""

    def __init__(self, keys: dict[int, bytes] | None = None):
        if keys is None:
            raw = os.environ.get(KEY_ENV)
            if not raw:
                raise CryptoError(
                    "%s is not set. Refusing to start rather than falling back to "
                    "plaintext, because a silent fallback is how unencrypted rows "
                    "get written for months." % KEY_ENV
                )
            keys = _parse_keys(raw)
        if not keys:
            raise CryptoError("no keys provided")
        # Validate HERE, not only in the env parser. A test passing {1: b"short"}
        # straight to the constructor found this: the parser checked the length
        # and the constructor trusted its caller, so any code path that built the
        # dict itself got no validation at all. AESGCM would have raised later,
        # somewhere far from the cause.
        for version, key in keys.items():
            if not 1 <= version <= 255:
                raise CryptoError("key version %r does not fit in one byte" % (version,))
            if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
                raise CryptoError(
                    "key version %s must be %d raw bytes" % (version, KEY_BYTES)
                )
        self._keys = dict(keys)
        #: Newest key encrypts. Everything still decrypts, which is what makes a
        #: rotation a config change rather than a migration.
        self._current = max(self._keys)

    @property
    def current_version(self) -> int:
        return self._current

    @staticmethod
    def aad(tenant_id: str, connection_id: str, platform: str) -> bytes:
        """Bind a ciphertext to exactly one connection row.

        Order and separator are fixed and the separator cannot appear in a UUID,
        so two different triples can never produce the same AAD by concatenation.
        Moving a blob to another row, or relabelling the row's platform, makes it
        undecryptable.
        """
        return ("v1|%s|%s|%s" % (tenant_id, connection_id, platform)).encode()

    def encrypt(self, plaintext: str, aad: bytes) -> bytes:
        if not isinstance(plaintext, str):
            raise CryptoError("plaintext must be str")
        nonce = secrets.token_bytes(NONCE_BYTES)
        blob = AESGCM(self._keys[self._current]).encrypt(nonce, plaintext.encode(), aad)
        return bytes([self._current]) + nonce + blob

    def decrypt(self, envelope: bytes, aad: bytes) -> Secret:
        if len(envelope) < VERSION_BYTES + NONCE_BYTES + 16:
            # 16 is the GCM tag, so anything shorter cannot be a valid envelope
            # and slicing it would produce a confusing error further down.
            raise DecryptionFailed("envelope too short to be valid")
        version = envelope[0]
        nonce = envelope[VERSION_BYTES:VERSION_BYTES + NONCE_BYTES]
        body = envelope[VERSION_BYTES + NONCE_BYTES:]
        key = self._keys.get(version)
        if key is None:
            raise KeyUnavailable(
                "envelope was encrypted with key version %d, which is not "
                "configured" % version
            )
        try:
            return Secret(AESGCM(key).decrypt(nonce, body, aad).decode())
        except InvalidTag as exc:
            raise DecryptionFailed(
                "credential failed authentication: wrong key, wrong row, or "
                "altered bytes"
            ) from exc
