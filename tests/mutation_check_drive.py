# LIFESPAN: permanent (the repo's own mutation check for the Google Drive delivery step, run by hand)
"""Mutation control for app/report/drive.py: break each guard one at a time and prove the Drive tests notice.

Same engine as tests/mutation_check_sms.py: no database, no Docker, runs only tests/test_drive.py (a few seconds a mutant), and each mutant
names the test that must be among the failures, so a guard cannot be "covered" by an unrelated test that happens to break.

THIS EDITS app/report/drive.py IN PLACE. It writes each mutation into the real file, runs the tests, and writes the original bytes back.
Run it in a COPY of the repository, never in a tree a sync might commit: a killed process can leave a weakened guard in the source, and
`git diff` is how to check. A normal exit, Ctrl-C and SIGTERM all restore the file first.

    python tests/mutation_check_drive.py --check-targets      only confirm every guard finds its text exactly once
    python tests/mutation_check_drive.py --match retry,mask   only the guards whose name holds a word
    python tests/mutation_check_drive.py                      every guard

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
TARGET = ROOT / "app" / "report" / "drive.py"
SUITE = "tests/test_drive.py"
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
    # -- finding the file that is already there
    ("search keeps trashed files",
     "'%s' in parents and trashed = false\"", "'%s' in parents\"",
     ["in_the_trash_with_the_same_name"], "a file in the trash is not the deck"),
    ("search not limited to the folder",
     "q = \"name = '%s' and '%s' in parents and trashed = false\" % (q_escape(name), q_escape(folder_id))", "q = \"name = '%s' and trashed = false\" % q_escape(name)",
     ["another_folder"], "the same name elsewhere is somebody else's file"),
    ("name not escaped in the query",
     "% (q_escape(name), q_escape(folder_id))", "% (name, q_escape(folder_id))",
     ["apostrophe_and_a_backslash"], "an apostrophe in a name breaks the query string"),
    ("backslash not escaped",
     "return text.replace(\"\\\\\", \"\\\\\\\\\").replace(\"'\", \"\\\\'\")", "return text.replace(\"'\", \"\\\\'\")",
     ["escapes_a_backslash", "apostrophe_and_a_backslash"], "a backslash must be doubled"),
    ("oldest copy not asked for first",
     "\"orderBy\": \"createdTime\", ", "",
     ["oldest_is_the_one_replaced"], "the original is the file to keep"),
    ("search stops after the first page",
     "token = page.get(\"nextPageToken\")", "token = None",
     ["next_page_token"], "duplicates past the first page would be missed"),
    ("search result ignored",
     "matches = self.find(name, folder_id)", "matches = []",
     ["never_leave_a_second_copy", "lost_answer_to_a_create"], "without the lookup every run creates a new file"),
    # -- create, replace, leave alone
    ("same bytes still uploaded",
     "if keep.get(\"md5Checksum\") == digest:", "if False:",
     ["same_bytes_sends_nothing"], "a rerun with nothing new must write nothing"),
    ("replace creates a new file",
     "meta, kind = self.replace(keep[\"id\"], data, mime, upload)", "meta, kind = self.create(name, folder_id, data, mime, upload)",
     ["replaces_the_content", "never_leave_a_second_copy"], "a rerun must not add a copy"),
    ("replace sent as POST",
     "return self._upload(\"PATCH\", \"/drive/v3/files/\"", "return self._upload(\"POST\", \"/drive/v3/files/\"",
     ["replaces_the_content", "resumable_replace"], "Drive updates a file with PATCH"),
    ("duplicates not counted",
     "keep, extra = matches[0], len(matches) - 1", "keep, extra = matches[0], 0",
     ["duplicate_is_reported", "next_page_token"], "a second copy someone made should be reported"),
    # -- the folder
    ("folder never checked",
     "self.check_folder(folder_id)\n                matches", "matches",
     ["does_not_exist"], "a wrong id must be one clear error"),
    ("a file accepted as the folder",
     "if meta.get(\"mimeType\") != FOLDER_MIME:", "if False:",
     ["given_as_the_folder"], "uploading into a file id is an error"),
    ("a trashed folder accepted",
     "if meta.get(\"trashed\"):", "if False:",
     ["folder_in_the_trash"], "a deck in a binned folder is lost"),
    # -- the upload types
    ("multipart limit off by one",
     "(\"multipart\" if len(data) <= MULTIPART_LIMIT else \"resumable\")", "(\"multipart\" if len(data) < MULTIPART_LIMIT else \"resumable\")",
     ["at_exactly_the_limit"], "the documentation says 5 MB or less is multipart"),
    ("large decks never go resumable",
     "(\"multipart\" if len(data) <= MULTIPART_LIMIT else \"resumable\")", "\"multipart\"",
     ["over_five_megabytes"], "above 5 MB the documentation says resumable"),
    ("multipart metadata part not JSON",
     "\\r\\nContent-Type: application/json; charset=UTF-8\\r\\n\\r\\n%s", "\\r\\nContent-Type: text/plain\\r\\n\\r\\n%s",
     ["created_in_the_folder", "metadata_first"], "the metadata part must be application/json"),
    ("multipart closing boundary missing",
     "(\"\\r\\n--%s--\" % boundary)", "(\"\\r\\n--%s\" % boundary)",
     ["metadata_first_and_the_media_second"], "a multipart body ends with two hyphens after the final boundary"),
    ("resumable length header wrong",
     "\"X-Upload-Content-Length\": str(total)", "\"X-Upload-Content-Length\": str(total + 1)",
     ["over_five_megabytes", "resumable_can_be_asked_for"], "the declared length must match the bytes"),
    ("resume restarts from zero",
     "offset = self._received(session, total)", "offset = 0",
     ["resumes_from_the_byte"], "an interrupted upload continues, it does not start over"),
    ("resume offset off by one",
     "return int(rng.rsplit(\"-\", 1)[1]) + 1 if rng else 0", "return int(rng.rsplit(\"-\", 1)[1]) if rng else 0",
     ["resumes_from_the_byte"], "Range bytes=0-42 means 43 bytes are held, the next is 43"),
    # -- retries
    ("nothing is retried",
     "retryable = status in RETRY_STATUSES or (status == 403 and reason in RATE_LIMIT_REASONS)", "retryable = False",
     ["a_429_waits", "5xx_is_retried", "403_rate_limit_is_retried"], "Google says to retry 5xx and rate limits"),
    ("every 403 is retried",
     "(status == 403 and reason in RATE_LIMIT_REASONS)", "(status == 403)",
     ["403_that_is_not_a_rate_limit"], "a permissions refusal will not fix itself"),
    ("429 not retried",
     "RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})", "RETRY_STATUSES = frozenset({500, 502, 503, 504})",
     ["a_429_waits"], "429 is in the documented retry list"),
    ("Retry-After ignored",
     "wait = max(0.0, min(MAX_RETRY_AFTER, float(retry_after)))", "wait = None",
     ["a_429_waits"], "wait as long as the server asked"),
    ("Retry-After not capped",
     "wait = max(0.0, min(MAX_RETRY_AFTER, float(retry_after)))", "wait = max(0.0, float(retry_after))",
     ["huge_retry_after"], "one odd header must not stall a job for a day"),
    ("backoff does not grow",
     "return min(self._backoff_cap, self._backoff_base * (2 ** attempt))", "return min(self._backoff_cap, self._backoff_base)",
     ["doubling_delay", "delay_between_tries_is_capped"], "exponential backoff is what the documentation asks for"),
    ("backoff not capped",
     "return min(self._backoff_cap, self._backoff_base * (2 ** attempt))", "return self._backoff_base * (2 ** attempt)",
     ["delay_between_tries_is_capped"], "the delay stops growing"),
    ("attempt limit removed",
     "if not exc.retryable or attempt == self.max_attempts - 1:", "if not exc.retryable:",
     ["stop_after_the_attempt_limit"], "retrying must end"),
    # -- credentials
    ("a 401 is not renewed",
     "if exc.code == 401 and not renewed and hasattr(self._token, \"refresh\"):", "if False:",
     ["401_in_the_middle"], "an access token can expire between two calls"),
    ("an expired token is used as is",
     "if self._access and self._expiry - self._now() > 60:", "if self._access:",
     ["expired_token_file_is_renewed", "expires_within_a_minute"], "an expired token is refused"),
    ("no margin before expiry",
     "if self._access and self._expiry - self._now() > 60:", "if self._access and self._expiry - self._now() > 0:",
     ["expires_within_a_minute"], "a token about to expire can lapse mid upload"),
    ("wrong grant type",
     "\"grant_type\": \"refresh_token\", ", "\"grant_type\": \"password\", ",
     ["renewed_with_the_refresh_grant"], "the refresh grant is grant_type=refresh_token"),
    ("unreadable token file names its path",
     "raise DriveConfigError(\"the Google token file could not be read (%s)\" % type(exc).__name__) from None", "raise DriveConfigError(\"the Google token file could not be read (%s)\" % exc) from None",
     ["unreadable_token_file"], "a path can hold a user name"),
    ("static token repr shows the token",
     "return \"StaticToken(<set>)\"", "return \"StaticToken(%s)\" % self._token",
     ["no_secret_reaches"], "a repr lands in tracebacks and debug output"),
    ("token file repr shows the refresh token",
     "return \"OAuthTokenFile(<set>)\"", "return \"OAuthTokenFile(%s)\" % self._refresh_token",
     ["no_secret_reaches"], "a repr lands in tracebacks and debug output"),
    # -- settings and names
    ("folder id not required",
     "    if not folder:\n", "    if False:\n",
     ["names_the_missing_setting"], "no folder means nowhere to deliver"),
    ("missing token message names no variable",
     "raise DriveConfigError(\"set GOOGLE_DRIVE_TOKEN_FILE (an authorized-user token file) or GOOGLE_DRIVE_ACCESS_TOKEN\")", "raise DriveConfigError(\"set the token\")",
     ["names_the_missing_setting"], "say which variable to set"),
    ("file name not slugged",
     "slug = re.sub(r\"[^a-z0-9]+\", \"-\", (label or \"\").lower()).strip(\"-\")", "slug = (label or \"\")",
     ["file_name_is_stable"], "a label must not put slashes or spaces in the file name"),
]


def run_suite(timeout: int = MUTANT_TIMEOUT_SECONDS):
    """Returns (all_green, failed test names), or (None, []) when the run hung."""
    # A mutant often has the same byte length as the original and is written within a second of the
    # restore, which is exactly what Python's .pyc staleness check (mtime in whole seconds + size)
    # cannot tell apart. Compiled copies are deleted and not written so every run reads the real source.
    for stale in (ROOT / "app" / "report" / "__pycache__").glob("drive.*.pyc"):
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
    """Guards whose old text is gone from app/report/drive.py, or that change nothing. Such a guard proves nothing."""
    text = TARGET.read_text(encoding="utf-8")
    problems = []
    for name, old, new, *_ in MUTATIONS:
        if text.count(old) != 1:
            problems.append(f"{name}: its target text appears {text.count(old)} times in drive.py, it must appear once")
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
