# LIFESPAN: permanent (the repo's own mutation check for the SMS sender, run by hand)
"""Mutation control for app/sms.py: break each guard one at a time and prove the SMS tests notice.

It follows tests/mutation_check.py, but is narrower on purpose. It needs no database and no
Docker, runs only tests/test_sms.py (about 4 seconds a mutant) and demands more than "the suite
went red": each mutant names the test that must be among the failures, so a guard cannot be
"covered" by an unrelated test that happens to break.

THIS EDITS app/sms.py IN PLACE. It writes each mutation into the real file, runs the tests, and
writes the original bytes back. Run it in a COPY of the repository, never in a tree a sync might
commit: a killed process can leave a weakened guard in the source, and `git diff` is how to check.
A normal exit, Ctrl-C and SIGTERM all restore the file first.

    python tests/mutation_check_sms.py --check-targets      only confirm every guard finds its text
    python tests/mutation_check_sms.py --match retry,mask   only the guards whose name holds a word
    python tests/mutation_check_sms.py                      every guard

An argument it does not know is refused with exit 2, and nothing is edited.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app" / "sms.py"
SUITE = "tests/test_sms.py"
BASELINE_TIMEOUT_SECONDS = 180
MUTANT_TIMEOUT_SECONDS = 120


def venv_python() -> Path:
    """The repository venv's interpreter: Scripts on Windows, bin elsewhere, else the running one."""
    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidate = ROOT.joinpath(".venv", *rel)
    return candidate if candidate.exists() else Path(sys.executable)


PY = venv_python()

# (name, old text, new text, test-name fragments of which at least one must fail, why it matters)
MUTATIONS = [
    # -- the E.164 check
    ("E.164 check removed",
     "if not isinstance(to, str) or not E164.fullmatch(to):", "if not isinstance(to, str):",
     ["not_e164"], "a malformed number must be refused before any request"),
    ("E.164 accepts 7 digits",
     "[1-9][0-9]{7,14}\")\n#: A messaging", "[1-9][0-9]{6,14}\")\n#: A messaging",
     ["not_e164"], "the lower bound of 8 digits"),
    ("E.164 accepts 16 digits",
     "[1-9][0-9]{7,14}\")\n#: A messaging", "[1-9][0-9]{7,15}\")\n#: A messaging",
     ["not_e164"], "the upper bound of 15 digits"),
    ("E.164 accepts a leading zero",
     'E164 = re.compile(r"\\+[1-9]', 'E164 = re.compile(r"\\+[0-9]',
     ["not_e164"], "a country code never starts with 0"),
    ("E.164 matches a prefix only",
     "E164.fullmatch(to)", "E164.match(to)",
     ["not_e164"], "trailing junk after a valid number"),
    # -- the body limits
    ("length limit removed",
     "if len(body) > MAX_BODY_CHARS:", "if False:",
     ["over_long_body"], "Twilio documents 1,600 characters at most"),
    ("length limit off by one",
     "if len(body) > MAX_BODY_CHARS:", "if len(body) >= MAX_BODY_CHARS:",
     ["1600_characters_is_accepted"], "exactly 1600 characters is allowed"),
    ("whitespace-only body accepted",
     "not body.strip()", "not body",
     ["empty_or_over_long_body"], "a blank message is still empty"),
    # -- Retry-After
    ("Retry-After ignored",
     "delay = self._retry_after(resp_headers)\n                if delay is None:", "delay = None\n                if delay is None:",
     ["retry_after"], "a 429 must wait as long as the vendor asked"),
    ("Retry-After not capped",
     "return max(0.0, min(self.max_retry_after, seconds))", "return max(0.0, seconds)",
     ["huge_retry_after"], "one odd header must not stall a worker for hours"),
    # -- retry caps
    ("5xx and 429 retry cap removed",
     "if attempt >= self.max_retries:\n                    err =", "if attempt >= self.max_retries + 5:\n                    err =",
     ["retry_cap"], "a vendor outage must not be retried forever"),
    ("unreachable retry cap removed",
     "if attempt >= self.max_retries:\n                    raise NetworkError", "if attempt >= self.max_retries + 5:\n                    raise NetworkError",
     ["refused_connection", "reading_a_message"], "a dead network must not be retried forever"),
    ("backoff not capped",
     "return min(cap, base * (2 ** attempt))", "return base * (2 ** attempt)",
     ["backoff_doubles_and_is_capped", "backoff_between_looks_is_capped"], "the delay stops growing at the cap"),
    ("5xx not retried",
     "if status == 429 or status >= 500:", "if status == 429:",
     ["503"], "a 503 is the vendor saying try again later"),
    # -- no blind retry
    ("lost response retried inside the sender",
     "if isinstance(exc, _Unknown) and not retry_unknown:\n                    raise", "if False:\n                    raise",
     ["lost_response_is_not_retried", "timeout_after_the_request", "may_come_after_the_request"], "a retry could text the customer twice"),
    ("any network error treated as not sent",
     "if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):", "if True:",
     ["may_come_after_the_request"], "only a refused connection is certain to be unsent"),
    ("read timeout treated as not sent",
     "except (TimeoutError, socket.timeout, OSError, http.client.HTTPException) as exc:\n            raise _Unknown(", "except (TimeoutError, socket.timeout, OSError, http.client.HTTPException) as exc:\n            raise _NotSent(",
     ["lost_response_is_not_retried", "timeout_after_the_request", "may_come_after_the_request"], "a timeout after writing is the dangerous case"),
    ("unreadable 201 not treated as unknown",
     "if not isinstance(data, dict) or not isinstance(data.get(\"sid\"), str) or not isinstance(data.get(\"status\"), str):", "if False:",
     ["body_we_cannot_read"], "a 201 we cannot read still means a message probably exists"),
    ("idempotency key not sent",
     "{IDEMPOTENCY_HEADER: key}", "{}",
     ["same_key", "distinct_key", "property_with_faults"], "the key is how a safe resend is recognised"),
    ("caller's key ignored",
     "key = validate_key(idempotency_key) if idempotency_key is not None else uuid.uuid4().hex", "key = uuid.uuid4().hex",
     ["same_key", "property_with_faults"], "a resend must reuse the key it was given"),
    ("vendor's duplicate answer not understood",
     "if status == 409 and isinstance(data, dict) and isinstance(data.get(\"existing_sid\"), str):", "if False:",
     ["same_key", "property_with_faults"], "a repeated key must resolve to the first message"),
    ("lost read not retried",
     "retry_unknown=True, label=sid", "retry_unknown=False, label=sid",
     ["lost_or_unavailable_read", "reading_a_message"], "reading is safe to repeat"),
    # -- secrets
    ("vendor text not scrubbed of the token",
     "text = mask_numbers_in(self._creds.scrub(text))[:300]", "text = mask_numbers_in(text)[:300]",
     ["scrubbed", "neither_secret"], "a vendor message that repeats the token must not be raised as is"),
    ("credentials repr shows the token",
     "return \"Credentials(account_sid=<set>, auth_token=<set>)\"", "return f\"Credentials({self.account_sid}, {self.auth_token})\"",
     ["neither_secret"], "a repr lands in tracebacks and debug output"),
    ("sender repr shows the token",
     "return f\"SmsSender(base_url={self._base!r})\"", "return f\"SmsSender({self._creds.auth_token})\"",
     ["neither_secret"], "a repr lands in tracebacks and debug output"),
    ("log line carries the auth header",
     "(accepted, not delivered)\", data[\"sid\"], data[\"status\"], masked)", "(accepted, not delivered)\", data[\"sid\"], data[\"status\"], self._auth)",
     ["neither_secret"], "the header is base64 of sid:token and must never be logged either"),
    # -- number masking
    ("number logged unmasked",
     "masked = mask_number(to)", "masked = to",
     ["masked_to_its_last_four"], "a customer's number in a log is personal data"),
    ("mask keeps the whole number",
     "return \"***\" + text[-4:] if len(text) > 4 else \"***\"", "return text",
     ["masked_to_its_last_four", "mask_number_keeps"], "only the last four may survive"),
    ("vendor-echoed number not masked",
     "return _NUMBER_IN_TEXT.sub(lambda m: mask_number(m.group(0)), text)", "return text",
     ["echoes_in_its_error_text", "mask_number_keeps"], "Twilio error text repeats the number"),
    ("retry log carries the raw number",
     "retry_unknown=False, label=masked)", "retry_unknown=False, label=to)",
     ["masked_to_its_last_four"], "the retry warning is where a number would leak first"),
    # -- the final-status loop
    ("any status taken as final",
     "if status in FINAL_STATUSES:", "if True:",
     ["polls_through_the_middle"], "queued, sending and sent are not outcomes"),
    ("undelivered not a final status",
     "FINAL_STATUSES = frozenset({\"delivered\", \"undelivered\", \"failed\", \"canceled\"})", "FINAL_STATUSES = frozenset({\"delivered\", \"failed\", \"canceled\"})",
     ["carrier_block"], "undelivered is the status the client job missed"),
    ("poll bound removed",
     "for poll in range(1, self.poll_attempts + 1):", "for poll in range(1, self.poll_attempts + 4):",
     ["gives_up_after_a_bounded"], "polling must stop"),
    ("poll delay not capped",
     "_backoff(self.poll_base, self.poll_cap, poll - 1)", "_backoff(self.poll_base, 1e9, poll - 1)",
     ["gives_up_after_a_bounded", "backoff_between_looks_is_capped"], "the delay between looks stops growing"),
    ("error code dropped",
     "error_code = ec if isinstance(ec, int) else None", "error_code = None",
     ["carrier_block", "failed_message"], "the code is the reason the message did not arrive"),
    ("unsettled poll reported as settled",
     "return CheckResult(sid, status, error_code, False, self.poll_attempts)", "return CheckResult(sid, status, error_code, True, self.poll_attempts)",
     ["gives_up_after_a_bounded"], "a message still in flight must not look finished"),
    # -- the request itself
    ("auth header not sent",
     "headers = {\"Authorization\": self._auth, \"Accept\"", "headers = {\"Accept\"",
     ["posts_the_form"], "basic auth is how the vendor knows the account"),
]


def run_suite(timeout: int = MUTANT_TIMEOUT_SECONDS):
    """Returns (all_green, failed test names), or (None, []) when the run hung."""
    # A mutant often has the same byte length as the original and is written within a second of the
    # restore, which is exactly what Python's .pyc staleness check (mtime in whole seconds + size)
    # cannot tell apart. Compiled copies are deleted and not written so every run reads the real source.
    for stale in (ROOT / "app" / "__pycache__").glob("sms.*.pyc"):
        stale.unlink(missing_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        r = subprocess.run(
            [str(PY), "-B", "-m", "pytest", SUITE, "-q", "--tb=no", "-rf", "-p", "no:cacheprovider"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return None, []
    failed = [line.split(" ", 1)[1] for line in r.stdout.splitlines() if line.startswith("FAILED ")]
    return r.returncode == 0, failed


def missing_targets() -> list[str]:
    """Guards whose old text is gone from app/sms.py, or that change nothing. Such a guard proves nothing."""
    text = TARGET.read_text(encoding="utf-8")
    problems = []
    for name, old, new, *_ in MUTATIONS:
        if text.count(old) != 1:
            problems.append(f"{name}: its target text appears {text.count(old)} times in sms.py, it must appear once")
        elif old == new:
            problems.append(f"{name}: the mutation changes nothing")
    return problems


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("--check-targets", action="store_true", help="only check that every guard finds its target text, edit nothing")
    parser.add_argument("--match", help="comma separated words; only guards whose name holds one run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    problems = missing_targets()
    if problems:
        print("update this file before mutating:\n  " + "\n  ".join(problems))
        return 1
    if args.check_targets:
        print(f"all {len(MUTATIONS)} guards find their target")
        return 0

    match = args.match.split(",") if args.match else None
    original = TARGET.read_bytes()

    def _terminate(signum, frame):
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except ValueError:
        pass

    killed = survived = wrong_test = 0
    try:
        green, _ = run_suite(BASELINE_TIMEOUT_SECONDS)
        if green is not True:
            print("baseline suite is RED or hung, fix that before mutating")
            return 1
        print("baseline: green")
        # The guards are written with LF; the file on disk may use CRLF (a Windows checkout), which
        # would make every multi-line guard a silent no-op. Match on normalised text and write the
        # line endings back the way they were.
        crlf = b"\r\n" in original
        text = original.decode("utf-8").replace("\r\n", "\n")
        ran = 0
        for name, old, new, expected, why in MUTATIONS:
            if match and not any(word in name for word in match):
                continue
            ran += 1
            mutated = text.replace(old, new, 1)
            assert mutated != text, f"{name}: the mutation changed nothing, so a green result would mean nothing"
            if crlf:
                mutated = mutated.replace("\n", "\r\n")
            try:
                TARGET.write_bytes(mutated.encode("utf-8"))
                green, failed = run_suite()
            finally:
                TARGET.write_bytes(original)
            if green is None:
                print(f"[ok]   {name}: suite HUNG, so the guard also stops an endless loop")
                killed += 1
            elif green:
                print(f"[FAIL] {name}: suite stayed GREEN, so nothing tests that {why}")
                survived += 1
            elif any(frag in test for test in failed for frag in expected):
                print(f"[ok]   {name}: failed {len(failed)} test(s), including the expected one")
                killed += 1
            else:
                print(f"[FAIL] {name}: the suite went red but not in {expected}; failed: {failed[:4]}")
                wrong_test += 1

        print()
        green, _ = run_suite(BASELINE_TIMEOUT_SECONDS)
        if green is not True:
            print("suite is RED or hung after restoring, a mutation leaked")
            return 1
        print("source restored, suite green again")
        print(f"mutants run {ran}: killed {killed}, survived {survived}, killed by the wrong test {wrong_test}")
        bad = survived + wrong_test
        print(f"mutation control: {'PASS' if bad == 0 else str(bad) + ' UNTESTED GUARD(S)'}")
        return 1 if bad else 0
    finally:
        if TARGET.read_bytes() != original:
            TARGET.write_bytes(original)


if __name__ == "__main__":
    sys.exit(main())
