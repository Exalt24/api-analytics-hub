"""A small SMS sender for the Twilio Programmable Messaging REST API (standard library only).

Written from Twilio's public documentation and tested only against a stand-in HTTP server
(tests/sms_stand_in.py). It has never been run against the live API; see docs/sms.md.

Three rules shape it, each learned from a real failure:

* A 201 means "queued", not "delivered". `send` returns the message sid and the first status and
  nothing else; `check` polls until the message reaches a final status and reports the error code
  of an undelivered or failed one. A client job once logged 201 for messages carriers blocked later.
* A send that may already have reached the vendor is never retried inside this module. Resending
  could text the customer twice. The caller gets `SendOutcomeUnknown` carrying the idempotency key
  and decides; sending again with the SAME key is the safe way, because a vendor that honours the
  key answers a repeat with 409 and the first message's sid.
* The auth token and account sid never reach a log line, an exception message or a repr, and a
  phone number is masked to its last four digits wherever it is written.
"""
from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Callable, Mapping

log = logging.getLogger("app.sms")

DEFAULT_BASE_URL = "https://api.twilio.com"
API_VERSION = "2010-04-01"
#: Header carrying the client reference. Twilio's Messages page does not document one, so this is
#: our contract with the stand-in; against the real API the header is most likely ignored.
IDEMPOTENCY_HEADER = "Idempotency-Key"

#: Twilio documents Body as "up to 1,600 characters".
MAX_BODY_CHARS = 1600
#: A plus sign and 8 to 15 digits, the first not zero (E.164 country codes never start with 0).
E164 = re.compile(r"\+[1-9][0-9]{7,14}")
#: A messaging service sid can stand in for a From number.
MESSAGING_SERVICE_SID = re.compile(r"MG[0-9a-fA-F]{32}")
#: Statuses after which Twilio changes nothing more for an SMS.
FINAL_STATUSES = frozenset({"delivered", "undelivered", "failed", "canceled"})
#: Failures a caller usually wants to look at by hand.
BAD_FINAL_STATUSES = frozenset({"undelivered", "failed"})

_NUMBER_IN_TEXT = re.compile(r"(?<!\w)\+?[0-9]{8,15}(?!\w)")


# ---------------------------------------------------------------- errors

class SmsError(Exception):
    """Base class. Messages are built here from known-safe parts, never from a raw urllib error."""


class ConfigError(SmsError):
    """A required environment variable is missing. Names the variable, never a value."""


class InvalidRequest(SmsError, ValueError):
    """Refused before any request was made."""


class ApiError(SmsError):
    """The vendor answered with an error that retrying will not fix (or retries ran out)."""

    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class RateLimited(ApiError):
    """429 on every attempt."""


class NetworkError(SmsError):
    """The vendor could not be reached at all (connection refused, name not found), after retries."""


class SendOutcomeUnknown(SmsError):
    """The request may have reached the vendor but the answer was lost or unreadable.

    The message may or may not exist. Do not call send again with a new key. Send again with
    `idempotency_key` set to this exception's key, or look the message up first.
    """

    def __init__(self, message: str, idempotency_key: str):
        super().__init__(message)
        self.idempotency_key = idempotency_key


# ---------------------------------------------------------------- masking and redaction

def mask_number(number: str) -> str:
    """Keep only the last four characters of a phone number: '+639171234567' becomes '***4567'."""
    text = str(number)
    return "***" + text[-4:] if len(text) > 4 else "***"


def mask_numbers_in(text: str) -> str:
    """Mask every phone-number-looking run in text that came from the vendor."""
    return _NUMBER_IN_TEXT.sub(lambda m: mask_number(m.group(0)), text)


@dataclass(frozen=True)
class Credentials:
    """Account sid and auth token. Both stay out of repr and str."""

    account_sid: str = field(repr=False)
    auth_token: str = field(repr=False)

    def __repr__(self) -> str:
        return "Credentials(account_sid=<set>, auth_token=<set>)"

    __str__ = __repr__

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Credentials":
        env = os.environ if environ is None else environ
        missing = [n for n in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN") if not env.get(n)]
        if missing:
            raise ConfigError("missing environment variable(s): " + ", ".join(missing))
        return cls(env["TWILIO_ACCOUNT_SID"], env["TWILIO_AUTH_TOKEN"])

    def scrub(self, text: str) -> str:
        """Remove both secrets from any text before it is logged or put in an exception."""
        for secret in (self.auth_token, self.account_sid):
            if secret:
                text = text.replace(secret, "<redacted>")
        return text


# ---------------------------------------------------------------- validation

def validate_to(to: object) -> str:
    if not isinstance(to, str) or not E164.fullmatch(to):
        raise InvalidRequest("destination must be E.164: a plus sign and 8 to 15 digits")
    return to


def validate_body(body: object) -> str:
    if not isinstance(body, str) or not body.strip():
        raise InvalidRequest("message body is empty")
    if len(body) > MAX_BODY_CHARS:
        raise InvalidRequest(f"message body is {len(body)} characters, the limit is {MAX_BODY_CHARS}")
    return body


def validate_key(key: object) -> str:
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        raise InvalidRequest("idempotency key must be 1 to 128 characters of letters, digits and . _ : -")
    return key


def _sender_form(sender: str) -> dict[str, str]:
    if MESSAGING_SERVICE_SID.fullmatch(sender):
        return {"MessagingServiceSid": sender}
    if E164.fullmatch(sender):
        return {"From": sender}
    raise InvalidRequest("sender must be an E.164 number or a messaging service sid")


# ---------------------------------------------------------------- results

@dataclass(frozen=True)
class SendResult:
    """What the vendor said when it accepted the request. Accepted is not delivered."""

    sid: str
    status: str
    idempotency_key: str
    #: True when the vendor recognised the key and returned the first message instead of a new one.
    duplicate: bool = False


@dataclass(frozen=True)
class CheckResult:
    sid: str
    status: str
    error_code: int | None
    #: False when polling ran out before the message reached a final status.
    settled: bool
    polls: int

    @property
    def delivered(self) -> bool:
        return self.settled and self.status == "delivered"

    @property
    def failed(self) -> bool:
        return self.settled and self.status in BAD_FINAL_STATUSES


class _NotSent(Exception):
    """Connection refused or name not found: nothing was written, so a retry cannot double-send."""


class _Unknown(Exception):
    """The request was or may have been written but no usable answer came back."""


# ---------------------------------------------------------------- the sender

def _backoff(base: float, cap: float, attempt: int) -> float:
    """Exponential delay, never above cap."""
    return min(cap, base * (2 ** attempt))


class SmsSender:
    def __init__(
        self,
        credentials: Credentials,
        sender: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        max_retry_after: float = 60.0,
        poll_attempts: int = 8,
        poll_base: float = 1.0,
        poll_cap: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        opener: urllib.request.OpenerDirector | None = None,
    ):
        self._creds = credentials
        self._sender_form = _sender_form(sender)
        self._base = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_retry_after = max_retry_after
        self.poll_attempts = poll_attempts
        self.poll_base = poll_base
        self.poll_cap = poll_cap
        self._sleep = sleep
        self._opener = opener or urllib.request.build_opener()
        raw = f"{credentials.account_sid}:{credentials.auth_token}".encode("utf-8")
        self._auth = "Basic " + base64.b64encode(raw).decode("ascii")

    def __repr__(self) -> str:
        return f"SmsSender(base_url={self._base!r})"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **options) -> "SmsSender":
        env = os.environ if environ is None else environ
        sender = env.get("TWILIO_MESSAGING_SERVICE_SID") or env.get("TWILIO_FROM_NUMBER")
        if not sender:
            raise ConfigError("missing environment variable: TWILIO_FROM_NUMBER or TWILIO_MESSAGING_SERVICE_SID")
        return cls(Credentials.from_env(env), sender, **options)

    # -- public

    def send(self, to: str, body: str, *, idempotency_key: str | None = None) -> SendResult:
        """Ask the vendor to send one SMS. Returns the sid and first status; that is acceptance only."""
        validate_to(to)
        validate_body(body)
        key = validate_key(idempotency_key) if idempotency_key is not None else uuid.uuid4().hex
        form = {"To": to, "Body": body, **self._sender_form}
        path = f"/{API_VERSION}/Accounts/{self._creds.account_sid}/Messages.json"
        masked = mask_number(to)
        try:
            status, _, raw = self._call("POST", path, form, {IDEMPOTENCY_HEADER: key}, retry_unknown=False, label=masked)
        except _Unknown as exc:
            log.error("sms outcome unknown key=%s to=%s (%s)", key, masked, exc)
            raise SendOutcomeUnknown(
                f"the request may have reached the vendor but no usable answer came back ({exc}); "
                f"the message may exist. Resend only with idempotency_key={key!r}",
                key,
            ) from None
        data = self._json(raw)
        if status == 201:
            if not isinstance(data, dict) or not isinstance(data.get("sid"), str) or not isinstance(data.get("status"), str):
                log.error("sms 201 with an unreadable body key=%s to=%s", key, masked)
                raise SendOutcomeUnknown(
                    "the vendor answered 201 but the body was not a message resource; "
                    f"the message probably exists. Resend only with idempotency_key={key!r}",
                    key,
                ) from None
            log.info("sms accepted sid=%s status=%s to=%s (accepted, not delivered)", data["sid"], data["status"], masked)
            return SendResult(data["sid"], data["status"], key)
        if status == 409 and isinstance(data, dict) and isinstance(data.get("existing_sid"), str):
            log.info("sms duplicate key=%s sid=%s to=%s: the vendor already had this send", key, data["existing_sid"], masked)
            return SendResult(data["existing_sid"], str(data.get("existing_status", "unknown")), key, duplicate=True)
        if 200 <= status < 300:
            raise SendOutcomeUnknown(f"the vendor answered HTTP {status}, not 201; resend only with idempotency_key={key!r}", key)
        raise self._api_error(status, data)

    def check(self, sid: str) -> CheckResult:
        """Poll the message until it reaches a final status, with a bounded backoff.

        Gives up after `poll_attempts` looks and says so (`settled` is False) rather than
        guessing. Reading a message is safe to repeat, so a lost answer is retried here.
        """
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z]{2}[0-9A-Za-z]{32}", sid):
            raise InvalidRequest("message sid looks like SM followed by 32 characters")
        path = f"/{API_VERSION}/Accounts/{self._creds.account_sid}/Messages/{sid}.json"
        status = "unknown"
        error_code: int | None = None
        for poll in range(1, self.poll_attempts + 1):
            code, _, raw = self._call("GET", path, None, {}, retry_unknown=True, label=sid)
            data = self._json(raw)
            if code != 200 or not isinstance(data, dict):
                raise self._api_error(code, data)
            status = str(data.get("status", "unknown"))
            ec = data.get("error_code")
            error_code = ec if isinstance(ec, int) else None
            if status in FINAL_STATUSES:
                log.log(logging.WARNING if status in BAD_FINAL_STATUSES else logging.INFO,
                        "sms final sid=%s status=%s error_code=%s", sid, status, error_code)
                return CheckResult(sid, status, error_code, True, poll)
            if poll < self.poll_attempts:
                self._sleep(_backoff(self.poll_base, self.poll_cap, poll - 1))
        log.warning("sms not final after %d looks sid=%s last status=%s", self.poll_attempts, sid, status)
        return CheckResult(sid, status, error_code, True, self.poll_attempts)

    # -- internals

    def _call(self, method, path, form, extra_headers, *, retry_unknown: bool, label: str):
        data = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
        headers = {"Authorization": self._auth, "Accept": "application/json", **extra_headers}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        attempt = 0
        while True:
            try:
                status, resp_headers, raw = self._once(method, self._base + path, data, headers)
            except (_NotSent, _Unknown) as exc:
                # Only a refused connection is certain to be unsent. Anything else on a POST is
                # handed back to the caller, because a retry could text the customer twice.
                if isinstance(exc, _Unknown) and not retry_unknown:
                    raise
                if attempt >= self.max_retries:
                    raise NetworkError(f"vendor unreachable after {attempt + 1} tries ({exc})") from None
                delay = _backoff(self.backoff_base, self.backoff_cap, attempt)
                log.warning("sms retry %d in %.1fs: %s [%s]", attempt + 1, delay, exc, label)
                self._sleep(delay)
                attempt += 1
                continue
            if status == 429 or status >= 500:
                if attempt >= self.max_retries:
                    err = self._api_error(status, self._json(raw))
                    if status == 429:
                        raise RateLimited(str(err), status, err.code) from None
                    raise err
                delay = self._retry_after(resp_headers)
                if delay is None:
                    delay = _backoff(self.backoff_base, self.backoff_cap, attempt)
                log.warning("sms retry %d in %.1fs: HTTP %d [%s]", attempt + 1, delay, status, label)
                self._sleep(delay)
                attempt += 1
                continue
            return status, resp_headers, raw

    def _once(self, method, url, data, headers):
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:  # an HTTP answer with an error status: the vendor did reply
            try:
                return exc.code, exc.headers, exc.read()
            except (OSError, http.client.HTTPException):
                raise _Unknown("error body lost") from None
            finally:
                exc.close()
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
                raise _NotSent(type(reason).__name__) from None
            raise _Unknown(type(reason).__name__) from None
        except (TimeoutError, socket.timeout, OSError, http.client.HTTPException) as exc:
            raise _Unknown(type(exc).__name__) from None

    def _retry_after(self, headers) -> float | None:
        """Seconds the vendor asked us to wait, capped. None when absent or unreadable."""
        value = headers.get("Retry-After")
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time.time()
            except (TypeError, ValueError):
                return None
        return max(0.0, min(self.max_retry_after, seconds))

    @staticmethod
    def _json(raw: bytes):
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _api_error(self, status: int, data) -> ApiError:
        code = data.get("code") if isinstance(data, dict) else None
        code = code if isinstance(code, int) else None
        text = data.get("message") if isinstance(data, dict) else None
        text = text if isinstance(text, str) else "no readable error body"
        text = mask_numbers_in(self._creds.scrub(text))[:300]
        return ApiError(f"vendor refused the request: HTTP {status}, code {code}: {text}", status, code)


# ---------------------------------------------------------------- module level shortcuts

def send(to: str, body: str, *, idempotency_key: str | None = None, client: SmsSender | None = None) -> SendResult:
    """send(to, body) using the environment for credentials and sender."""
    return (client or SmsSender.from_env()).send(to, body, idempotency_key=idempotency_key)


def check(sid: str, *, client: SmsSender | None = None) -> CheckResult:
    return (client or SmsSender.from_env()).check(sid)
