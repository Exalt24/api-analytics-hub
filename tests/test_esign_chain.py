"""The e-signature rules that need no database: the consent text, the name rule, the hashes, the chain check, the record check and the offline certificate check.

Every tamper below is made on plain values and must be reported by code and by row number; every control is the untouched value that must pass, because a checker that
reported a problem for everything would pass every negative test.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.esign import certificate, chain, consent  # noqa: E402
from app.esign.chain import Event  # noqa: E402

T = datetime(2026, 11, 14, 12, 0, 0, 123456, tzinfo=timezone.utc)
TENANT, RID, SIG = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333"
DOC = b"%PDF-1.4 a small document"


def make_events(specs):
    """Rows chained from the genesis value. `specs` is a list of (kind, detail)."""
    out, prev = [], chain.genesis_hash(TENANT, RID)
    for i, (kind, detail) in enumerate(specs, start=1):
        at = T + timedelta(minutes=i)
        digest = chain.event_hash(tenant_id=TENANT, request_id=RID, seq=i, kind=kind, at=at, actor="key-1", detail=detail, prev_hash=prev)
        out.append(Event(i, kind, at, "key-1", detail, prev, digest))
        prev = digest
    return out


def make_world(*, signed=True):
    """A request, its audit rows and a signature, all consistent."""
    expires = T + timedelta(hours=72)
    request = {"id": RID, "tenant_id": TENANT, "document_sha256": chain.sha256_hex(DOC), "signer_name": "Dana Whitfield", "signer_email": "dana@example.com",
               "consent_version": consent.CURRENT_VERSION, "status": "pending", "expires_at": expires, "document_name": "letter.pdf", "document_bytes": len(DOC),
               "created_at": T}
    created = {"document_sha256": request["document_sha256"], "document_bytes": len(DOC), "signer_name_sha256": chain.sha256_hex(b"Dana Whitfield"),
               "signer_email_sha256": chain.sha256_hex(b"dana@example.com"), "consent_version": consent.CURRENT_VERSION, "expires_at": chain.fmt_time(expires)}
    specs = [("created", created), ("viewed", {"what": "request", "role": "viewer"})]
    if not signed:
        return request, make_events(specs), None
    ctext = consent.consent_text_sha256(consent.CURRENT_VERSION)
    specs.append(("consented", {"consent_version": consent.CURRENT_VERSION, "consent_text_sha256": ctext}))
    before = make_events(specs)
    signed_at = T + timedelta(minutes=4)
    sig = {"id": SIG, "tenant_id": TENANT, "request_id": RID, "document_sha256": request["document_sha256"], "signer_name_typed": "Dana Whitfield",
           "consent_version": consent.CURRENT_VERSION, "consent_text_sha256": ctext, "signed_at": signed_at, "user_agent_hash": "a" * 64, "client_addr_hash": "b" * 64,
           "prev_hash": before[-1].entry_hash}
    sig["signature_hash"] = chain.signature_hash(signature_id=SIG, tenant_id=TENANT, request_id=RID, document_sha256=sig["document_sha256"], signer_name_typed="Dana Whitfield",
                                                 consent_version=sig["consent_version"], consent_text_sha256=ctext, signed_at=signed_at, user_agent_hash="a" * 64,
                                                 client_addr_hash="b" * 64, prev_hash=sig["prev_hash"])
    specs.append(("signed", {"signature_id": SIG, "signature_hash": sig["signature_hash"], "signed_at": chain.fmt_time(signed_at), "document_sha256": sig["document_sha256"]}))
    request["status"] = "signed"
    return request, make_events(specs), sig


def codes(problems):
    return {p.code for p in problems}


# ---- the consent text ------------------------------------------------------------------------------------------------------------------

def test_the_consent_text_covers_consent_intent_association_and_retention():
    text = consent.consent_text(consent.CURRENT_VERSION).lower()
    for idea in ("electronic record", "paper copy", "intent to sign", "belongs to this record", "sha-256", "retention", "decline"):
        assert idea in text, idea


def test_an_unknown_consent_version_is_an_error_and_never_a_default():
    with pytest.raises(ValueError):
        consent.consent_text("1999-01-01.1")
    assert consent.consent_text_sha256(consent.CURRENT_VERSION) == chain.sha256_hex(consent.consent_text(consent.CURRENT_VERSION).encode("utf-8"))


# ---- the name rule ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("typed", ["Dana Whitfield", "dana whitfield", "DANA WHITFIELD", "  Dana   Whitfield  ", "Dana Whitfield", "Ｄana Whitfield", "Dana\tWhitfield"])
def test_name_rule_accepts_case_spacing_and_compatibility_forms(typed):
    assert consent.names_match(typed, "Dana Whitfield")


@pytest.mark.parametrize("typed", ["", "   ", "Dana", "Whitfield", "Dana M Whitfield", "Dana Whitfeld", "Whitfield Dana", "Dana Whitfield Jr", "Eve Mallory", "Dana-Whitfield"])
def test_name_rule_refuses_empty_partial_reordered_and_different_names(typed):
    assert not consent.names_match(typed, "Dana Whitfield")


def test_name_rule_does_not_match_two_empty_names():
    assert not consent.names_match("", "")


# ---- the hashes ------------------------------------------------------------------------------------------------------------------------

def test_a_time_is_hashed_in_utc_to_the_microsecond_whatever_zone_it_was_read_in():
    plus8 = T.astimezone(timezone(timedelta(hours=8)))
    assert chain.fmt_time(T) == chain.fmt_time(plus8) == "2026-11-14T12:00:00.123456Z"
    with pytest.raises(ValueError):
        chain.fmt_time(datetime(2026, 11, 14, 12, 0))


def test_canonical_json_is_the_same_for_any_key_order():
    assert chain.canonical({"b": 1, "a": [2, {"d": 3, "c": 4}]}) == chain.canonical({"a": [2, {"c": 4, "d": 3}], "b": 1}) == '{"a":[2,{"c":4,"d":3}],"b":1}'


def test_personal_values_are_keyed_hashes_labelled_by_kind_and_never_the_plain_hash():
    plain = chain.sha256_hex(b"203.0.113.77")
    keyed = chain.pii_hash(b"k1", "client-address", "203.0.113.77")
    assert keyed != plain and "203.0.113.77" not in keyed
    assert keyed != chain.pii_hash(b"k2", "client-address", "203.0.113.77")
    assert keyed != chain.pii_hash(b"k1", "user-agent", "203.0.113.77")
    assert keyed == chain.pii_hash(b"k1", "client-address", "203.0.113.77")
    with pytest.raises(ValueError):
        chain.pii_hash(b"", "client-address", "x")


def test_an_event_hash_covers_every_field():
    base = dict(tenant_id=TENANT, request_id=RID, seq=1, kind="created", at=T, actor="k", detail={"a": 1}, prev_hash="0" * 64)
    reference = chain.event_hash(**base)
    for field, other in (("tenant_id", "other"), ("request_id", "other"), ("seq", 2), ("kind", "viewed"), ("at", T + timedelta(microseconds=1)), ("actor", "j"),
                         ("detail", {"a": 2}), ("prev_hash", "1" * 64)):
        assert chain.event_hash(**{**base, field: other}) != reference, field


def test_a_signature_hash_covers_every_field():
    base = dict(signature_id=SIG, tenant_id=TENANT, request_id=RID, document_sha256="d" * 64, signer_name_typed="Dana", consent_version="v", consent_text_sha256="c" * 64,
                signed_at=T, user_agent_hash="a" * 64, client_addr_hash="b" * 64, prev_hash="p" * 64)
    reference = chain.signature_hash(**base)
    changes = {"signature_id": "x", "tenant_id": "x", "request_id": "x", "document_sha256": "e" * 64, "signer_name_typed": "Dane", "consent_version": "w", "consent_text_sha256": "f" * 64,
               "signed_at": T + timedelta(microseconds=1), "user_agent_hash": "c" * 64, "client_addr_hash": "d" * 64, "prev_hash": "q" * 64}
    assert set(changes) == set(base)
    for field, other in changes.items():
        assert chain.signature_hash(**{**base, field: other}) != reference, field


# ---- the chain check -------------------------------------------------------------------------------------------------------------------

def test_an_untouched_chain_passes_and_its_head_is_the_last_hash():
    events = make_events([("created", {}), ("viewed", {}), ("signed", {})])
    assert chain.check_chain(TENANT, RID, events) == []
    assert chain.head_of(events) == events[-1].entry_hash
    assert chain.head_of([]) is None
    assert chain.check_chain(TENANT, RID, []) == []


def test_an_edited_row_is_reported_with_its_number():
    events = make_events([("created", {}), ("viewed", {"n": 1}), ("signed", {})])
    events[1] = dataclasses.replace(events[1], detail={"n": 2})
    found = chain.check_chain(TENANT, RID, events)
    assert [(p.code, p.seq) for p in found] == [("row_edited", 2)]
    assert "audit row 2" in found[0].message


def test_an_edited_row_whose_hash_was_recomputed_breaks_the_next_link():
    events = make_events([("created", {}), ("viewed", {"n": 1}), ("signed", {})])
    forged = dataclasses.replace(events[1], detail={"n": 2})
    forged = dataclasses.replace(forged, entry_hash=chain.event_hash(tenant_id=TENANT, request_id=RID, seq=2, kind="viewed", at=forged.at, actor="key-1", detail={"n": 2},
                                                                     prev_hash=forged.prev_hash))
    events[1] = forged
    assert [(p.code, p.seq) for p in chain.check_chain(TENANT, RID, events)] == [("link_broken", 3)]


def test_a_deleted_row_is_reported_as_missing_and_breaks_the_link():
    events = make_events([("created", {}), ("viewed", {}), ("consented", {}), ("signed", {})])
    del events[1]
    found = chain.check_chain(TENANT, RID, events)
    assert ("row_missing", 3) in {(p.code, p.seq) for p in found} and ("link_broken", 3) in {(p.code, p.seq) for p in found}
    assert "[2]" in next(p.message for p in found if p.code == "row_missing")


def test_a_deleted_first_row_is_reported():
    events = make_events([("created", {}), ("viewed", {}), ("signed", {})])
    found = chain.check_chain(TENANT, RID, events[1:])
    assert {"row_missing", "link_broken"} <= codes(found)
    assert any("genesis" in p.message for p in found if p.code == "link_broken")


def test_two_rows_swapped_in_order_are_reported_as_reordered():
    events = make_events([("created", {}), ("viewed", {}), ("consented", {}), ("signed", {})])
    events[1], events[2] = events[2], events[1]
    assert "rows_reordered" in codes(chain.check_chain(TENANT, RID, events))


def test_a_chain_made_for_another_request_does_not_pass_as_this_one():
    events = make_events([("created", {}), ("viewed", {})])
    assert chain.check_chain(TENANT, "99999999-9999-4999-8999-999999999999", events)
    assert chain.check_chain("other-tenant", RID, events)


def test_a_truncated_tail_leaves_no_gap_and_that_is_the_stated_limit():
    events = make_events([("created", {}), ("viewed", {}), ("viewed", {})])
    assert chain.check_chain(TENANT, RID, events[:-1]) == []


# ---- the record check ------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("signed", [False, True])
def test_an_untouched_record_has_no_problem(signed):
    request, events, sig = make_world(signed=signed)
    assert chain.check_record(request, events, sig, DOC) == []


def test_a_changed_document_byte_is_reported_with_both_hashes():
    request, events, sig = make_world()
    found = chain.check_record(request, events, sig, b"%PDF-1.4 a small documenT")
    assert codes(found) == {"document_changed"}
    assert request["document_sha256"] in found[0].message


def test_a_document_and_hash_replaced_together_is_caught_by_the_created_row():
    request, events, sig = make_world()
    other = b"a different document"
    request = {**request, "document_sha256": chain.sha256_hex(other)}
    assert "request_changed" in codes(chain.check_record(request, events, sig, other))


@pytest.mark.parametrize("field,value", [("signer_name", "Eve Mallory"), ("signer_email", "eve@example.com"), ("consent_version", "2020-01-01.1"),
                                         ("expires_at", T + timedelta(days=300))])
def test_a_changed_signer_or_expiry_or_consent_version_is_caught_by_the_created_row(field, value):
    request, events, sig = make_world(signed=False)
    assert "request_changed" in codes(chain.check_record({**request, field: value}, events, sig, DOC))


@pytest.mark.parametrize("edit,expected", [
    (lambda s: {**s, "signer_name_typed": "Eve Mallory"}, "signature_edited"),
    (lambda s: {**s, "user_agent_hash": "c" * 64}, "signature_edited"),
    (lambda s: {**s, "client_addr_hash": "c" * 64}, "signature_edited"),
    (lambda s: {**s, "consent_version": "x"}, "signature_edited"),
    (lambda s: {**s, "signed_at": s["signed_at"] + timedelta(seconds=1)}, "signature_edited"),
    (lambda s: {**s, "document_sha256": "e" * 64}, "signature_document_mismatch"),
    (lambda s: {**s, "tenant_id": "55555555-5555-4555-8555-555555555555"}, "signature_foreign"),
    (lambda s: {**s, "request_id": "55555555-5555-4555-8555-555555555555"}, "signature_foreign"),
    (lambda s: {**s, "prev_hash": "f" * 64}, "signature_link_broken"),
    (lambda s: None, "signature_missing"),
])
def test_an_edited_or_missing_signature_record_is_reported(edit, expected):
    request, events, sig = make_world()
    assert expected in codes(chain.check_record(request, events, edit(sig), DOC))


def test_a_signature_for_one_request_pasted_onto_another_is_reported():
    request, events, sig = make_world()
    foreign_doc = b"another request's document"
    request2 = {**request, "document_sha256": chain.sha256_hex(foreign_doc)}
    assert "signature_document_mismatch" in codes(chain.check_record(request2, events, sig, foreign_doc))


def test_a_status_that_disagrees_with_the_audit_rows_is_reported_each_way():
    request, events, sig = make_world()
    assert "status_mismatch" in codes(chain.check_record({**request, "status": "pending"}, events, sig, DOC))
    request, events, sig = make_world(signed=False)
    assert "status_mismatch" in codes(chain.check_record({**request, "status": "declined"}, events, None, DOC))
    assert "status_mismatch" in codes(chain.check_record({**request, "status": "expired"}, events, None, DOC))
    ended = make_events([("created", events[0].detail), ("declined", {})])
    assert "status_mismatch" in codes(chain.check_record(request, ended, None, DOC))
    assert "signature_missing" in codes(chain.check_record({**request, "status": "signed"}, events, None, DOC))


def test_a_missing_signed_row_or_created_row_is_reported():
    request, events, sig = make_world()
    without_signed = events[:-1]
    assert "signed_row_missing" in codes(chain.check_record(request, without_signed, sig, DOC))
    assert "created_row_missing" in codes(chain.check_record(request, events[1:], sig, DOC))


def test_a_signed_row_that_does_not_carry_the_signatures_hash_is_reported():
    request, events, sig = make_world()
    forged = dataclasses.replace(events[-1], detail={**events[-1].detail, "signature_hash": "9" * 64})
    assert "signature_not_in_chain" in codes(chain.check_record(request, events[:-1] + [forged], sig, DOC))
    shifted = dataclasses.replace(events[-1], detail={**events[-1].detail, "signed_at": "2020-01-01T00:00:00.000000Z"})
    assert "signature_time_changed" in codes(chain.check_record(request, events[:-1] + [shifted], sig, DOC))


# ---- the certificate, checked offline --------------------------------------------------------------------------------------------------

def make_certificate():
    request, events, sig = make_world()
    return certificate.build({**request, "created_at": T}, sig, events, consent.consent_text(consent.CURRENT_VERSION), T + timedelta(hours=1))


def test_a_certificate_checks_out_with_and_without_the_document_and_with_the_head_kept_elsewhere():
    cert = make_certificate()
    assert certificate.verify_certificate(cert) == []
    assert certificate.verify_certificate(cert, DOC) == []
    assert certificate.verify_certificate(cert, DOC, cert["chain"]["head"]) == []
    assert certificate.verify_certificate(json.loads(json.dumps(cert)), DOC) == []


def test_a_certificate_names_the_head_the_hash_and_the_register():
    cert = make_certificate()
    text = certificate.render_text(cert)
    for needle in (cert["chain"]["head"], cert["request"]["document_sha256"], "Dana Whitfield", "not a certified e-signature service", cert["signature"]["signature_hash"]):
        assert needle in text
    assert certificate.render_text(cert) == text


def test_a_certificate_with_a_changed_document_is_reported():
    assert "document_changed" in codes(certificate.verify_certificate(make_certificate(), DOC + b"x"))


@pytest.mark.parametrize("path,value,expected", [
    (("signature", "signer_name_typed"), "Eve Mallory", "signature_edited"),
    (("signature", "signed_at"), "2026-11-14T13:00:00.000000Z", "signature_edited"),
    (("signature", "user_agent_hash"), "c" * 64, "signature_edited"),
    (("signature", "document_sha256"), "e" * 64, "signature_document_mismatch"),
    (("signature", "prev_hash"), "f" * 64, "signature_link_broken"),
    (("signature", "signature_hash"), "9" * 64, "signature_edited"),
    (("chain", "head"), "9" * 64, "head_mismatch"),
    (("chain", "length"), 9, "head_mismatch"),
    (("request", "document_sha256"), "e" * 64, "signature_document_mismatch"),
    (("consent_text",), "I agree to anything.", "consent_text_changed"),
])
def test_a_certificate_edited_in_any_part_is_reported(path, value, expected):
    cert = make_certificate()
    target = cert
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    assert expected in codes(certificate.verify_certificate(cert))


def test_a_certificate_with_a_changed_audit_row_or_a_dropped_row_is_reported():
    cert = make_certificate()
    cert["chain"]["events"][1]["detail"] = {"what": "request", "role": "admin"}
    assert "row_edited" in codes(certificate.verify_certificate(cert))
    cert = make_certificate()
    del cert["chain"]["events"][1]
    assert {"row_missing", "link_broken"} <= codes(certificate.verify_certificate(cert))
    cert = make_certificate()
    cert["chain"]["events"].pop()
    assert "head_not_signed" in codes(certificate.verify_certificate(cert))


def test_a_head_kept_elsewhere_that_differs_is_reported_and_a_malformed_file_is_refused_politely():
    cert = make_certificate()
    assert "head_differs_from_copy" in codes(certificate.verify_certificate(cert, None, "0" * 64))
    assert codes(certificate.verify_certificate({"format": "something-else"})) == {"format"}
    assert codes(certificate.verify_certificate({"format": certificate.FORMAT})) == {"malformed"}
    broken = make_certificate()
    broken["chain"]["events"] = []
    assert codes(certificate.verify_certificate(broken)) == {"malformed"}


def test_the_command_line_check_passes_a_good_file_and_fails_a_changed_document(tmp_path):
    cert_file, doc_file, bad_doc = tmp_path / "c.json", tmp_path / "d.pdf", tmp_path / "bad.pdf"
    cert_file.write_text(json.dumps(make_certificate()), encoding="utf-8")
    doc_file.write_bytes(DOC)
    bad_doc.write_bytes(DOC + b"!")
    run = lambda *a: subprocess.run([sys.executable, "-m", "app.esign.certificate", "verify", str(cert_file), *a], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60)  # noqa: E731
    good = run("--document", str(doc_file))
    assert good.returncode == 0 and "PASS" in good.stdout
    bad = run("--document", str(bad_doc))
    assert bad.returncode == 1 and "document_changed" in bad.stdout
