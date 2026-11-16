# LIFESPAN: permanent (the repo's own mutation check for the e-signature slice, run by hand and its --check-targets in CI)
"""Mutation control for the e-signature slice: break each guard one at a time and prove the tests notice.

It follows tests/mutation_check_gbp.py and demands more than "the suite went red": each mutant names the tests that must be among the failures, so a guard cannot be
"covered" by an unrelated test that happens to break. Each mutant runs only the tests it names (a `-k` selection over the e-signature test files), the baseline and the
final restore run all of them. It needs the compose Postgres (the database suites drop and recreate the `public` schema of the database they point at).

THIS EDITS SOURCE FILES IN PLACE (app/esign/*.py, app/api/esign.py, app/api/auth.py, migrations/016_esign.sql). It writes each mutation into the real file, runs the
tests, and writes the original bytes back. Run it in a COPY of the repository, never in a tree a sync might commit: a killed process can leave a weakened guard in the
source, and `git diff` is how to check. A normal exit, Ctrl-C and SIGTERM all restore every file first.

    python tests/mutation_check_esign.py --check-targets      only confirm every guard finds its text
    python tests/mutation_check_esign.py --match lock,expiry  only the guards whose name holds a word
    python tests/mutation_check_esign.py                      every guard
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = ["tests/test_esign_chain.py", "tests/test_esign_db.py", "tests/test_esign_api_db.py", "tests/test_esign_simulation.py"]
BASELINE_TIMEOUT_SECONDS = 900
MUTANT_TIMEOUT_SECONDS = 300

TARGETS = {
    "service": ROOT / "app" / "esign" / "service.py",
    "chain": ROOT / "app" / "esign" / "chain.py",
    "consent": ROOT / "app" / "esign" / "consent.py",
    "certificate": ROOT / "app" / "esign" / "certificate.py",
    "routes": ROOT / "app" / "api" / "esign.py",
    "auth": ROOT / "app" / "api" / "auth.py",
    "m016": ROOT / "migrations" / "016_esign.sql",
}


def venv_python() -> Path:
    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidate = ROOT.joinpath(".venv", *rel)
    return candidate if candidate.exists() else Path(sys.executable)


PY = venv_python()

SIGN_BLOCK = '        if req["status"] == "expired":\n            raise RequestExpired("this request has expired")\n        expired = await _expire_if_due(conn, req, now)\n        if not expired:\n            if consent is not True:'

# (name, target, old text, new text, test-name fragments of which at least one must fail, why it matters)
MUTATIONS = [
    # -- one signature for each request, and the retry
    ("double-sign: a different attempt after a success is answered as a retry",
     "service", '                return _result(existing, True)\n            raise AlreadySigned("this request is already signed")',
     '                return _result(existing, True)\n            return _result(existing, True)',
     ["second_attempt_with_a_different_name"], "a second signing with other content must be refused, not answered with the first"),
    ("retry: a retry after a lost answer is refused",
     "service", "                return _result(existing, True)\n", "                pass\n",
     ["retry_after_a_lost_answer", "retry_after_a_lost_answer_is_200", "concurrent_attempts"], "a client that lost the answer must get the first result back"),
    ("retry: the name is not compared, so another name gets the first result",
     "service", ' and names_match(typed_name, existing["signer_name_typed"]):', ":",
     ["second_attempt_with_a_different_name", "another_name_is_409"], "a retry means the same content, not any content"),
    ("double-sign: the request is not locked, so two signers race",
     "service", '    await conn.execute("select pg_advisory_xact_lock(hashtextextended($1, 0))", "esign:" + request_id)', "    return None",
     ["concurrent_attempts_make_exactly_one_signature", "simulation_world"], "two attempts at the same moment must end in one signature and one result"),
    ("double-sign: the database's unique key on the request is gone",
     "m016", "    unique (request_id),\n", "", ["second_signature_row_for_one_request"], "the database must refuse a second signature even if the code did not"),
    # -- expiry, decline
    ("expiry: a request past its expiry is signable",
     "service", "        expired = await _expire_if_due(conn, req, now)\n        if not expired:\n            if consent is not True:",
     "        expired = False\n        if not expired:\n            if consent is not True:", ["expired_request_cannot_be_signed", "expiry_boundary"], "an expired request must not be signed"),
    ("expiry: the boundary instant counts as still valid",
     "service", 'if req["status"] != "pending" or now < req["expires_at"]:', 'if req["status"] != "pending" or now <= req["expires_at"]:',
     ["expiry_boundary"], "at the expiry instant the request is expired"),
    ("expiry: an already expired request can be signed again",
     "service", SIGN_BLOCK, SIGN_BLOCK.split("\n", 2)[2], ["expired_request_cannot_be_signed"], "the expired status must be refused, not only the clock"),
    ("expiry: sweeping expires requests that are not yet due",
     "service", "status = 'pending' and expires_at <= $1", "status = 'pending'", ["expire_due_marks_only"], "only past-due requests may be expired by the sweep"),
    ("decline: a declined request can be signed",
     "service", '        if req["status"] == "declined":\n            raise RequestDeclined("this request was declined")\n', "", ["declined_request_cannot_be_signed"], "a declined request must not be signed"),
    ("decline: a signed request can be declined",
     "service", '        if req["status"] == "signed":\n            raise AlreadySigned("this request is already signed")\n        if req["status"] == "declined":\n            return "declined"',
     '        if req["status"] == "declined":\n            return "declined"', ["declining_twice_changes_nothing"], "a signature cannot be undone by declining"),
    # -- consent, name, document
    ("consent: the box is not checked",
     "service", '            if consent is not True:\n                raise ConsentRequired("tick the consent box to sign")', '            if False:\n                raise ConsentRequired("tick the consent box to sign")',
     ["no_consent_means_no_signature", "signing_refusal_has_its_own_status"], "no consent, no signature"),
    ("consent: any truthy value counts",
     "service", "            if consent is not True:", "            if not consent:", ["no_consent_means_no_signature"], "only a real True is a tick"),
    ("consent: the version is not compared",
     "service", '            if consent_version != req["consent_version"]:', "            if False:", ["consent_version_other_than_the_requests"], "the signer must have seen this request's text"),
    ("name: an empty name is not named as such",
     "service", '            if not typed_name.strip():\n                raise NameRequired("type your full name to sign")', '            if False:\n                raise NameRequired("type your full name to sign")',
     ["empty_or_mismatched_typed_name"], "an empty name is its own refusal"),
    ("name: the match is not checked",
     "service", 'if len(typed_name) > MAX_TYPED_NAME or not names_match(typed_name, req["signer_name"]):', "if len(typed_name) > MAX_TYPED_NAME:",
     ["empty_or_mismatched_typed_name", "each_signing_refusal"], "the typed name must be the signer's"),
    ("name: an over-long name is accepted",
     "service", "if len(typed_name) > MAX_TYPED_NAME or not", "if not", ["empty_or_mismatched_typed_name"], "padding must not get a name through"),
    ("name rule: case matters",
     "consent", '.split()).casefold()', ".split())", ["name_rule_accepts"], "upper and lower case do not matter"),
    ("name rule: compatibility forms are not normalised",
     "consent", 'unicodedata.normalize("NFKC", value).split()', "value.split()", ["name_rule_accepts"], "a full width letter is the same letter"),
    ("name rule: whitespace is not collapsed",
     "consent", '" ".join(unicodedata.normalize("NFKC", value).split())', 'unicodedata.normalize("NFKC", value).strip()', ["name_rule_accepts"], "extra spaces do not matter"),
    ("name rule: two empty names match",
     "consent", "    return bool(a) and a == normalise_name(signer)", "    return a == normalise_name(signer)", ["not_match_two_empty_names"], "an empty name never signs"),
    ("document: the stored bytes are not rehashed at signing",
     "service", 'if sha256_hex(bytes(req["document"])) != req["document_sha256"] or seen != req["document_sha256"]:', 'if seen != req["document_sha256"]:',
     ["replaced_document_is_refused_at_signing"], "a replaced document must be refused"),
    ("document: the hash the signer saw is not compared",
     "service", 'if sha256_hex(bytes(req["document"])) != req["document_sha256"] or seen != req["document_sha256"]:', 'if sha256_hex(bytes(req["document"])) != req["document_sha256"]:',
     ["signer_who_saw_a_different_hash", "replaced_document_is_refused_at_signing"], "the signer signs what they saw"),
    ("document: the download is not rehashed",
     "service", '        if sha256_hex(data) != req["document_sha256"]:', "        if False:", ["replaced_document_is_not_handed_out"], "a replaced document must not be handed out"),
    # -- tenants
    ("tenant: the event policy lets every tenant read",
     "m016", "create policy esign_event_by_tenant on esign_event\n    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)",
     "create policy esign_event_by_tenant on esign_event\n    using (true)", ["tenant_a_sees_none"], "one tenant must not read another's audit rows"),
    ("tenant: row level security is not forced on the requests",
     "m016", "alter table esign_request   force row level security;", "select 1;", ["row_level_security_enabled_forced"], "the owner must be filtered too"),
    # -- append only
    ("append only: audit rows can be changed by the owner",
     "m016", "create trigger esign_event_append_only before update or delete on esign_event for each row execute function esign_append_only();", "select 1;",
     ["even_the_owner_cannot_update"], "an audit row is never edited"),
    ("append only: the request role can edit and delete the log",
     "m016", "grant select, insert on esign_request, esign_signature, esign_event to app_request;",
     "grant select, insert, update, delete on esign_request, esign_signature, esign_event to app_request;", ["request_role_cannot_update_or_delete"], "the application role may only add rows"),
    ("append only: the request role can edit the document",
     "m016", "grant update (status) on esign_request to app_request;", "grant update on esign_request to app_request;", ["request_role_cannot_update_or_delete"], "only the status may change"),
    ("append only: the request is not frozen",
     "m016", "create trigger esign_request_frozen before update on esign_request for each row execute function esign_request_frozen();", "select 1;",
     ["even_the_owner_cannot_update"], "the document and signer never change"),
    ("append only: a status can go backward",
     "m016", "if new.status <> old.status and old.status <> 'pending' then", "if false then", ["status_moves_forward"], "signed stays signed"),
    # -- the chain and the checks
    ("chain: a new row does not link to the row before it",
     "service", '    prev = last["entry_hash"] if last else genesis_hash(tenant_id, request_id)', "    prev = genesis_hash(tenant_id, request_id)",
     ["untouched_record_verifies", "simulation_world"], "every row must carry the hash of the one before"),
    ("chain: a row's hash leaves out its detail",
     "chain", '"detail": detail, "prev_hash": prev_hash}\n    return sha256_hex(canonical(body)', '"prev_hash": prev_hash}\n    return sha256_hex(canonical(body)', ["event_hash_covers_every_field"], "an edited detail must change the hash"),
    ("chain: a signature hash leaves out the typed name",
     "chain", '"signer_name_typed": signer_name_typed, "consent_version": consent_version, "consent_text_sha256": consent_text_sha256,\n            "signed_at"',
     '"consent_version": consent_version, "consent_text_sha256": consent_text_sha256,\n            "signed_at"', ["signature_hash_covers_every_field"], "the name as typed is part of the signature"),
    ("verify: an edited row is not detected",
     "chain", "        if recomputed != e.entry_hash:", "        if False:", ["edited_row_is_reported", "each_kind_of_tampering"], "an edited audit row must show"),
    ("verify: a broken link is not detected",
     "chain", "        if e.prev_hash != prev:", "        if False:", ["recomputed_breaks_the_next_link", "deleted_row_is_reported"], "a deleted or forged row must show"),
    ("verify: a gap in the numbers is not detected",
     "chain", "        if e.seq > expected:", "        if False:", ["deleted_row_is_reported_as_missing", "first_row_is_reported"], "a deleted row must be named"),
    ("verify: reordered rows are not detected",
     "chain", "    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):", "    if False:", ["swapped_in_order_are_reported_as_reordered", "two_audit_rows_reordered"], "reordering must show"),
    ("verify: the document is not rehashed",
     "chain", '    if actual != request["document_sha256"]:', "    if False:", ["changed_document_byte_is_reported", "a_changed_byte_in_the_document"], "a changed byte must show"),
    ("verify: the created row is not compared with the request",
     "chain", "            if d.get(key) != value:", "            if False:", ["replaced_together_is_caught_by_the_created_row", "signer_or_expiry_or_consent_version"], "the request's own fields are pinned"),
    ("verify: the signature hash is not recomputed",
     "chain", '        if recomputed != sig["signature_hash"]:', "        if False:", ["edited_or_missing_signature_record_is_reported", "the_typed_name_edited"], "an edited signature must show"),
    ("verify: a deleted signature is not noticed",
     "chain", '    if signature is None and (status == "signed" or signed_rows):', "    if False:", ["edited_or_missing_signature_record_is_reported", "the_signature_deleted"], "a missing signature must show"),
    ("verify: a status that disagrees with the signature is not noticed",
     "chain", '        if status != "signed":', "        if False:", ["status_that_disagrees_with_the_audit_rows"], "the status must agree with the record"),
    ("verify: the certificate is issued without verifying",
     "service", "    if not report.ok:\n        raise CertificateRefused(report.problems)", "    if False:\n        raise CertificateRefused(report.problems)",
     ["no_certificate_for_an_unsigned_request_and_none_for_a_record"], "a record that does not verify gets no certificate"),
    ("verify: a verification run is not recorded as one",
     "service", '"verified", actor, {"ok": not problems', '"viewed", actor, {"ok": not problems', ["untouched_record_verifies_in_every_state"], "a verification run is an audit event"),
    ("audit: opening a request writes no viewed row",
     "service", '        await _append(conn, tenant_id, rid, "viewed", actor, {"what": "request", "role": role}, now)\n', "", ["opening_a_request_shows_the_hash"], "every view is audited"),
    # -- what is stored and logged
    ("privacy: the address hash is a plain SHA-256",
     "service", 'pii_hash(pii_key, "client-address", client_addr)', 'sha256_hex(client_addr.encode("utf-8"))', ["signing_records_every_field", "address_is_the_first_forwarded"], "a plain hash of an address is guessable"),
    ("privacy: the user agent is stored raw",
     "service", 'pii_hash(pii_key, "user-agent", user_agent)', "user_agent", ["signing_records_every_field", "the_whole_flow_over_http"], "the raw user agent is never stored"),
    ("privacy: the hash has no key",
     "chain", '    return hmac.new(key, (label + "\\x00" + value).encode("utf-8"), hashlib.sha256).hexdigest()', '    return hashlib.sha256((label + "\\x00" + value).encode("utf-8")).hexdigest()',
     ["personal_values_are_keyed_hashes"], "the key is what stops a guessed address"),
    ("privacy: the hash ignores what kind of value it is",
     "chain", '(label + "\\x00" + value).encode("utf-8"), hashlib.sha256)', '(value).encode("utf-8"), hashlib.sha256)', ["personal_values_are_keyed_hashes"], "an address and an agent must not share a hash"),
    ("privacy: the connection address is logged",
     "routes", '    return request.client.host if request.client else ""',
     '    log.info("address %s", request.client.host if request.client else "")\n    return request.client.host if request.client else ""',
     ["no_raw_agent_address_or_document_byte_reaches_a_log"], "no raw address in a log"),
    ("privacy: forwarded headers are always trusted",
     "routes", '    if os.environ.get("ESIGN_TRUST_FORWARDED") == "1":', "    if True:", ["address_is_the_first_forwarded_entry"], "a forwarded header is trusted only when told to"),
    # -- the certificate
    ("certificate: the chain head is not checked",
     "certificate", '    if chain.get("head") != last.entry_hash or chain.get("length") != last.seq:', "    if False:", ["edited_in_any_part_is_reported"], "the head is what a third party holds"),
    ("certificate: the document is not rehashed",
     "certificate", "    if document is not None and sha256_hex(document) != req[\"document_sha256\"]:", "    if False:", ["certificate_with_a_changed_document"], "the third party's document must match"),
    ("certificate: the consent text is not rehashed",
     "certificate", '    if sha256_hex(cert["consent_text"].encode("utf-8")) != sig["consent_text_sha256"]:', "    if False:", ["edited_in_any_part_is_reported"], "what was agreed to must be what is shown"),
    ("certificate: rows after the signed row are included",
     "service", "    upto = [e for e in events if e.seq <= signed[0].seq]", "    upto = events", ["certificate_of_a_signed_request_verifies_offline"], "the certificate ends at the signed row"),
    # -- input checks
    ("request: an expiry beyond a year is accepted",
     "service", " or expires_at - now > MAX_LIFETIME:", ":", ["cannot_be_stored_is_refused_and_leaves_nothing"], "the lifetime is bounded"),
    ("request: an expiry at this moment is accepted",
     "service", "if expires_at <= now or", "if expires_at < now or", ["cannot_be_stored_is_refused_and_leaves_nothing"], "an expiry must be in the future"),
    ("request: the size limit is not checked",
     "service", "    if len(document) > MAX_DOCUMENT_BYTES:", "    if False:", ["cannot_be_stored_is_refused_and_leaves_nothing"], "the document size is bounded"),
    ("request: the signer's address is not checked",
     "service", "    if not EMAIL_RE.match(signer_email) or len(signer_email) > 254:", "    if len(signer_email) > 254:", ["cannot_be_stored_is_refused_and_leaves_nothing"], "the signer's address must look like one"),
    # -- who may do what
    ("roles: a viewer may create requests",
     "auth", '    Role.VIEWER: frozenset({"read:kpis", "read:runs", "read:report", "read:esign", "sign:esign", "read:jobs"}),',
     '    Role.VIEWER: frozenset({"read:kpis", "read:runs", "read:report", "read:esign", "sign:esign", "read:jobs", "write:esign"}),', ["creating_needs_a_key_and_an_operator_or_admin"], "only a firm user creates a request"),
    ("status: a missing request is not 404",
     "routes", "RequestNotFound: 404,", "RequestNotFound: 403,", ["another_tenants_key_gets_the_same_404"], "a missing request is a 404, like someone else's"),
    ("status: an expired request is not 410",
     "routes", "RequestExpired: 410,", "RequestExpired: 409,", ["declined_request_is_409_to_sign_and_an_expired_one_is_410"], "expired is its own status"),
]


def run_suite(select: str | None = None, timeout: int = MUTANT_TIMEOUT_SECONDS):
    """Returns (all_green, failed test names), or (None, []) when the run hung. `select` is a -k expression."""
    for cache in (ROOT / "app" / "esign" / "__pycache__", ROOT / "app" / "api" / "__pycache__"):
        for stale in cache.glob("*.pyc"):
            stale.unlink(missing_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    cmd = [str(PY), "-B", "-m", "pytest", *SUITES, "-q", "--tb=no", "-rf", "-p", "no:cacheprovider"]
    if select:
        cmd += ["-k", select]
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, []
    failed = [line.split(" ", 1)[1] for line in r.stdout.splitlines() if line.startswith("FAILED ")]
    if select and r.returncode == 5:
        print("      no test matched %r" % select)
    return r.returncode == 0, failed


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def missing_targets() -> list[str]:
    """Guards whose old text is gone from their file or appears more than once, or that change nothing. Such a guard proves nothing."""
    texts = {key: read_text(path) for key, path in TARGETS.items()}
    problems = []
    for name, key, old, new, *_ in MUTATIONS:
        count = texts[key].count(old)
        if count != 1:
            problems.append(f"{name}: its target text appears {count} times in {TARGETS[key].name}, it must appear once")
        elif old == new:
            problems.append(f"{name}: the mutation changes nothing")
    names = [m[0] for m in MUTATIONS]
    problems += [f"{n}: the name is used twice" for n in sorted({n for n in names if names.count(n) > 1})]
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
    originals = {key: path.read_bytes() for key, path in TARGETS.items()}

    def restore() -> None:
        for key, path in TARGETS.items():
            if path.read_bytes() != originals[key]:
                path.write_bytes(originals[key])

    def _terminate(signum, frame):
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except ValueError:
        pass

    killed = survived = wrong_test = 0
    try:
        green, _ = run_suite(None, BASELINE_TIMEOUT_SECONDS)
        if green is not True:
            print("baseline suite is RED or hung, fix that before mutating")
            return 1
        print("baseline: green")
        ran = 0
        for name, key, old, new, expected, why in MUTATIONS:
            if match and not any(word in name for word in match):
                continue
            ran += 1
            original = originals[key]
            crlf = b"\r\n" in original
            text = original.decode("utf-8").replace("\r\n", "\n")
            mutated = text.replace(old, new, 1)
            assert mutated != text, f"{name}: the mutation changed nothing, so a green result would mean nothing"
            if crlf:
                mutated = mutated.replace("\n", "\r\n")
            select = " or ".join(expected)
            try:
                TARGETS[key].write_bytes(mutated.encode("utf-8"))
                green, failed = run_suite(select)
            finally:
                TARGETS[key].write_bytes(original)
            if green is None:
                print(f"[ok]   {name}: suite HUNG, so the guard also stops an endless loop")
                killed += 1
            elif green:
                print(f"[FAIL] {name}: the named tests stayed GREEN, so nothing tests that {why}")
                survived += 1
            elif any(frag in test for test in failed for frag in expected):
                print(f"[ok]   {name}: failed {len(failed)} test(s), including the expected one")
                killed += 1
            else:
                print(f"[FAIL] {name}: the suite went red but not in {expected}; failed: {failed[:4]}")
                wrong_test += 1

        print()
        green, _ = run_suite(None, BASELINE_TIMEOUT_SECONDS)
        if green is not True:
            print("suite is RED or hung after restoring, a mutation leaked")
            return 1
        print("source restored, suite green again")
        print(f"mutants run {ran}: killed {killed}, survived {survived}, killed by the wrong test {wrong_test}")
        bad = survived + wrong_test
        print(f"mutation control: {'PASS' if bad == 0 else str(bad) + ' UNTESTED GUARD(S)'}")
        return 1 if bad else 0
    finally:
        restore()


if __name__ == "__main__":
    sys.exit(main())
