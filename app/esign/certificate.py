"""The completion certificate: a signed request written out so a third party can check it without the database.

The certificate holds the request, the signature record, the consent text, and every audit row from the first to the signed row with its hashes. `verify_certificate`
recomputes the document hash (when the document is handed in), the whole chain from its genesis value, the signature hash, and the links between them. The chain head is the
hash of the signed row; a copy of it kept elsewhere is what shows a rewritten or truncated store (app/esign/chain.py says why).

    python -m app.esign.certificate verify certificate.json [--document file.pdf] [--head <sha256>]

A PDF is not produced: the repository has no Python PDF library, so the certificate is JSON and a plain text rendering of the same facts.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .chain import Event, Problem, check_chain, fmt_time, genesis_hash, sha256_hex, signature_hash
from .consent import CONSENT_TEXTS

FORMAT = "esign-certificate-v1"
REGISTER = ("A signing record with a tamper evident audit trail, built and tested against a local Postgres. It is not a certified e-signature service, "
            "not legal advice and not used by a firm.")


def parse_time(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def event_to_dict(e: Event) -> dict:
    return {"seq": e.seq, "kind": e.kind, "at": fmt_time(e.at), "actor": e.actor, "detail": e.detail, "prev_hash": e.prev_hash, "entry_hash": e.entry_hash}


def event_from_dict(d: dict) -> Event:
    return Event(seq=int(d["seq"]), kind=d["kind"], at=parse_time(d["at"]), actor=d["actor"], detail=d["detail"], prev_hash=d["prev_hash"], entry_hash=d["entry_hash"])


def build(request: dict, signature: dict, events: Sequence[Event], consent_text: str, issued_at: datetime) -> dict:
    """`events` are the audit rows up to and including the signed row, in order."""
    ordered = sorted(events, key=lambda e: e.seq)
    head = ordered[-1]
    return {
        "format": FORMAT,
        "issued_at": fmt_time(issued_at),
        "register": REGISTER,
        "request": {"id": str(request["id"]), "tenant_id": str(request["tenant_id"]), "document_name": request["document_name"],
                    "document_sha256": request["document_sha256"], "document_bytes": request["document_bytes"], "signer_name": request["signer_name"],
                    "signer_email": request["signer_email"], "created_at": fmt_time(request["created_at"]), "expires_at": fmt_time(request["expires_at"])},
        "signature": {"id": str(signature["id"]), "document_sha256": signature["document_sha256"], "signer_name_typed": signature["signer_name_typed"],
                      "consent_version": signature["consent_version"], "consent_text_sha256": signature["consent_text_sha256"], "signed_at": fmt_time(signature["signed_at"]),
                      "user_agent_hash": signature["user_agent_hash"], "client_addr_hash": signature["client_addr_hash"], "prev_hash": signature["prev_hash"],
                      "signature_hash": signature["signature_hash"]},
        "consent_text": consent_text,
        "chain": {"genesis": genesis_hash(str(request["tenant_id"]), str(request["id"])), "head": head.entry_hash, "length": head.seq,
                  "events": [event_to_dict(e) for e in ordered]},
    }


def verify_certificate(cert: dict, document: bytes | None = None, expected_head: str | None = None) -> list[Problem]:
    """Every problem found, empty when the certificate checks out. `document` is the file the signer was shown; `expected_head` is a chain head kept elsewhere."""
    problems: list[Problem] = []
    if cert.get("format") != FORMAT:
        return [Problem("format", "this is not an %s file" % FORMAT)]
    try:
        req, sig, chain = cert["request"], cert["signature"], cert["chain"]
        events = [event_from_dict(d) for d in chain["events"]]
        tenant, rid = req["tenant_id"], req["id"]
    except (KeyError, TypeError, ValueError) as exc:
        return [Problem("malformed", "the certificate is missing a part or holds a bad value: %s" % type(exc).__name__)]
    if not events:
        return [Problem("malformed", "the certificate holds no audit rows")]
    problems.extend(check_chain(tenant, rid, events))
    last = max(events, key=lambda e: e.seq)
    if last.kind != "signed":
        problems.append(Problem("head_not_signed", "the last audit row in the certificate is %r, not the signed row" % last.kind, last.seq))
    if chain.get("head") != last.entry_hash or chain.get("length") != last.seq:
        problems.append(Problem("head_mismatch", "the certificate's chain head or length is not the last audit row's", last.seq))
    if expected_head is not None and expected_head != last.entry_hash:
        problems.append(Problem("head_differs_from_copy", "the chain head %s is not the %s that was kept elsewhere" % (last.entry_hash, expected_head)))
    if document is not None and sha256_hex(document) != req["document_sha256"]:
        problems.append(Problem("document_changed", "the document handed in hashes to %s, not to the certificate's %s" % (sha256_hex(document), req["document_sha256"])))
    if sig["document_sha256"] != req["document_sha256"]:
        problems.append(Problem("signature_document_mismatch", "the signature was made over a different document hash than the request holds"))
    created = [e for e in events if e.kind == "created"]
    if not created or created[0].detail.get("document_sha256") != req["document_sha256"]:
        problems.append(Problem("created_row_mismatch", "the 'created' audit row does not pin the certificate's document hash"))
    if sha256_hex(cert["consent_text"].encode("utf-8")) != sig["consent_text_sha256"]:
        problems.append(Problem("consent_text_changed", "the consent text in the certificate does not hash to the one the signature recorded"))
    known = CONSENT_TEXTS.get(sig["consent_version"])
    if known is not None and known != cert["consent_text"]:
        problems.append(Problem("consent_text_not_the_version", "the consent text is not the text of version %s" % sig["consent_version"]))
    try:
        recomputed = signature_hash(signature_id=sig["id"], tenant_id=tenant, request_id=rid, document_sha256=sig["document_sha256"], signer_name_typed=sig["signer_name_typed"],
                                    consent_version=sig["consent_version"], consent_text_sha256=sig["consent_text_sha256"], signed_at=parse_time(sig["signed_at"]),
                                    user_agent_hash=sig["user_agent_hash"], client_addr_hash=sig["client_addr_hash"], prev_hash=sig["prev_hash"])
    except (KeyError, ValueError):
        return problems + [Problem("malformed", "the signature part cannot be hashed")]
    if recomputed != sig["signature_hash"]:
        problems.append(Problem("signature_edited", "the signature part does not hash to its recorded signature hash"))
    if last.detail.get("signature_hash") != sig["signature_hash"] or last.detail.get("signature_id") != sig["id"]:
        problems.append(Problem("signature_not_in_chain", "the signed audit row does not carry this signature's hash", last.seq))
    before = [e for e in sorted(events, key=lambda e: e.seq) if e.seq < last.seq]
    if not before or before[-1].entry_hash != sig["prev_hash"]:
        problems.append(Problem("signature_link_broken", "the signature does not link to the audit row before the signed row", last.seq))
    return problems


def render_text(cert: dict) -> str:
    """The same facts as plain text, for a person to read or print."""
    r, s, c = cert["request"], cert["signature"], cert["chain"]
    lines = [
        "SIGNATURE CERTIFICATE",
        "",
        "Document:        %s" % r["document_name"],
        "Document SHA-256: %s" % r["document_sha256"],
        "Request:         %s" % r["id"],
        "Named signer:    %s <%s>" % (r["signer_name"], r["signer_email"]),
        "Typed name:      %s" % s["signer_name_typed"],
        "Signed at:       %s" % s["signed_at"],
        "Request made:    %s, expires %s" % (r["created_at"], r["expires_at"]),
        "Consent version: %s (text SHA-256 %s)" % (s["consent_version"], s["consent_text_sha256"]),
        "User agent hash: %s" % s["user_agent_hash"],
        "Address hash:    %s" % s["client_addr_hash"],
        "Signature hash:  %s" % s["signature_hash"],
        "Chain head:      %s (row %d)" % (c["head"], c["length"]),
        "Issued:          %s" % cert["issued_at"],
        "",
        "Audit rows",
    ]
    for e in c["events"]:
        lines.append("  %d  %s  %-9s  %s" % (e["seq"], e["at"], e["kind"], e["entry_hash"]))
    lines += ["", "To check it again: python -m app.esign.certificate verify <this file as JSON> --document <the document>", "", cert["register"], ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("verify", help="check a certificate file")
    v.add_argument("certificate")
    v.add_argument("--document", help="the document file the signer was shown")
    v.add_argument("--head", help="a chain head kept elsewhere")
    args = parser.parse_args(argv)
    cert = json.loads(Path(args.certificate).read_text(encoding="utf-8"))
    document = Path(args.document).read_bytes() if args.document else None
    problems = verify_certificate(cert, document, args.head)
    if problems:
        for p in problems:
            print("FAIL %s: %s" % (p.code, p.message))
        return 1
    print("PASS: the certificate checks out" + (" and the document matches" if document is not None else " (no document was given, so the document hash was not recomputed)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
