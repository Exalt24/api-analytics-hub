"""The SMS sender, tested against a stand-in HTTP server on localhost (no network, no database, no Twilio account).

Every behaviour has a test and, where the test could pass for the wrong reason, a control that
shows it can fail. tests/mutation_check_sms.py then breaks each guard in app/sms.py and requires
the matching test here to go red. The stand-in was written from Twilio's documentation, so a green
run says the code matches that reading, not that the live API agrees.
"""
from __future__ import annotations

import base64
import http.client
import logging
import random
import re
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import sms  # noqa: E402
from sms_stand_in import StandIn  # noqa: E402

# Built from pieces so no whole token sits in this file for the repo's secret scan to flag.
ACCOUNT_SID = "AC" + "7f3a" * 8
AUTH_TOKEN = "tk" + "-Q9" + "x" * 3 + "-m2" + "Lp4" * 4
FROM_NUMBER = "+1" + "555" + "010" + "0199"
CUSTOMER = "+63" + "917" + "123" + "4567"
SERVICE_SID = "MG" + "ab12" * 8
LOGGER = "app.sms"


class Sleeps:
    """Stands in for time.sleep: records the delays instead of waiting."""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture(scope="module")
def server():
    stand_in = StandIn(ACCOUNT_SID, AUTH_TOKEN).start()
    yield stand_in
    stand_in.stop()


@pytest.fixture
def stand_in(server):
    server.reset()
    server.script = "delivered"
    server.retry_after = "7"
    server.slow_seconds = 1.0
    server.enforce_idempotency = True
    server.leak_secret_in_errors = False
    server.reject_numbers = set()
    return server


@pytest.fixture
def sleeps():
    return Sleeps()


def make_sender(stand_in, sleeps, **options):
    """A sender wired to the stand-in, bypassing any proxy in the environment, with sleeping replaced."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return sms.SmsSender(
        sms.Credentials(ACCOUNT_SID, AUTH_TOKEN), FROM_NUMBER,
        base_url=stand_in.base_url, sleep=sleeps, opener=opener, **options,
    )


@pytest.fixture
def sender(stand_in, sleeps):
    return make_sender(stand_in, sleeps)


# ------------------------------------------------------------------ (1) the request and what send returns

def test_send_posts_the_form_with_basic_auth_and_returns_sid_and_first_status(stand_in, sender):
    result = sender.send(CUSTOMER, "Your code is 123456")
    (post,) = stand_in.posts
    assert post["path"] == f"/2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json"
    assert post["headers"]["Authorization"] == "Basic " + base64.b64encode(f"{ACCOUNT_SID}:{AUTH_TOKEN}".encode()).decode()
    assert post["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert post["form"] == {"To": CUSTOMER, "From": FROM_NUMBER, "Body": "Your code is 123456"}
    assert result.sid == stand_in.messages[0]["sid"] and result.sid.startswith("SM")
    assert result.status == "queued"
    assert result.duplicate is False


def test_a_messaging_service_sid_is_sent_instead_of_from(stand_in, sleeps):
    s = sms.SmsSender(sms.Credentials(ACCOUNT_SID, AUTH_TOKEN), SERVICE_SID, base_url=stand_in.base_url, sleep=sleeps,
                      opener=urllib.request.build_opener(urllib.request.ProxyHandler({})))
    s.send(CUSTOMER, "hello")
    assert stand_in.posts[0]["form"] == {"To": CUSTOMER, "MessagingServiceSid": SERVICE_SID, "Body": "hello"}


def test_control_the_stand_in_really_checks_the_credentials(stand_in, sleeps):
    wrong = sms.SmsSender(sms.Credentials(ACCOUNT_SID, AUTH_TOKEN + "x"), FROM_NUMBER, base_url=stand_in.base_url,
                          sleep=sleeps, opener=urllib.request.build_opener(urllib.request.ProxyHandler({})))
    with pytest.raises(sms.ApiError) as err:
        wrong.send(CUSTOMER, "hello")
    assert err.value.status == 401 and err.value.code == 20003
    assert stand_in.messages == []
    assert sleeps.calls == [], "a 401 is not worth retrying"


def test_a_401_from_the_vendor_is_an_error_with_its_code_and_no_retry(stand_in, sender, sleeps):
    stand_in.fail_posts = ["401"]
    with pytest.raises(sms.ApiError) as err:
        sender.send(CUSTOMER, "hello")
    assert (err.value.status, err.value.code) == (401, 20003)
    assert len(stand_in.posts) == 1 and sleeps.calls == []


def test_a_vendor_refusal_carries_status_and_code(stand_in, sender):
    stand_in.reject_numbers = {CUSTOMER}
    with pytest.raises(sms.ApiError) as err:
        sender.send(CUSTOMER, "hello")
    assert (err.value.status, err.value.code) == (400, 21211)
    assert len(stand_in.posts) == 1


# ------------------------------------------------------------------ (2) accepted is not delivered

def test_a_201_is_acceptance_and_check_reports_the_carrier_block_with_its_error_code(stand_in, sender):
    stand_in.script = "undelivered"
    sent = sender.send(CUSTOMER, "hello")
    assert sent.status == "queued" and not hasattr(sent, "delivered"), "send must not claim delivery"
    done = sender.check(sent.sid)
    assert done.status == "undelivered"
    assert done.error_code == 30007
    assert done.settled and done.failed and not done.delivered


def test_control_a_delivered_message_is_reported_delivered_with_no_error_code(stand_in, sender):
    sent = sender.send(CUSTOMER, "hello")
    done = sender.check(sent.sid)
    assert (done.status, done.error_code) == ("delivered", None)
    assert done.delivered and not done.failed


def test_a_failed_message_is_reported_failed_with_its_code(stand_in, sender):
    stand_in.script = "failed"
    done = sender.check(sender.send(CUSTOMER, "hello").sid)
    assert (done.status, done.error_code, done.failed) == ("failed", 30008, True)


# ------------------------------------------------------------------ (2b) the final-status loop

def test_check_polls_through_the_middle_statuses_and_stops_at_the_first_final_one(stand_in, sleeps):
    s = make_sender(stand_in, sleeps, poll_base=1.0, poll_cap=30.0)
    sent = s.send(CUSTOMER, "hello")
    done = s.check(sent.sid)
    assert done.status == "delivered" and done.polls == 4
    assert sleeps.calls == [1.0, 2.0, 4.0], "doubling delay between looks"
    gets = [r for r in stand_in.requests if r["method"] == "GET"]
    assert len(gets) == 4, "no look after the final status"


def test_check_backoff_between_looks_is_capped(stand_in, sleeps):
    s = make_sender(stand_in, sleeps, poll_base=1.0, poll_cap=2.0)
    s.check(s.send(CUSTOMER, "hello").sid)
    assert sleeps.calls == [1.0, 2.0, 2.0]


def test_check_gives_up_after_a_bounded_number_of_looks_and_says_it_is_not_settled(stand_in, sleeps):
    stand_in.script = "stuck"
    s = make_sender(stand_in, sleeps, poll_attempts=5, poll_base=1.0, poll_cap=3.0)
    done = s.check(s.send(CUSTOMER, "hello").sid)
    assert done.settled is False and done.status == "queued" and not done.delivered and not done.failed
    assert done.polls == 5
    assert len([r for r in stand_in.requests if r["method"] == "GET"]) == 5
    assert sleeps.calls == [1.0, 2.0, 3.0, 3.0], "one fewer sleep than looks, each at most the cap"


def test_check_retries_a_lost_or_unavailable_read_because_reading_is_safe_to_repeat(stand_in, sender):
    sent = sender.send(CUSTOMER, "hello")
    stand_in.fail_gets = ["lost", "503"]
    done = sender.check(sent.sid)
    assert done.delivered
    assert len(stand_in.messages) == 1


def test_check_of_an_unknown_sid_is_a_404_error(stand_in, sender):
    with pytest.raises(sms.ApiError) as err:
        sender.check("SM" + "0" * 32)
    assert err.value.status == 404


# ------------------------------------------------------------------ (3) 429, 5xx and retries

def test_a_429_waits_for_the_retry_after_the_vendor_sent_and_then_succeeds(stand_in, sender, sleeps):
    stand_in.fail_posts = ["429"]
    stand_in.retry_after = "7"
    result = sender.send(CUSTOMER, "hello")
    assert sleeps.calls == [7.0], "the vendor's number, not our own backoff (0.5)"
    assert len(stand_in.posts) == 2 and len(stand_in.messages) == 1
    assert result.sid == stand_in.messages[0]["sid"]


def test_a_huge_retry_after_is_capped(stand_in, sender, sleeps):
    stand_in.fail_posts = ["429"]
    stand_in.retry_after = "9999"
    sender.send(CUSTOMER, "hello")
    assert sleeps.calls == [60.0]


def test_a_429_without_retry_after_or_with_garbage_falls_back_to_the_backoff(stand_in, sender, sleeps):
    stand_in.fail_posts = ["429"]
    stand_in.retry_after = None
    sender.send(CUSTOMER, "hello")
    stand_in.fail_posts = ["429"]
    stand_in.retry_after = "soon"
    sender.send(CUSTOMER, "hello")
    assert sleeps.calls == [0.5, 0.5]


def test_a_429_that_never_clears_stops_at_the_retry_cap_and_creates_nothing(stand_in, sender, sleeps):
    stand_in.fail_posts = ["429"] * 20
    with pytest.raises(sms.RateLimited) as err:
        sender.send(CUSTOMER, "hello")
    assert err.value.status == 429
    assert len(stand_in.posts) == 4, "the first try plus max_retries (3), no more"
    assert stand_in.messages == [] and len(sleeps.calls) == 3


def test_a_503_then_success_is_retried_with_a_short_backoff(stand_in, sender, sleeps):
    stand_in.fail_posts = ["503"]
    result = sender.send(CUSTOMER, "hello")
    assert sleeps.calls == [0.5]
    assert len(stand_in.posts) == 2 and len(stand_in.messages) == 1 and result.status == "queued"


def test_the_5xx_backoff_doubles_and_is_capped(stand_in, sleeps):
    s = make_sender(stand_in, sleeps, max_retries=4, backoff_base=1.0, backoff_cap=2.0)
    stand_in.fail_posts = ["503"] * 3
    s.send(CUSTOMER, "hello")
    assert sleeps.calls == [1.0, 2.0, 2.0]


def test_a_503_that_never_clears_stops_at_the_retry_cap(stand_in, sender, sleeps):
    stand_in.fail_posts = ["503"] * 20
    with pytest.raises(sms.ApiError) as err:
        sender.send(CUSTOMER, "hello")
    assert err.value.status == 503 and not isinstance(err.value, sms.RateLimited)
    assert len(stand_in.posts) == 4 and len(sleeps.calls) == 3
    assert stand_in.messages == []


class RefusingOpener:
    """An opener whose connection is always refused, as urllib reports it. A real closed port takes about
    two seconds per try on Windows, which would make the retry test and its mutants slow."""

    def __init__(self):
        self.tries = 0

    def open(self, request, timeout=None):
        import urllib.error
        self.tries += 1
        raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))


def test_a_refused_connection_was_never_sent_so_it_is_retried_then_reported_as_unreachable(sleeps):
    opener = RefusingOpener()
    s = sms.SmsSender(sms.Credentials(ACCOUNT_SID, AUTH_TOKEN), FROM_NUMBER, base_url="http://127.0.0.1:9",
                      sleep=sleeps, opener=opener)
    with pytest.raises(sms.NetworkError):
        s.send(CUSTOMER, "hello")
    assert opener.tries == 4 and sleeps.calls == [0.5, 1.0, 2.0]


# ------------------------------------------------------------------ (3b) never retry blindly

class FailingOpener:
    """An opener that fails the way urllib reports one kind of network failure."""

    def __init__(self, error):
        self.error = error
        self.tries = 0

    def open(self, request, timeout=None):
        self.tries += 1
        raise self.error


@pytest.mark.parametrize("error", [
    urllib.error.URLError(ConnectionResetError(10054, "reset by peer")),
    urllib.error.URLError(TimeoutError("timed out")),
    TimeoutError("read timed out"),
    http.client.IncompleteRead(b"par"),
], ids=["reset", "url-timeout", "read-timeout", "short-read"])
def test_any_failure_that_may_come_after_the_request_was_written_is_not_retried(sleeps, error):
    opener = FailingOpener(error)
    s = sms.SmsSender(sms.Credentials(ACCOUNT_SID, AUTH_TOKEN), FROM_NUMBER, base_url="http://127.0.0.1:9", sleep=sleeps, opener=opener)
    with pytest.raises(sms.SendOutcomeUnknown):
        s.send(CUSTOMER, "hello")
    assert opener.tries == 1 and sleeps.calls == []


def test_control_the_same_failures_are_retried_when_reading_a_message_because_that_is_safe(sleeps):
    opener = FailingOpener(urllib.error.URLError(ConnectionResetError(10054, "reset by peer")))
    s = sms.SmsSender(sms.Credentials(ACCOUNT_SID, AUTH_TOKEN), FROM_NUMBER, base_url="http://127.0.0.1:9", sleep=sleeps, opener=opener)
    with pytest.raises(sms.NetworkError):
        s.check("SM" + "0" * 32)
    assert opener.tries == 4



def test_a_lost_response_is_not_retried_inside_the_sender(stand_in, sender, sleeps):
    """The message exists at the vendor but we never saw the answer. A blind retry would text the customer twice."""
    stand_in.enforce_idempotency = False   # so a blind retry could not be hidden by the stand-in
    stand_in.fail_posts = ["lost"]
    with pytest.raises(sms.SendOutcomeUnknown) as err:
        sender.send(CUSTOMER, "hello")
    assert len(stand_in.posts) == 1, "exactly one request was made"
    assert len(stand_in.messages) == 1, "and the message does exist"
    assert sleeps.calls == []
    assert re.fullmatch(r"[0-9a-f]{32}", err.value.idempotency_key)


def test_a_timeout_after_the_request_was_written_is_not_retried(stand_in, sleeps):
    stand_in.enforce_idempotency = False
    stand_in.slow_seconds = 0.8
    stand_in.fail_posts = ["slow"]
    s = make_sender(stand_in, sleeps, timeout=0.2)
    with pytest.raises(sms.SendOutcomeUnknown):
        s.send(CUSTOMER, "hello")
    assert len(stand_in.posts) == 1 and sleeps.calls == []


def test_a_201_with_a_body_we_cannot_read_is_an_unknown_outcome_not_a_retry(stand_in, sender, sleeps):
    stand_in.enforce_idempotency = False
    stand_in.fail_posts = ["malformed"]
    with pytest.raises(sms.SendOutcomeUnknown):
        sender.send(CUSTOMER, "hello")
    assert len(stand_in.posts) == 1 and len(stand_in.messages) == 1 and sleeps.calls == []


def test_control_the_message_count_does_see_a_double_send_when_a_caller_resends_with_a_new_key(stand_in, sender):
    """Shows the counter in the tests above can fail: a naive caller that resends under a fresh key does produce two."""
    stand_in.fail_posts = ["lost"]
    with pytest.raises(sms.SendOutcomeUnknown):
        sender.send(CUSTOMER, "hello")
    sender.send(CUSTOMER, "hello")   # no key given, so a new one is made
    assert len(stand_in.messages) == 2


def test_a_resend_with_the_same_key_after_a_lost_response_does_not_make_a_second_message(stand_in, sender):
    stand_in.fail_posts = ["lost"]
    with pytest.raises(sms.SendOutcomeUnknown) as err:
        sender.send(CUSTOMER, "hello")
    again = sender.send(CUSTOMER, "hello", idempotency_key=err.value.idempotency_key)
    assert len(stand_in.messages) == 1, "one message in total"
    assert len(stand_in.posts) == 2
    assert again.duplicate is True and again.sid == stand_in.messages[0]["sid"]
    assert stand_in.posts[0]["headers"]["Idempotency-Key"] == stand_in.posts[1]["headers"]["Idempotency-Key"] == err.value.idempotency_key


def test_control_the_same_resend_makes_two_messages_when_the_vendor_does_not_honour_keys(stand_in, sender):
    """The honest limit: if the vendor ignores the key (Twilio documents none for this endpoint), the resend is a second SMS."""
    stand_in.enforce_idempotency = False
    stand_in.fail_posts = ["lost"]
    with pytest.raises(sms.SendOutcomeUnknown) as err:
        sender.send(CUSTOMER, "hello")
    sender.send(CUSTOMER, "hello", idempotency_key=err.value.idempotency_key)
    assert len(stand_in.messages) == 2


def test_every_send_carries_a_distinct_key_unless_the_caller_gives_one(stand_in, sender):
    sender.send(CUSTOMER, "a")
    sender.send(CUSTOMER, "b")
    keys = [p["headers"]["Idempotency-Key"] for p in stand_in.posts]
    assert len(set(keys)) == 2
    with pytest.raises(sms.InvalidRequest):
        sender.send(CUSTOMER, "c", idempotency_key="has space")


def test_property_with_faults_dropped_in_at_random_a_caller_that_reuses_the_key_never_double_sends(stand_in, sender, sleeps):
    """Seeded random worlds: any mix of lost answers, unreadable answers, 503s and 429s ahead of each attempt.

    Invariant: a caller that follows the rule (resend only with the key from SendOutcomeUnknown) ends with
    at most one message, and if it got an answer, that answer names that message.
    """
    rng = random.Random(20261106)
    outcomes = {"answered": 0, "gave_up": 0}
    for world in range(150):
        stand_in.reset()
        stand_in.retry_after = rng.choice(["0", "1", None])
        stand_in.fail_posts = [rng.choice([None, None, "lost", "malformed", "503", "429"]) for _ in range(rng.randint(0, 6))]
        sleeps.calls.clear()
        key = None
        result = None
        for _attempt in range(5):
            try:
                result = sender.send(CUSTOMER, "world %d" % world, idempotency_key=key)
                break
            except sms.SendOutcomeUnknown as exc:
                key = exc.idempotency_key
            except sms.ApiError:
                break
        assert len(stand_in.messages) <= 1, f"world {world} double-sent: {stand_in.fail_posts}"
        if result is not None:
            outcomes["answered"] += 1
            assert result.sid == stand_in.messages[0]["sid"]
        else:
            outcomes["gave_up"] += 1
    assert outcomes["answered"] > 50 and outcomes["gave_up"] > 0, outcomes   # the worlds cover both endings


# ------------------------------------------------------------------ (4) validation before any request

VALID_NUMBERS = ["+" + "1" * 8, "+" + "1" * 15, CUSTOMER, "+14155550123"]
INVALID_NUMBERS = [
    "+" + "1" * 7,            # 7 digits
    "+" + "1" * 16,           # 16 digits
    "+0123456789",            # country code cannot start with 0
    "639171234567",           # no plus sign
    "+63 917 123 4567",       # spaces
    "+63-917-123-4567",       # dashes
    "+63917123456x",          # a letter
    "",                       # empty
    "+",                      # only the sign
    "+63917123456\n",         # a trailing newline must not slip past a regex
    None,                     # not a string
]


@pytest.mark.parametrize("number", INVALID_NUMBERS)
def test_a_destination_that_is_not_e164_is_refused_before_any_request(stand_in, sender, number):
    with pytest.raises(sms.InvalidRequest):
        sender.send(number, "hello")
    assert stand_in.requests == []


@pytest.mark.parametrize("number", VALID_NUMBERS)
def test_control_a_well_formed_e164_destination_goes_through(stand_in, sender, number):
    assert sender.send(number, "hello").sid
    assert len(stand_in.posts) == 1


@pytest.mark.parametrize("body", ["", "   ", "\n\t", None, "x" * 1601, "é" * 1601])
def test_an_empty_or_over_long_body_is_refused_before_any_request(stand_in, sender, body):
    with pytest.raises(sms.InvalidRequest):
        sender.send(CUSTOMER, body)
    assert stand_in.requests == []


@pytest.mark.parametrize("body", ["x", "x" * 1600, "é" * 1600])
def test_control_a_body_of_one_to_1600_characters_is_accepted(stand_in, sender, body):
    sender.send(CUSTOMER, body)
    assert stand_in.posts[0]["form"]["Body"] == body


def test_a_bad_sender_is_refused_when_the_client_is_built():
    with pytest.raises(sms.InvalidRequest):
        sms.SmsSender(sms.Credentials(ACCOUNT_SID, AUTH_TOKEN), "not a number")


def test_check_refuses_a_malformed_sid_before_any_request(stand_in, sender):
    with pytest.raises(sms.InvalidRequest):
        sender.check("../../Accounts")
    assert stand_in.requests == []


# ------------------------------------------------------------------ (5) secrets stay out of everything written

def everything_written(caplog, errors) -> str:
    """Every log line, plus the str, repr and full traceback text of each exception."""
    parts = [r.getMessage() + " " + str(r.args) + " " + (r.exc_text or "") for r in caplog.records]
    for exc in errors:
        parts += [str(exc), repr(exc), "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))]
    return "\n".join(parts)


def run_every_scenario(stand_in, sender, sleeps):
    """Drive success, every fault and every refusal; return the exceptions raised."""
    errors = []

    def attempt(fn):
        try:
            fn()
        except sms.SmsError as exc:
            errors.append(exc)

    stand_in.script = "undelivered"
    sent = sender.send(CUSTOMER, "hello")
    sender.check(sent.sid)
    for faults in (["429"], ["503"], ["lost"], ["malformed"], ["401"], ["503"] * 9, ["429"] * 9):
        stand_in.fail_posts = list(faults)
        attempt(lambda: sender.send(CUSTOMER, "hello"))
    stand_in.reject_numbers = {CUSTOMER}
    attempt(lambda: sender.send(CUSTOMER, "hello"))
    stand_in.reject_numbers = set()
    attempt(lambda: sender.send("12345", "hello"))
    attempt(lambda: sender.send(CUSTOMER, ""))
    attempt(lambda: sender.check("SM" + "0" * 32))
    stand_in.fail_gets = ["lost", "503"]
    attempt(lambda: sender.check(sent.sid))
    stand_in.leak_secret_in_errors = True
    stand_in.fail_posts = ["401"]
    attempt(lambda: sender.send(CUSTOMER, "hello"))
    return errors


def test_neither_secret_appears_in_any_log_line_exception_or_repr(stand_in, sender, sleeps, caplog):
    caplog.set_level(logging.DEBUG)
    errors = run_every_scenario(stand_in, sender, sleeps)
    assert len(errors) >= 8 and caplog.records, "the scenarios must really have produced errors and log lines"
    written = everything_written(caplog, errors)
    objects = " ".join([repr(sender), str(sender), repr(sender._creds), str(sender._creds)])
    pair = base64.b64encode(f"{ACCOUNT_SID}:{AUTH_TOKEN}".encode()).decode()
    for secret in (AUTH_TOKEN, ACCOUNT_SID, pair):   # the pair is what the Authorization header holds
        assert secret not in written, "a secret reached a log line or an exception"
        assert secret not in objects, "a secret reached a repr"


def test_the_vendor_text_that_repeats_the_token_is_scrubbed_before_it_is_raised(stand_in, sender):
    stand_in.leak_secret_in_errors = True
    stand_in.fail_posts = ["401"]
    with pytest.raises(sms.ApiError) as err:
        sender.send(CUSTOMER, "hello")
    assert "Authenticate" in str(err.value), "the useful part of the vendor text is kept"
    assert AUTH_TOKEN not in str(err.value) and ACCOUNT_SID not in str(err.value)
    assert "<redacted>" in str(err.value)


def test_control_the_secret_scan_does_catch_a_leaky_log_line(caplog):
    """The scan above would pass on an empty capture, so prove it fails when a secret is planted."""
    caplog.set_level(logging.DEBUG)
    logging.getLogger(LOGGER).info("calling with token %s", AUTH_TOKEN)
    assert AUTH_TOKEN in everything_written(caplog, [RuntimeError("sid " + ACCOUNT_SID)])
    pair = base64.b64encode(f"{ACCOUNT_SID}:{AUTH_TOKEN}".encode()).decode()
    logging.getLogger(LOGGER).info("header Basic %s", pair)
    assert pair in everything_written(caplog, [])
    assert ACCOUNT_SID in everything_written(caplog, [RuntimeError("sid " + ACCOUNT_SID)])


def test_credentials_come_from_the_environment_and_a_missing_one_is_named_without_values():
    env = {"TWILIO_ACCOUNT_SID": ACCOUNT_SID, "TWILIO_AUTH_TOKEN": AUTH_TOKEN, "TWILIO_FROM_NUMBER": FROM_NUMBER}
    s = sms.SmsSender.from_env(env)
    assert s._creds.account_sid == ACCOUNT_SID and s._creds.auth_token == AUTH_TOKEN
    assert sms.SmsSender.from_env({**env, "TWILIO_MESSAGING_SERVICE_SID": SERVICE_SID})._sender_form == {"MessagingServiceSid": SERVICE_SID}
    with pytest.raises(sms.ConfigError) as err:
        sms.SmsSender.from_env({"TWILIO_ACCOUNT_SID": ACCOUNT_SID, "TWILIO_FROM_NUMBER": FROM_NUMBER})
    assert "TWILIO_AUTH_TOKEN" in str(err.value) and ACCOUNT_SID not in str(err.value)
    with pytest.raises(sms.ConfigError):
        sms.SmsSender.from_env({"TWILIO_ACCOUNT_SID": ACCOUNT_SID, "TWILIO_AUTH_TOKEN": AUTH_TOKEN})


def test_the_module_level_send_and_check_use_a_given_client(stand_in, sender):
    result = sms.send(CUSTOMER, "hello", client=sender)
    assert sms.check(result.sid, client=sender).delivered


# ------------------------------------------------------------------ (6) phone numbers are masked in logs

def test_a_phone_number_is_masked_to_its_last_four_digits_everywhere_it_is_written(stand_in, sender, sleeps, caplog):
    caplog.set_level(logging.DEBUG)
    errors = run_every_scenario(stand_in, sender, sleeps)
    written = everything_written(caplog, errors)
    digits = CUSTOMER.lstrip("+")
    assert CUSTOMER not in written and digits not in written, "the whole number was written somewhere"
    assert "***4567" in written, "the masked form is what the log line holds"


def test_the_number_the_vendor_echoes_in_its_error_text_is_masked_too(stand_in, sender):
    stand_in.reject_numbers = {CUSTOMER}
    with pytest.raises(sms.ApiError) as err:
        sender.send(CUSTOMER, "hello")
    assert CUSTOMER not in str(err.value) and CUSTOMER.lstrip("+") not in str(err.value)
    assert "***4567" in str(err.value)


def test_mask_number_keeps_only_the_last_four():
    assert sms.mask_number(CUSTOMER) == "***4567"
    assert sms.mask_number("1234") == "***" and sms.mask_number("") == "***"
    assert sms.mask_numbers_in("The 'To' number +639171234567 is bad, code 21211, sid " + SERVICE_SID) == \
        "The 'To' number ***4567 is bad, code 21211, sid " + SERVICE_SID


def test_control_the_masking_scan_catches_an_unmasked_number(caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger(LOGGER).info("sent to %s", CUSTOMER)
    assert CUSTOMER in everything_written(caplog, [])


# ------------------------------------------------------------------ the form body is exactly what the docs name

def test_the_request_body_is_plain_form_encoding_with_special_characters_intact(stand_in, sender):
    body = "Total: 5 & 6 = 11; ünïcode 100% ok + more"
    sender.send(CUSTOMER, body)
    assert stand_in.posts[0]["form"]["Body"] == body
