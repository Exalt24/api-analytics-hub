# LIFESPAN: permanent (the repo's own mutation check for the Harvest and Microsoft Graph connectors, run by hand and in CI with --check-targets)
"""Mutation control for app/connectors/harvest.py and app/connectors/msgraph.py: break each guard one at a time and prove the tests notice.

It follows tests/mutation_check_salesforce.py. It needs no database and no Docker, runs only the connector's own test file, and demands more than "the suite went red": each
mutant names the test that must be among the failures, so a guard cannot be "covered" by an unrelated test that happens to break.

THIS EDITS THE TWO SOURCE FILES IN PLACE. It writes each mutation into the real file, runs the tests, and writes the original bytes back. Run it in a COPY of the
repository, never in a tree a sync might commit: a killed process can leave a weakened guard in the source, and `git diff` is how to check. A normal exit, Ctrl-C and
SIGTERM all restore the files first.

    python tests/mutation_check_harvest_graph.py --check-targets      only confirm every guard finds its text
    python tests/mutation_check_harvest_graph.py --match range,token  only the guards whose name holds a word
    python tests/mutation_check_harvest_graph.py                      every guard
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HV = ROOT / "app" / "connectors" / "harvest.py"
MG = ROOT / "app" / "connectors" / "msgraph.py"
SUITES = {HV: "tests/test_harvest_connector.py", MG: "tests/test_msgraph_connector.py"}
BASELINE_TIMEOUT_SECONDS = 240
MUTANT_TIMEOUT_SECONDS = 180


def venv_python() -> Path:
    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidate = ROOT.joinpath(".venv", *rel)
    return candidate if candidate.exists() else Path(sys.executable)


PY = venv_python()

# (file, name, [(old, new), ...], test-name fragments of which at least one must fail, why it matters)
MUTATIONS = [
    # ------------------------------------------------------------------ Harvest: the request
    (HV, "to date exclusive", [('"to": (end - timedelta(days=1)).isoformat()', '"to": end.isoformat()')],
     ["three_headers", "seeded_worlds"], "the window is half open and Harvest's `to` is inclusive"),
    (HV, "bearer word dropped", [('"Authorization": "Bearer " + self._token.reveal()', '"Authorization": self._token.reveal()')],
     ["three_headers", "refused_token"], "the token is how Harvest knows the caller"),
    (HV, "account header dropped", [('"Harvest-Account-Id": self._account,', '"X-Account": self._account,')],
     ["three_headers", "wrong_account"], "a token can reach several accounts, the header says which"),
    (HV, "user agent dropped", [('"User-Agent": self._user_agent,', '"X-Agent": self._user_agent,')],
     ["three_headers"], "Harvest answers 400 to a request that does not say who is calling"),
    (HV, "page size not asked for", [('"per_page": str(PER_PAGE)}, "time_entries")', '"x": "1"}, "time_entries")')],
     ["three_headers"], "the page size is how a sync stays inside the call limit"),
    (HV, "calls undercounted", [("api_calls=self._spent[0], throttle_waits", "api_calls=0, throttle_waits")],
     ["api_calls_counts"], "the run record must count every request"),
    # ------------------------------------------------------------------ Harvest: the arithmetic
    (HV, "hours not in seconds", [("hours * SECONDS_PER_HOUR", "hours * 60")],
     ["four_counts", "seeded_worlds"], "a figure in the wrong unit charts without complaint"),
    (HV, "billable includes all", [('if r["billable"]:\n                billable_seconds', "if True:\n                billable_seconds")],
     ["four_counts", "seeded_worlds"], "unbilled time is not billable time"),
    (HV, "hours instead of rounded", [('to_decimal(r.get("rounded_hours"), "the rounded_hours of time entry %d" % r["id"])', 'to_decimal(r.get("hours"), "the hours of time entry %d" % r["id"])')],
     ["four_counts", "seeded_worlds"], "invoices use the rounded hours"),
    (HV, "other currency added in", [("if currencies[pid] != self.currency:", "if False:")],
     ["another_currency"], "euros added to dollars is the defect that charts without complaint"),
    (HV, "unknown project allowed", [("if pid not in currencies:", "if False:")],
     ["list_does_not_hold"], "an entry on a project nobody listed has no known currency"),
    (HV, "no degrade on a 403", [("except ProtectedDataDenied:\n            currencies = None", "except ZeroDivisionError:\n            currencies = None")],
     ["may_not_read_projects"], "a key that cannot read projects still gets its hour counts"),
    # ------------------------------------------------------------------ Harvest: paging
    (HV, "next host not checked", [('"%s://%s" % (parsed.scheme, parsed.netloc) != self._base or not parsed.path.startswith("/v2/")', 'not parsed.path.startswith("/v2/")')],
     ["another_host", "not_this_host"], "the token must never be sent to a host the answer names"),
    (HV, "next path not checked", [('or not parsed.path.startswith("/v2/") or ".." in parsed.path:', 'or ".." in parsed.path:')],
     ["not_this_host"], "the token must only go to the v2 API"),
    (HV, "next dotdot not checked", [('or not parsed.path.startswith("/v2/") or ".." in parsed.path:', 'or not parsed.path.startswith("/v2/"):')],
     ["not_this_host"], "a path that climbs out of /v2/ is another endpoint"),
    (HV, "loop not noticed", [('if nxt in seen:\n                raise UpstreamBroken("a next page names itself")', 'if False:\n                raise UpstreamBroken("a next page names itself")')],
     ["names_itself"], "a next page that names itself would loop"),
    (HV, "total not checked", [("if total is not None and (", "if False and (")],
     ["total_that_does_not_match"], "a page lost on the way must not chart as fewer hours"),
    (HV, "duplicate entry allowed", [('if ident in ids:\n                raise UpstreamBroken("time entry %d appears twice" % ident)', 'if False:\n                raise UpstreamBroken("time entry %d appears twice" % ident)')],
     ["malformed_entry"], "the same entry twice doubles a day"),
    (HV, "row outside the window accepted", [("if not start <= day < end:", "if False:")],
     ["malformed_entry", "seeded_worlds"], "a day the query did not ask for must not be stored"),
    (HV, "hours bound removed", [("if hours < 0 or hours > MAX_ENTRY_HOURS:", "if False:")],
     ["malformed_entry"], "an entry above 24 hours is a broken answer, not a long day"),
    (HV, "billable type not checked", [('if not isinstance(r.get("billable"), bool):', "if False:")],
     ["malformed_entry"], "a billable that is text would count everything"),
    # ------------------------------------------------------------------ Harvest: limits and errors
    (HV, "pacing off", [("if len(self._sent) >= RATE_LIMIT_REQUESTS:", "if False:")],
     ["101st"], "the connector must leave the vendor's limit alone"),
    (HV, "pacing window never ends", [("self._sent = [t for t in self._sent if now - t < RATE_LIMIT_SECONDS]", "self._sent = list(self._sent)")],
     ["101st"], "after 15 seconds the next request goes"),
    (HV, "401 not expired", [('if http == 401:\n            raise AuthExpired("the access token was refused (HTTP 401)")', 'if False:\n            raise AuthExpired("the access token was refused (HTTP 401)")')],
     ["refused_token"], "a 401 is the session ending"),
    (HV, "403 not no-access", [('raise ProtectedDataDenied("this user may not read that (HTTP 403)")', 'raise UpstreamBroken("this user may not read that (HTTP 403)")')],
     ["wrong_account", "may_not_read_projects", "projects_for_a_key"], "a 403 is a person's permission"),
    (HV, "429 not retried", [('if http == 429:\n            return Retry(retry_after(response), "HTTP 429")', 'if False:\n            return Retry(retry_after(response), "HTTP 429")')],
     ["short_retry_after", "longer_than_the_cap"], "a 429 says wait, not stop"),
    (HV, "5xx not retried", [("if http in (500, 502, 503, 504):", "if False:")],
     ["server_error"], "a 503 is the vendor saying try again"),
    # ------------------------------------------------------------------ Harvest: budgets and inputs
    (HV, "equal to budget is over", [('status = "over" if spent > p.budget_hours else', 'status = "over" if spent >= p.budget_hours else')],
     ["budget_status_has_exact_edges"], "exactly on the budget has nothing left but nothing over"),
    (HV, "warn edge exclusive", [('"warn" if percent >= warn_percent', '"warn" if percent > warn_percent')],
     ["budget_status_has_exact_edges"], "80 percent is the warning, not 81"),
    (HV, "percent off by one", [("percent = int((spent * 100) // p.budget_hours)", "percent = int((spent * 100) // p.budget_hours) + 1")],
     ["budget_status_has_exact_edges"], "a project at 79 percent is not at 80"),
    (HV, "monthly budget compared anyway", [("if p.budget_is_monthly:", "if False:")],
     ["monthly_one"], "a budget that resets every month is not comparable with hours over another span"),
    (HV, "zero budget is under", [("elif p.budget_hours is None or p.budget_hours <= 0:", "elif p.budget_hours is None:")],
     ["monthly_one", "no_hours_budget"], "a project with no hours budget is never called under"),
    (HV, "warn percent unchecked", [("if isinstance(warn_percent, bool) or not isinstance(warn_percent, int) or not 1 <= warn_percent <= 100:", "if False:")],
     ["warn_percent_that_is_not"], "a threshold of 0 or 500 percent is a typo"),
    (HV, "cursor ignored", [('if cursor:\n            raise ValueError("the Harvest connector', 'if False:\n            raise ValueError("the Harvest connector')],
     ["cursor_and_an_empty_window"], "a cursor this connector never issued must not be silently dropped"),
    (HV, "empty window allowed", [('if window_end <= window_start:\n            raise ValueError("the window must hold at least one day")\n        if cursor:',
                                   'if window_end < window_start:\n            raise ValueError("the window must hold at least one day")\n        if cursor:')],
     ["cursor_and_an_empty_window"], "a window of no days is a caller bug"),
    (HV, "currency not checked", [("if not _CURRENCY_RE.match(str(currency)):", "if False:")],
     ["bad_options"], "money without an ISO currency is refused downstream"),
    (HV, "contact not checked", [("if not isinstance(contact, str) or not _CONTACT_RE.match(contact):", "if False:")],
     ["bad_options"], "Harvest asks every request to say who is calling"),
    (HV, "base scheme not checked", [('if parsed.scheme != "https" and not (loopback and parsed.scheme == "http"):\n        raise ValueError("base_url must use https")', 'if False:\n        raise ValueError("base_url must use https")')],
     ["bare_https_origin"], "a token sent over plain http leaves in the clear"),
    # ------------------------------------------------------------------ Microsoft Graph: the sign-in
    (MG, "wrong scope", [('"scope": SCOPE,', '"scope": "openid",')],
     ["exchanges_the_secret"], "the .default scope is how the app's own permissions are asked for"),
    (MG, "secrets in the query", [('data={"client_id": self._id,', 'params={"client_id": self._id,')],
     ["exchanges_the_secret"], "a secret in a URL ends up in logs"),
    (MG, "no early renewal", [("self._clock() < self._expires_at - TOKEN_SKEW_SECONDS", "self._clock() < self._expires_at + TOKEN_SKEW_SECONDS")],
     ["renewed_a_minute_before"], "a token that dies in flight fails a request that was fine"),
    (MG, "never renewed", [("self._clock() < self._expires_at - TOKEN_SKEW_SECONDS", "True")],
     ["renewed_a_minute_before", "expired_session"], "an expired token must be replaced"),
    (MG, "no renewal on a 401", [("response, c, w = await self._retrier.run(send, self._classify)\n            except AuthExpired:\n                if attempt == 0:",
                                  "response, c, w = await self._retrier.run(send, self._classify)\n            except AuthExpired:\n                if False:")],
     ["expired_session", "refused_again"], "an expired session must be renewed, not reported as a failure"),
    (MG, "renewal repeats forever", [("response, c, w = await self._retrier.run(send, self._classify)\n            except AuthExpired:\n                if attempt == 0:",
                                      "response, c, w = await self._retrier.run(send, self._classify)\n            except AuthExpired:\n                if True:")],
     ["refused_again"], "a token refused twice is a person's problem and must be reported"),
    (MG, "stale token check removed", [("if self._access is not None and self._access != rejected:", "if False:")],
     ["many_callers"], "twelve callers must not mint twelve tokens"),
    (MG, "token type not checked", [('if body.get("token_type") != "Bearer":', "if False:")],
     ["breaks_the_documented_shape"], "a token that is not a bearer token is sent wrong"),
    (MG, "expires_in not checked", [("if isinstance(expires, bool) or not isinstance(expires, int) or expires <= 0:", "if False:")],
     ["breaks_the_documented_shape"], "without a lifetime the renewal point is unknown"),
    (MG, "refused client retried", [('if code in ("invalid_client", "unauthorized_client", "invalid_scope", "invalid_request", "unsupported_grant_type"):', "if False:")],
     ["refused_app_registration"], "retrying a refused app registration burns quota while looking like progress"),
    # ------------------------------------------------------------------ Microsoft Graph: the calls
    (MG, "next host not checked", [('"%s://%s" % (parsed.scheme, parsed.netloc) != self._base or not parsed.path.startswith("/v1.0/")', 'not parsed.path.startswith("/v1.0/")')],
     ["another_host"], "the token must never be sent to a host the answer names"),
    (MG, "next path not checked", [('or not parsed.path.startswith("/v1.0/") or ".." in parsed.path:', 'or ".." in parsed.path:')],
     ["another_host"], "the token must only go to /v1.0/"),
    (MG, "next dotdot not checked", [('or not parsed.path.startswith("/v1.0/") or ".." in parsed.path:', 'or not parsed.path.startswith("/v1.0/"):')],
     ["another_host"], "a path that climbs out of /v1.0/ is another endpoint"),
    (MG, "loop not noticed", [('if nxt in seen:\n                raise UpstreamBroken("a next page names itself")', 'if False:\n                raise UpstreamBroken("a next page names itself")')],
     ["names_itself"], "a next page that names itself would loop"),
    (MG, "path outside v1.0 allowed", [('if not url.startswith(self._base + "/v1.0/"):', "if False:")],
     ["outside_v1_0"], "the token must only go to the versioned API"),
    (MG, "fields not expanded", [('{"expand": "fields"}', "{}")],
     ["ask_for_fields"], "without expand=fields the rows come back with no register columns"),
    (MG, "channel page size not asked for", [('{"$top": str(TEAMS_PAGE)}', "{}")],
     ["ask_for_fields"], "the default page of 20 would triple the calls"),
    (MG, "verify follows every page", [('body, _, _ = await self._get(self._items_path(), {"$top": "1"})',
                                        'body = {"value": (await self._values(self._items_path(), {"$top": "1"}))[0]}')],
     ["verify_reads_one_item"], "a login check must be one cheap call"),
    (MG, "calls undercounted", [("api_calls=self._spent[0] + self._tokens.mint_calls", "api_calls=self._spent[0]")],
     ["api_calls_counts_the_token"], "the run record must count every request"),
    # ------------------------------------------------------------------ Microsoft Graph: errors
    (MG, "401 not expired", [('if http == 401:\n            raise AuthExpired("the access token was refused (%s)" % text)', 'if False:\n            raise AuthExpired("the access token was refused (%s)" % text)')],
     ["expired_session", "refused_again"], "a 401 is the session ending"),
    (MG, "403 not no-access", [('raise AuthRevoked("the app is not allowed to read that, an administrator must grant the permission (%s)" % text)',
                                'raise UpstreamBroken("the app is not allowed to read that, an administrator must grant the permission (%s)" % text)')],
     ["without_the_permission", "workbook_endpoints"], "a 403 needs an administrator, not a retry"),
    (MG, "429 and 5xx not retried", [("if http == 429 or http in (500, 502, 503, 504):", "if False:")],
     ["short_retry_after", "server_error"], "a 429 says wait, not stop"),
    # ------------------------------------------------------------------ Microsoft Graph: the data
    (MG, "modified counted as created", [("day = item.modified.date()", "day = item.created.date()")],
     ["three_counts_per_day", "seeded_worlds"], "created and modified are different days"),
    (MG, "system messages counted", [('if r.get("messageType") != "message" or r.get("deletedDateTime") is not None:', "if False:")],
     ["three_counts_per_day", "seeded_worlds"], "a joined-the-channel notice is not a message"),
    (MG, "messages outside the window counted", [("if not window_start <= created.date() < window_end:\n                continue", "if False:\n                continue")],
     ["three_counts_per_day", "seeded_worlds", "typed_reads"], "a day the window did not ask for must not be counted"),
    (MG, "modified before created accepted", [("if modified < created:", "if False:")],
     ["malformed_list_item"], "a row changed before it existed is a broken answer"),
    (MG, "duplicate row allowed", [('if ident in ids:\n                raise UpstreamBroken("list item %s appears twice" % ident)', 'if False:\n                raise UpstreamBroken("list item %s appears twice" % ident)')],
     ["malformed_list_item"], "the same row twice doubles a count"),
    (MG, "duplicate message allowed", [('if ident in ids:\n                raise UpstreamBroken("message %s appears twice" % ident)', 'if False:\n                raise UpstreamBroken("message %s appears twice" % ident)')],
     ["no_body_or_a_repeated_id"], "the same message twice doubles a count"),
    (MG, "message body not checked", [('if not isinstance(body, dict) or not isinstance(body.get("content"), str) or not isinstance(body.get("contentType"), str):', "if False:")],
     ["no_body_or_a_repeated_id"], "a message with no body would reach a reader as nothing"),
    (MG, "datetime kept in its own zone", [("return local.astimezone(timezone.utc)", "return local")],
     ["utc_instant", "other_zone", "seeded_worlds"], "01:30 in Manila is the evening before in UTC"),
    (MG, "fraction not read", [('int((m.group(7) or "0").ljust(6, "0")[:6])', 'int((m.group(7) or "0"))')],
     ["utc_instant"], "395 milliseconds are not 395 microseconds"),
    (MG, "team count without a team", [("if self._team is not None:\n            messages = {}", "if True:\n            messages = {}")],
     ["without_a_team"], "a connection with no channel must not ask for one"),
    # ------------------------------------------------------------------ Microsoft Graph: the workbook
    (MG, "token sent to the download address", [('headers={"Accept": "*/*"}, follow_redirects=False)', 'headers={"Accept": "*/*", "Authorization": "Bearer " + token}, follow_redirects=False)')],
     ["never_to_the_download_address"], "a pre-authenticated address needs no token and must not get one"),
    (MG, "size cap removed", [("if len(data) > MAX_WORKBOOK_BYTES:", "if False:")],
     ["not_a_workbook_or_is_too_large"], "one huge file must not fill the worker's memory"),
    (MG, "zip header not checked", [('if not data.startswith(b"PK"):', "if False:")],
     ["not_a_workbook_or_is_too_large"], "a PDF is not a register"),
    (MG, "location scheme not checked", [('(target.scheme != "https" and not (loopback and target.scheme == "http"))', "False")],
     ["no_https_location"], "a download address on plain http or ftp is not the service"),
    (MG, "repeated header allowed", [("if len(set(names)) != len(names):", "if False:")],
     ["header_row_that_would_lose"], "a dict would silently drop a column"),
    (MG, "blank header allowed", [("if h is None or (isinstance(h, str) and not h.strip()):", "if False:")],
     ["header_row_that_would_lose"], "a column with no name cannot be read"),
    (MG, "blank rows kept", [("if all(v is None or (isinstance(v, str) and not v.strip()) for v in row):\n            continue", "if False:\n            continue")],
     ["downloaded_and_read"], "a blank line in a register is not a job"),
    (MG, "row numbers lost", [("out.append(WorkbookRow(first_row + offset, values))", "out.append(WorkbookRow(offset, values))")],
     ["downloaded_and_read"], "a person has to find the row in the sheet"),
    (MG, "sheet and table together", [("if sheet is not None and table is not None:", "if False:")],
     ["together_are_refused"], "two selectors would silently choose one"),
    (MG, "drive id not checked", [('if not isinstance(value, str) or not _ID_RE.match(value):\n                raise ValueError("%s is not a usable Graph id" % name)\n        if sheet is not None',
                                   'if False:\n                raise ValueError("%s is not a usable Graph id" % name)\n        if sheet is not None')],
     ["not_an_id_is_refused"], "an id goes into a URL path"),
    # ------------------------------------------------------------------ Microsoft Graph: inputs
    (MG, "cursor ignored", [('if cursor:\n            raise ValueError("the Microsoft Graph connector', 'if False:\n            raise ValueError("the Microsoft Graph connector')],
     ["cursor_and_an_empty_window"], "a cursor this connector never issued must not be silently dropped"),
    (MG, "team and channel unpaired", [("if (team_id is None) != (channel_id is None):", "if False:")],
     ["bad_options"], "a team with no channel is a half configured connection"),
    (MG, "tenant not checked", [("if not _TENANT_RE.match(str(tenant_id)):", "if False:")],
     ["bad_options"], "the tenant goes into a URL path"),
    (MG, "graph base scheme not checked", [('if parsed.scheme != "https" and not (loopback and parsed.scheme == "http"):\n        raise ValueError("%s must use https" % what)', 'if False:\n        raise ValueError("%s must use https" % what)')],
     ["bad_options"], "a token sent over plain http leaves in the clear"),
]


def _clean_bytecode() -> None:
    for stale in list((ROOT / "app" / "connectors" / "__pycache__").glob("harvest.*.pyc")) + list((ROOT / "app" / "connectors" / "__pycache__").glob("msgraph.*.pyc")):
        stale.unlink(missing_ok=True)


def run_suite(target: Path, timeout: int = MUTANT_TIMEOUT_SECONDS, stop_first: bool = True):
    """Returns (all_green, failed test names), or (None, []) when the run hung."""
    _clean_bytecode()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    cmd = [str(PY), "-B", "-m", "pytest", SUITES[target], "-q", "--tb=no", "-rf", "-p", "no:cacheprovider"] + (["-x"] if stop_first else [])
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, []
    failed = [line.split(" ", 1)[1] for line in r.stdout.splitlines() if line.startswith("FAILED ")]
    return r.returncode == 0, failed


def missing_targets() -> list[str]:
    """Guards whose old text is gone from its file, or that change nothing. Such a guard proves nothing."""
    texts = {HV: HV.read_text(encoding="utf-8").replace("\r\n", "\n"), MG: MG.read_text(encoding="utf-8").replace("\r\n", "\n")}
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
    originals = {HV: HV.read_bytes(), MG: MG.read_bytes()}

    def _terminate(signum, frame):
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except ValueError:
        pass

    killed = survived = wrong_test = 0
    try:
        for target in SUITES:
            green, _ = run_suite(target, BASELINE_TIMEOUT_SECONDS)
            if green is not True:
                print(f"baseline suite for {target.name} is RED or hung, fix that before mutating")
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
                green, failed = run_suite(target, stop_first=False)
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
        for target in SUITES:
            green, _ = run_suite(target, BASELINE_TIMEOUT_SECONDS)
            if green is not True:
                print("suite is RED or hung after restoring, a mutation leaked")
                return 1
        print("source restored, suites green again")
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
