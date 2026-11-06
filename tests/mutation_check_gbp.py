# LIFESPAN: permanent (the repo's own mutation check for the Google Business Profile connector, run by hand)
"""Mutation control for app/connectors/gbp.py: break each guard one at a time and prove the connector tests notice.

It follows tests/mutation_check_sms.py. It needs no database and no Docker, runs only tests/test_gbp_connector.py, and demands more than "the suite went
red": each mutant names the test that must be among the failures, so a guard cannot be "covered" by an unrelated test that happens to break.

THIS EDITS app/connectors/gbp.py IN PLACE. It writes each mutation into the real file, runs the tests, and writes the original bytes back. Run it in a COPY of
the repository, never in a tree a sync might commit: a killed process can leave a weakened guard in the source, and `git diff` is how to check. A normal exit,
Ctrl-C and SIGTERM all restore the file first.

    python tests/mutation_check_gbp.py --check-targets      only confirm every guard finds its text
    python tests/mutation_check_gbp.py --match range,token  only the guards whose name holds a word
    python tests/mutation_check_gbp.py                      every guard
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app" / "connectors" / "gbp.py"
SUITE = "tests/test_gbp_connector.py"
BASELINE_TIMEOUT_SECONDS = 180
MUTANT_TIMEOUT_SECONDS = 120


def venv_python() -> Path:
    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidate = ROOT.joinpath(".venv", *rel)
    return candidate if candidate.exists() else Path(sys.executable)


PY = venv_python()

# (name, old text, new text, test-name fragments of which at least one must fail, why it matters)
MUTATIONS = [
    # -- the request
    ("range end not inclusive-adjusted",
     "last = window_end - timedelta(days=1)", "last = window_end",
     ["both_range_ends_inclusive", "one_day_window"], "the API's range is inclusive at both ends, so [1st, 4th) is asked to the 3rd"),
    ("only one metric asked",
     'params: list[tuple[str, str]] = [("dailyMetrics", m) for m in metrics]', 'params: list[tuple[str, str]] = [("dailyMetrics", metrics[0])]',
     ["asks_every_metric", "six_counts"], "one parameter per metric, or five of the six counts are silently zero"),
    ("one surface of impressions dropped",
     '"BUSINESS_IMPRESSIONS_MOBILE_MAPS", "BUSINESS_IMPRESSIONS_MOBILE_SEARCH"),', '"BUSINESS_IMPRESSIONS_MOBILE_MAPS"),',
     ["asks_every_metric", "six_counts", "recount"], "impressions are four counts added"),
    ("bearer token not sent",
     'headers={"Authorization": "Bearer " + self._bearer()}', "headers={}",
     ["asks_every_metric", "six_counts", "token_provider"], "the token is how Google knows the caller"),
    ("window limit off by one",
     "> MAX_DAYS:", ">= MAX_DAYS:",
     ["over_a_year"], "366 days is allowed"),
    ("window limit removed",
     "> MAX_DAYS:", "> MAX_DAYS + 5:",
     ["over_a_year"], "a window over a year is cut by the caller"),
    ("empty window allowed",
     "if window_end <= window_start:", "if window_end < window_start:",
     ["over_a_year", "empty_window"], "a window of no days is a caller bug"),
    ("cursor ignored",
     "        if cursor:\n            raise ValueError", "        if False:\n            raise ValueError",
     ["over_a_year", "empty_window", "cursor"], "a cursor this connector never issued must not be silently dropped"),
    ("location id not checked",
     '_LOCATION_RE = re.compile(r"^[0-9]{1,24}$")', '_LOCATION_RE = re.compile(r"^.{1,24}$")',
     ["not_numeric"], "an id with a slash or a colon would change the path that is called"),
    # -- reading the answer
    ("absent value not read as zero",
     "    if value is None:\n        return 0", '    if value is None:\n        raise UpstreamBroken("no value")',
     ["zero_is_absent", "six_counts", "recount"], "the API leaves the value out when it is zero"),
    ("unlisted day not read as zero",
     "totals.get((key, day), 0)", "totals[(key, day)]",
     ["zero_is_absent", "no_activity", "recount"], "a day the answer leaves out is a zero, not a crash"),
    ("sums not added",
     'totals[key] = totals.get(key, 0) + _count(point.get("value"), "the value of a datapoint")', 'totals[key] = _count(point.get("value"), "the value of a datapoint")',
     ["six_counts", "recount"], "four surfaces are added into one impressions count"),
    ("negative count accepted",
     '_INT64_RE = re.compile(r"^[0-9]{1,19}$")', '_INT64_RE = re.compile(r"^-?[0-9]{1,19}$")',
     ["whole_count"], "a count below zero is a broken answer"),
    ("count past the bigint accepted",
     "    if number > BIGINT_MAX:\n        raise UpstreamBroken", "    if False:\n        raise UpstreamBroken",
     ["whole_count"], "a number the snapshot column cannot hold must be refused here"),
    ("metric not asked for accepted",
     "if metric not in _KEY_OF:", "if False:",
     ["not_asked_for"], "an unknown metric must never be added into a total"),
    ("breakdown accepted",
     'if series.get("dailySubEntityType"):', "if False:",
     ["breakdown"], "a breakdown nobody asked for would be added twice"),
    ("same metric twice accepted",
     "if metric in seen_metrics:", "if False:",
     ["same_metric_twice"], "the same series twice doubles a count"),
    ("month value accepted",
     "    if not day:\n        raise UpstreamBroken", "    if False:\n        raise UpstreamBroken",
     ["month_value"], "a month total is not a day"),
    ("date outside the range accepted",
     "if not first <= day <= last:", "if False:",
     ["outside_the_requested_range"], "a day the request did not ask for must not be stored"),
    ("same date twice accepted",
     "if day in days:", "if False:",
     ["same_date"], "a repeated date doubles a day's count"),
    ("error inside a 200 accepted",
     'if body.get("error"):', "if False:",
     ["error_object_inside_a_200"], "Google can put an error object in the body"),
    # -- errors
    ("401 without a status not expired",
     'if http == 401 or status == "UNAUTHENTICATED":', 'if status == "UNAUTHENTICATED":',
     ["http_status_decides"], "a 401 with an empty body is still an expired token"),
    ("403 and 404 without a status not no-access",
     'if http in (403, 404) or status in ("PERMISSION_DENIED", "NOT_FOUND"):', 'if status in ("PERMISSION_DENIED", "NOT_FOUND"):',
     ["http_status_decides"], "a 403 with an empty body is still no access"),
    ("429 without a status not retried",
     'if http == 429 or status == "RESOURCE_EXHAUSTED" or http >= 500', 'if status == "RESOURCE_EXHAUSTED" or http >= 500',
     ["429_and_5xx_with_no_google_status"], "a 429 is the vendor saying slow down"),
    ("5xx without a status not retried",
     'or http >= 500 or status in ("UNAVAILABLE"', 'or status in ("UNAVAILABLE"',
     ["429_and_5xx_with_no_google_status"], "a 502 is the vendor saying try again"),
    ("google status ignored",
     'status = str(_error(body).get("status") or "")', 'status = ""',
     ["google_status_decides", "401_is_an_expired_token"], "the status says what the HTTP code does not"),
    ("bad request retried",
     'raise UpstreamBroken("Business Profile refused the request (%s)" % text)', "return Retry(None, text)",
     ["bad_request"], "a 400 will not pass by waiting"),
]


def run_suite(timeout: int = MUTANT_TIMEOUT_SECONDS):
    """Returns (all_green, failed test names), or (None, []) when the run hung."""
    for stale in (ROOT / "app" / "connectors" / "__pycache__").glob("gbp.*.pyc"):
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
    """Guards whose old text is gone from gbp.py, or that change nothing. Such a guard proves nothing."""
    text = TARGET.read_text(encoding="utf-8").replace("\r\n", "\n")
    problems = []
    for name, old, new, *_ in MUTATIONS:
        if text.count(old) != 1:
            problems.append(f"{name}: its target text appears {text.count(old)} times in gbp.py, it must appear once")
        elif old == new:
            problems.append(f"{name}: the mutation changes nothing")
    return problems


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
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
