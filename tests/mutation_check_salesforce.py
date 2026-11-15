# LIFESPAN: permanent (the repo's own mutation check for the Salesforce connector and the lead matching, run by hand)
"""Mutation control for app/connectors/salesforce.py and app/lead_match.py: break each guard one at a time and prove the tests notice.

It follows tests/mutation_check_gbp.py. It needs no database and no Docker, runs only tests/test_salesforce_connector.py and tests/test_lead_match.py, and demands
more than "the suite went red": each mutant names the test that must be among the failures, so a guard cannot be "covered" by an unrelated test that happens to break.

THIS EDITS THE TWO SOURCE FILES IN PLACE. It writes each mutation into the real file, runs the tests, and writes the original bytes back. Run it in a COPY of the
repository, never in a tree a sync might commit: a killed process can leave a weakened guard in the source, and `git diff` is how to check. A normal exit, Ctrl-C and
SIGTERM all restore the files first.

    python tests/mutation_check_salesforce.py --check-targets      only confirm every guard finds its text
    python tests/mutation_check_salesforce.py --match range,token  only the guards whose name holds a word
    python tests/mutation_check_salesforce.py                      every guard
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SF = ROOT / "app" / "connectors" / "salesforce.py"
LM = ROOT / "app" / "lead_match.py"
SUITE = ["tests/test_salesforce_connector.py", "tests/test_lead_match.py"]
BASELINE_TIMEOUT_SECONDS = 240
MUTANT_TIMEOUT_SECONDS = 180


def venv_python() -> Path:
    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidate = ROOT.joinpath(".venv", *rel)
    return candidate if candidate.exists() else Path(sys.executable)


PY = venv_python()

# (file, name, [(old, new), ...], test-name fragments of which at least one must fail, why it matters)
MUTATIONS = [
    # -- the request
    (SF, "range end inclusive", [('"%s >= %s AND %s < %s"', '"%s >= %s AND %s <= %s"')],
     ["queries_are_soql", "five_counts"], "the window is half open: the end day belongs to the next window"),
    (SF, "datetime literal quoted", [('return day.isoformat() + "T00:00:00Z"', 'return "\'" + day.isoformat() + "T00:00:00Z\'"')],
     ["queries_are_soql", "five_counts"], "SOQL refuses a quoted datetime literal"),
    (SF, "bearer token not sent", [('"Authorization": "Bearer " + token', '"Authorization": token')],
     ["five_counts", "queries_are_soql"], "the token is how Salesforce knows the caller"),
    (SF, "batch size header dropped", [('"Sforce-Query-Options": "batchSize=%d" % self._batch', '"X-Batch": "batchSize=%d" % self._batch')],
     ["long_result", "queries_are_soql"], "the batch size is how a page is bounded"),
    (SF, "pacing off", [("if limit - used < self._min_remaining:", "if False:")],
     ["stops_before_a_call", "floor_of_zero"], "a sync must leave the org's other users their calls"),
    (SF, "limit header not read", [("self.api_usage = (int(m.group(1)), int(m.group(2)))", "self.api_usage = None")],
     ["limit_header_is_read", "stops_before_a_call"], "pacing reads what the platform reports"),
    # -- paging
    (SF, "next page url not checked", [('or not nxt.startswith("/services/data/") or nxt in seen_urls or ".." in nxt', ""),
                                       ('if not path.startswith("/services/data/"):', "if False:")],
     ["leaves_the_data_api"], "the token must never be sent to a host the answer names"),
    (SF, "repeated next page allowed", [('or nxt in seen_urls or ".." in nxt', 'or ".." in nxt')],
     ["repeated_next_page"], "a next page that names itself would loop"),
    (SF, "total not checked", [('if body["totalSize"] != len(records):', "if False:")],
     ["total_that_does_not_match"], "a page lost on the way must not chart as fewer leads"),
    (SF, "done with a next page accepted", [('if body.get("nextRecordsUrl"):\n                    raise UpstreamBroken("the query says done', 'if False:\n                    raise UpstreamBroken("the query says done')],
     ["done_with_a_next_page"], "an answer that contradicts itself is broken"),
    (SF, "duplicate id accepted", [("if ident in ids:", "if False:")],
     ["malformed_answer"], "the same record twice doubles a count"),
    (SF, "record type not checked", [('if not isinstance(attributes, dict) or attributes.get("type") != expected:', "if not isinstance(attributes, dict):")],
     ["malformed_answer"], "a lead counted as an opportunity is a wrong chart"),
    (SF, "id shape not checked", [('_ID_RE = re.compile(r"^[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?$")', '_ID_RE = re.compile(r"^.*$")')],
     ["malformed_answer"], "an Id that is not a Salesforce Id is a broken row"),
    (SF, "row outside the window accepted", [("if not start <= day < end:", "if False:")],
     ["malformed_answer", "seeded_world"], "a day the query did not ask for must not be stored"),
    (SF, "datetime kept in its own zone", [("return local.astimezone(timezone.utc)", "return local")],
     ["read_into_the_utc_day"], "a lead created at 01:30 in Manila belongs to the previous UTC day"),
    (SF, "unconverted row counted", [('if r.get("IsConverted") is not True:', "if False:")],
     ["not_converted"], "the converted query must hold only converted leads"),
    (SF, "unwon row counted", [('if r.get("IsWon") is not True:', "if False:")],
     ["not_won"], "the won query must hold only won opportunities"),
    # -- money
    (SF, "other currency added in", [('if multi and r.get("CurrencyIsoCode") != self.currency:', "if False:")],
     ["multi_currency_org", "seeded_world"], "euros added to dollars is the defect that charts without complaint"),
    (SF, "currency field never asked", [('(", CurrencyIsoCode" if multi else "")', '""')],
     ["multi_currency_org"], "without the field the currency of a row is unknown"),
    (SF, "negative amount refused", [("allow_negative=True", "allow_negative=False")],
     ["five_counts", "seeded_world"], "a credit is an amount"),
    (SF, "null amount not read as zero", [("if amount is not None:", "if True:")],
     ["five_counts", "seeded_world"], "an opportunity can have no Amount"),
    # -- inputs
    (SF, "cursor ignored", [('if cursor:\n            raise ValueError("the Salesforce', 'if False:\n            raise ValueError("the Salesforce')],
     ["cursor_and_an_empty_window"], "a cursor this connector never issued must not be silently dropped"),
    (SF, "empty window allowed", [('if window_end <= window_start:\n            raise ValueError("the window must hold at least one day")\n        if cursor:',
                                   'if window_end < window_start:\n            raise ValueError("the window must hold at least one day")\n        if cursor:')],
     ["cursor_and_an_empty_window"], "a window of no days is a caller bug"),
    (SF, "currency not checked", [("if not _CURRENCY_RE.match(str(currency)):", "if False:")],
     ["bad_options"], "money without an ISO currency is refused downstream"),
    (SF, "instance scheme not checked", [('if parsed.scheme != "https" and not (loopback and parsed.scheme == "http"):', "if False:")],
     ["org_address_that_is_not"], "a token sent over plain http leaves in the clear"),
    (SF, "instance address shape not checked", [('parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")', "False")],
     ["org_address_that_is_not"], "an address with a login or a path is not the org"),
    # -- describe
    (SF, "missing field not noticed", [("if found is None:", "if False:")],
     ["hidden_from_this_user"], "a hidden field must be named before any query runs"),
    (SF, "field type not checked", [('if field_name != "Id" and found.get("type") != kind', 'if False and found.get("type") != kind')],
     ["wrong_type"], "a field of another type reads as nonsense"),
    (SF, "conversion link not checked", [('if not isinstance(link, dict) or link.get("referenceTo") != ["Opportunity"]:', "if not isinstance(link, dict):")],
     ["points_somewhere_else"], "the conversion counts rely on the lead to opportunity relationship"),
    (SF, "api calls undercounted", [("calls, waits = self._spent", "calls, waits = 0, 0")],
     ["api_calls_counts_every_request"], "the run record must count every request"),
    # -- OAuth
    (SF, "no refresh on a 401", [("if attempt == 0 and self._tokens is not None:", "if False:")],
     ["401_mid_sync"], "an expired session must be renewed, not reported as a failure"),
    (SF, "stale token check removed", [("if self._access is not None and self._access != rejected:", "if False:")],
     ["many_callers"], "simultaneous refreshes with one refresh token fail"),
    (SF, "secrets sent in the query", [('data={"grant_type": "refresh_token"', 'params={"grant_type": "refresh_token"')],
     ["first_call_exchanges"], "the page says never put secrets in the query string"),
    (SF, "rotated token forgotten", [("self._refresh = Secret(rotated)", "self._refresh = self._refresh")],
     ["rotated_refresh_token"], "the old refresh token is dead the moment a new one arrives"),
    (SF, "refused refresh not a reconnect", [('if code in ("invalid_grant", "inactive_user", "invalid_client", "invalid_client_id", "unauthorized_client"):', "if False:")],
     ["person_must_reconnect"], "retrying a dead refresh token burns quota while looking like progress"),
    (SF, "token type not checked", [('if body.get("token_type") != "Bearer":', "if False:")],
     ["not_a_bearer_token"], "a token that is not a bearer token is sent wrong"),
    # -- errors
    (SF, "401 not expired", [('if http == 401:\n            raise AuthExpired("the access token was refused', 'if False:\n            raise AuthExpired("the access token was refused')],
     ["401_is_an_expired", "pasted_access_token"], "a 401 is the session ending"),
    (SF, "limit exceeded not handed back", [('if http == 403 and "REQUEST_LIMIT_EXCEEDED" in codes:', "if False:")],
     ["request_limit_exceeded"], "waiting a minute does not refill a daily limit"),
    (SF, "403 not no-access", [('raise AuthRevoked("this user is not allowed to read that (%s)" % text)', 'raise UpstreamBroken("this user is not allowed to read that (%s)" % text)')],
     ["401_is_an_expired", "http_status_decides"], "a 403 is a person's permission"),
    (SF, "5xx not retried", [("if http in (500, 502, 503):", "if False:")],
     ["500_502_503"], "a 503 is the vendor saying try again"),
    (SF, "lead field type not checked", [("if r.get(field) is not None and not isinstance(r[field], str):", "if False:")],
     ["phone_that_is_not_text"], "a number where text should be would reach the matcher"),
    (SF, "record link id not checked", [("if not isinstance(record_id, str) or not _ID_RE.match(record_id):", "if not isinstance(record_id, str):")],
     ["link_to_its_own_record"], "a value from an answer must never turn a record link into another address"),
    # -- the matching step
    (LM, "record links dropped", [("links = tuple(by_id[l].url for l in cluster.lead_ids if by_id[l].url)", "links = ()")],
     ["record_links_of_its_leads"], "a drill-through needs the link to the source record"),
    (LM, "trunk zero kept", [('if default_country != "1" and digits.startswith("0"):', "if False:")],
     ["become_one_key"], "09171234567 and 9171234567 are one number"),
    (LM, "extension kept", [('text = _EXT_RE.sub("", (raw or "").strip())', 'text = (raw or "").strip()')],
     ["become_one_key"], "an extension is not part of the number"),
    (LM, "digit bounds removed", [("MIN_DIGITS, MAX_DIGITS = 8, 15", "MIN_DIGITS, MAX_DIGITS = 0, 99")],
     ["cannot_be_trusted"], "a wrong match merges two strangers"),
    (LM, "email case kept", [('text = (raw or "").strip().lower()', 'text = (raw or "").strip()')],
     ["lower_cased", "same_email_in_any_case"], "Pat@X.com and pat@x.com are one person"),
    (LM, "survivor not the oldest", [("group.sort(key=lambda l: (l.created_at, l.id))", "group.sort(key=lambda l: (l.id,))")],
     ["oldest_survives", "planted_people"], "the first lead is the record of the person"),
    (LM, "shared line merges", [("if len(people) >= 2:", "if False:")],
     ["shared_line", "planted_people"], "an office line behind two emails is not one person"),
    (LM, "email never merges", [("sets.union(by_email[email], lead_id)", "pass")],
     ["same_email", "planted_people"], "the same email is the same person"),
    (LM, "phone never merges", [("sets.union(held[0], other)", "pass")],
     ["same_phone", "one_email_and_an_email_less"], "the same phone with no conflicting email is the same person"),
    (LM, "window edge excluded", [("first - window <= call.started_at <= first", "first - window < call.started_at <= first")],
     ["after_the_lead_or_before"], "exactly at the edge counts"),
    (LM, "call after the lead attributes", [("first - window <= call.started_at <= first", "first - window <= call.started_at <= first + window")],
     ["after_the_lead_or_before", "reason_code"], "a call after the lead did not bring the lead in"),
    (LM, "shared number attributes", [("if len(owners.get(number, ())) > 1:", "if False:")],
     ["shared_line", "reason_code"], "a call from an office line proves nothing about one person"),
    (LM, "latest call is the first touch", [("call = usable[0]", "call = usable[-1]")],
     ["earliest_call", "reason_code"], "the first touch is the earliest"),
    (LM, "override flag always set", [("overrode_lead_source=bool(own and own != label)", "overrode_lead_source=True")],
     ["earliest_call", "reason_code"], "an agreeing call is not an override"),
    (LM, "answered call queued", [("        if call.answered:\n            continue", "        if False:\n            continue")],
     ["missed_call", "queue_holds"], "an answered call is not a task"),
    (LM, "follow up ignored", [("other.answered and other.started_at > call.started_at", "other.answered and True")],
     ["queue_holds"], "only a LATER answered call closes a missed one"),
    (LM, "converted lead chased", [("elif not all(by_id[l].converted for l in lead_ids):", "elif True:")],
     ["missed_call", "queue_holds"], "a converted lead needs no chasing"),
    (LM, "duplicate ids allowed", [("if len(set(ids)) != len(ids):", "if False:")],
     ["duplicate_lead_ids"], "two leads with one id would silently overwrite each other"),
]


def run_suite(timeout: int = MUTANT_TIMEOUT_SECONDS):
    """Returns (all_green, failed test names), or (None, []) when the run hung."""
    for stale in list((ROOT / "app" / "connectors" / "__pycache__").glob("salesforce.*.pyc")) + list((ROOT / "app" / "__pycache__").glob("lead_match.*.pyc")):
        stale.unlink(missing_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        r = subprocess.run([str(PY), "-B", "-m", "pytest", *SUITE, "-q", "--tb=no", "-rf", "-p", "no:cacheprovider", "-x"],
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, []
    failed = [line.split(" ", 1)[1] for line in r.stdout.splitlines() if line.startswith("FAILED ")]
    return r.returncode == 0, failed


def run_suite_all_failures(timeout: int = MUTANT_TIMEOUT_SECONDS):
    """Like run_suite but without -x, so every failing test is named and the expected one can be found among them."""
    for stale in list((ROOT / "app" / "connectors" / "__pycache__").glob("salesforce.*.pyc")) + list((ROOT / "app" / "__pycache__").glob("lead_match.*.pyc")):
        stale.unlink(missing_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        r = subprocess.run([str(PY), "-B", "-m", "pytest", *SUITE, "-q", "--tb=no", "-rf", "-p", "no:cacheprovider"],
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, []
    failed = [line.split(" ", 1)[1] for line in r.stdout.splitlines() if line.startswith("FAILED ")]
    return r.returncode == 0, failed


def missing_targets() -> list[str]:
    """Guards whose old text is gone from its file, or that change nothing. Such a guard proves nothing."""
    texts = {SF: SF.read_text(encoding="utf-8").replace("\r\n", "\n"), LM: LM.read_text(encoding="utf-8").replace("\r\n", "\n")}
    problems = []
    for target, name, edits, *_ in MUTATIONS:
        for old, new in edits:
            if texts[target].count(old) != 1:
                problems.append(f"{name}: its target text appears {texts[target].count(old)} times in {target.name}, it must appear once")
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
    originals = {SF: SF.read_bytes(), LM: LM.read_bytes()}

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
        ran = 0
        for target, name, edits, expected, why in MUTATIONS:
            if match and not any(word in name for word in match):
                continue
            ran += 1
            original = originals[target]
            crlf = b"\r\n" in original
            text = original.decode("utf-8").replace("\r\n", "\n")
            mutated = text
            for old, new in edits:
                mutated = mutated.replace(old, new, 1)
            assert mutated != text, f"{name}: the mutation changed nothing, so a green result would mean nothing"
            if crlf:
                mutated = mutated.replace("\n", "\r\n")
            try:
                target.write_bytes(mutated.encode("utf-8"))
                green, failed = run_suite_all_failures()
            finally:
                target.write_bytes(original)
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
        for target, original in originals.items():
            if target.read_bytes() != original:
                target.write_bytes(original)


if __name__ == "__main__":
    sys.exit(main())
