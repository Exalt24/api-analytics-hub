"""The ways a signing command is refused. Each is its own type so a caller (and a test) can tell one refusal from another."""
from __future__ import annotations


class EsignError(Exception):
    """Base for every refusal in this package."""


class RequestNotFound(EsignError):
    """No signing request with this id is visible to the caller's tenant.

    ONE REFUSAL FOR TWO CASES. An id that does not exist and an id that belongs to another tenant raise this same error with the same text, because
    row level security hides the other tenant's row and the code never looks further. A different answer for the second case would let a caller
    probe which ids are real."""

    def __init__(self) -> None:
        super().__init__("signing request not found")


class InvalidRequest(EsignError):
    """A new request that cannot be stored as asked (an empty document, a bad email, an expiry in the past)."""


class AlreadySigned(EsignError):
    """The request already has a signature and this attempt is not a retry of it (different name, hash or consent)."""


class RequestExpired(EsignError):
    pass


class RequestDeclined(EsignError):
    pass


class ConsentRequired(EsignError):
    """The consent box was not ticked."""


class ConsentVersionMismatch(EsignError):
    """The signer consented to a different version of the consent text than the one this request carries."""


class NameRequired(EsignError):
    pass


class NameMismatch(EsignError):
    """The typed name is not the named signer's name under the rule in app/esign/consent.py (names_match)."""


class DocumentChanged(EsignError):
    """The stored bytes no longer hash to the recorded hash, or the signer saw a different hash from the one stored."""


class NotSigned(EsignError):
    """A certificate was asked for a request that has no signature."""


class CertificateRefused(EsignError):
    """The record does not verify, so no certificate is issued for it."""

    def __init__(self, problems):
        super().__init__("the record does not verify: %d problem(s)" % len(problems))
        self.problems = problems
