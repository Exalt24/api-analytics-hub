"""The signing flow as HTTP calls, against real Postgres with real row level security: who may create, open, sign and verify, the same 404 for a missing id and someone else's,
a retry that returns the first result, the statuses of every refusal, and that no raw user agent, address or document byte reaches a log or a stored audit row.

Every refusal is paired with the same call succeeding for the caller who should have it.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from esign_fixtures import DOCUMENT, DSN, add_key, admin_rows, needs_db, new_tenants, reset_esign_schema, tamper  # noqa: E402

pytestmark = needs_db

from app.esign import errors  # noqa: E402
from app.esign.certificate import verify_certificate  # noqa: E402

RAW_AGENT = "ApiAgent/9.9 (secret-marker-ua-5521)"
RAW_ADDR = "198.51.100.23"
DOC_MARK = b"API-DOC-MARKER-88213"


@pytest.fixture(scope="module")
def seeded():
    reset_esign_schema()
    a, b = new_tenants("api-firm-a", "api-firm-b")
    keys = {"a_operator": add_key(a, "operator"), "a_admin": add_key(a, "admin"), "a_viewer": add_key(a, "viewer"), "b_operator": add_key(b, "operator"), "b_viewer": add_key(b, "viewer")}
    return {"a": a, "b": b, "keys": keys}


@pytest.fixture(scope="module")
def client(seeded):
    os.environ["DATABASE_URL"] = DSN
    os.environ["ESIGN_PII_KEY"] = "api-test-key-not-a-secret"
    os.environ["ESIGN_TRUST_FORWARDED"] = "1"
    os.environ.setdefault("DASHBOARD_ORIGIN", "http://localhost:3000")
    from contextlib import AsyncExitStack
    from app.api.main import app as fastapi_app

    loop = asyncio.new_event_loop()
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test", timeout=60)
    stack = AsyncExitStack()
    loop.run_until_complete(stack.enter_async_context(fastapi_app.router.lifespan_context(fastapi_app)))
    yield loop, c
    loop.run_until_complete(stack.aclose())
    loop.run_until_complete(c.aclose())
    loop.close()


def call(client, method, url, key=None, **kw):
    loop, c = client
    headers = kw.pop("headers", {})
    if key:
        headers["Authorization"] = "Bearer %s" % key
    return loop.run_until_complete(getattr(c, method)(url, headers=headers, **kw))


def create(client, seeded, who="a_operator", *, document=DOCUMENT, signer="Dana Whitfield", hours=72, **over):
    body = {"document_b64": base64.b64encode(document).decode(), "document_name": "Engagement letter.pdf", "signer_name": signer, "signer_email": "dana@example.com",
            "expires_in_hours": hours, **over}
    return call(client, "post", "/api/esign/requests", seeded["keys"][who], json=body)


def sign_body(view, **over):
    return {"typed_name": view["signer_name"], "consent": True, "consent_version": view["consent_version"], "document_sha256": view["document_sha256"], **over}


def sign(client, seeded, view, who="a_viewer", **over):
    return call(client, "post", "/api/esign/requests/%s/sign" % view["id"], seeded["keys"][who], json=sign_body(view, **over),
                headers={"User-Agent": RAW_AGENT, "X-Forwarded-For": RAW_ADDR + ", 10.0.0.1"})


# ---- who may do what -------------------------------------------------------------------------------------------------------------------

def test_creating_needs_a_key_and_an_operator_or_admin_while_a_viewer_may_read_and_sign(client, seeded):
    body = {"document_b64": base64.b64encode(DOCUMENT).decode(), "document_name": "x.pdf", "signer_name": "Dana Whitfield", "signer_email": "dana@example.com"}
    assert call(client, "post", "/api/esign/requests", json=body).status_code == 401
    assert call(client, "post", "/api/esign/requests", "aah_not_a_real_key", json=body).status_code == 401
    denied = create(client, seeded, who="a_viewer")
    assert denied.status_code == 403 and "viewer" in denied.json()["detail"]
    ok = create(client, seeded, who="a_operator")                                              # the paired control
    assert ok.status_code == 201 and ok.json()["status"] == "pending"
    assert create(client, seeded, who="a_admin").status_code == 201
    view = ok.json()
    assert call(client, "get", "/api/esign/requests", seeded["keys"]["a_viewer"]).status_code == 200
    assert call(client, "get", "/api/esign/requests/%s" % view["id"], seeded["keys"]["a_viewer"]).status_code == 200
    assert call(client, "get", "/api/esign/requests/%s" % view["id"]).status_code == 401
    assert call(client, "post", "/api/esign/expire", seeded["keys"]["a_viewer"]).status_code == 403
    assert call(client, "post", "/api/esign/expire", seeded["keys"]["a_operator"]).status_code == 200


def test_the_whole_flow_over_http_create_open_read_sign_verify_and_certify(client, seeded):
    view = create(client, seeded).json()
    assert view["document_sha256"] == hashlib.sha256(DOCUMENT).hexdigest() and "Consent to sign electronically" in view["consent_text"]
    opened = call(client, "get", "/api/esign/requests/%s" % view["id"], seeded["keys"]["a_viewer"])
    assert opened.status_code == 200 and opened.json()["document_sha256"] == view["document_sha256"]
    doc = call(client, "get", "/api/esign/requests/%s/document" % view["id"], seeded["keys"]["a_viewer"])
    assert doc.status_code == 200 and doc.content == DOCUMENT and doc.headers["x-document-sha256"] == view["document_sha256"]
    signed = sign(client, seeded, view)
    assert signed.status_code == 200 and signed.json()["replayed"] is False and signed.json()["document_sha256"] == view["document_sha256"]
    report = call(client, "post", "/api/esign/requests/%s/verify" % view["id"], seeded["keys"]["a_viewer"]).json()
    assert report["ok"] is True and report["problems"] == [] and report["events_checked"] >= 4
    cert = call(client, "get", "/api/esign/requests/%s/certificate" % view["id"], seeded["keys"]["a_operator"])
    assert cert.status_code == 200 and verify_certificate(cert.json(), DOCUMENT) == []
    text = call(client, "get", "/api/esign/requests/%s/certificate?format=text" % view["id"], seeded["keys"]["a_operator"])
    assert text.status_code == 200 and "SIGNATURE CERTIFICATE" in text.text and cert.json()["chain"]["head"] in text.text
    assert call(client, "get", "/api/esign/requests/%s/certificate?format=xml" % view["id"], seeded["keys"]["a_operator"]).status_code == 400


# ---- retries and refusals ---------------------------------------------------------------------------------------------------------------

def test_a_retry_after_a_lost_answer_is_200_with_the_first_result_and_another_name_is_409(client, seeded):
    view = create(client, seeded).json()
    first = sign(client, seeded, view)
    retry = sign(client, seeded, view)
    assert first.status_code == retry.status_code == 200 and retry.json()["replayed"] is True
    assert {k: retry.json()[k] for k in ("signature_id", "signature_hash", "signed_at")} == {k: first.json()[k] for k in ("signature_id", "signature_hash", "signed_at")}
    other = sign(client, seeded, view, typed_name="Eve Mallory")
    assert other.status_code == 409
    assert admin_rows("select count(*) from esign_signature where request_id = %s", (view["id"],))[0][0] == 1


@pytest.mark.parametrize("over,code", [({"consent": False}, 422), ({"typed_name": ""}, 422), ({"typed_name": "Eve Mallory"}, 422), ({"consent_version": "1999-01-01.1"}, 409),
                                       ({"document_sha256": "0" * 64}, 409)])
def test_each_signing_refusal_has_its_own_status_and_the_good_call_then_works(client, seeded, over, code):
    view = create(client, seeded).json()
    refused = sign(client, seeded, view, **over)
    assert refused.status_code == code, refused.text
    assert admin_rows("select count(*) from esign_signature where request_id = %s", (view["id"],))[0][0] == 0
    assert sign(client, seeded, view).status_code == 200                                       # the paired control


def test_a_declined_request_is_409_to_sign_and_an_expired_one_is_410(client, seeded):
    declined = create(client, seeded).json()
    assert call(client, "post", "/api/esign/requests/%s/decline" % declined["id"], seeded["keys"]["a_viewer"]).json() == {"status": "declined"}
    assert sign(client, seeded, declined).status_code == 409
    expired = create(client, seeded).json()
    past = datetime.now(timezone.utc) - timedelta(days=2)
    tamper(("update esign_request set created_at = %s, expires_at = %s where id = %s", (past - timedelta(days=1), past, expired["id"])))
    assert sign(client, seeded, expired).status_code == 410
    assert call(client, "post", "/api/esign/requests/%s/decline" % expired["id"], seeded["keys"]["a_viewer"]).status_code == 410
    assert sign(client, seeded, create(client, seeded).json()).status_code == 200              # the paired control


def test_a_request_that_cannot_be_stored_or_read_is_422_and_nothing_is_written(client, seeded):
    before = admin_rows("select count(*) from esign_request")[0][0]
    assert create(client, seeded, document_b64="not base64 !!!").status_code == 422
    assert create(client, seeded, document=b"").status_code == 422
    assert create(client, seeded, signer_email="nobody").status_code == 422
    assert create(client, seeded, hours=0).status_code == 422
    assert create(client, seeded, document=b"x" * (5 * 1024 * 1024 + 1)).status_code == 422
    assert admin_rows("select count(*) from esign_request")[0][0] == before
    assert create(client, seeded).status_code == 201


def test_a_tampered_record_fails_verify_and_gets_no_certificate(client, seeded):
    view = create(client, seeded).json()
    assert sign(client, seeded, view).status_code == 200
    tamper(("update esign_event set actor = 'someone-else' where request_id = %s and seq = 2", (view["id"],)))
    report = call(client, "post", "/api/esign/requests/%s/verify" % view["id"], seeded["keys"]["a_viewer"]).json()
    assert report["ok"] is False and {p["code"] for p in report["problems"]} >= {"row_edited"}
    refused = call(client, "get", "/api/esign/requests/%s/certificate" % view["id"], seeded["keys"]["a_operator"])
    assert refused.status_code == 409 and refused.json()["detail"]["problems"]


# ---- tenant isolation ------------------------------------------------------------------------------------------------------------------

def test_another_tenants_key_gets_the_same_404_as_for_an_id_that_does_not_exist(client, seeded):
    view = create(client, seeded).json()
    assert sign(client, seeded, view).status_code == 200
    ghost = str(uuid.uuid4())
    for who in ("b_viewer", "b_operator"):
        for method, suffix in (("get", ""), ("get", "/document"), ("post", "/sign"), ("post", "/decline"), ("post", "/verify"), ("get", "/certificate")):
            kw = {"json": sign_body(view)} if suffix == "/sign" else {}
            theirs = call(client, method, "/api/esign/requests/%s%s" % (view["id"], suffix), seeded["keys"][who], **kw)
            nobody = call(client, method, "/api/esign/requests/%s%s" % (ghost, suffix), seeded["keys"][who], **kw)
            assert (theirs.status_code, theirs.json()) == (nobody.status_code, nobody.json()) == (404, {"detail": "signing request not found"}), (who, method, suffix)
    assert call(client, "get", "/api/esign/requests/%s" % view["id"], seeded["keys"]["a_viewer"]).status_code == 200      # the owner still gets in
    listed = call(client, "get", "/api/esign/requests", seeded["keys"]["b_viewer"]).json()["requests"]
    assert view["id"] not in {r["id"] for r in listed}
    assert admin_rows("select count(*) from esign_signature where request_id = %s", (view["id"],))[0][0] == 1


# ---- what reaches a log or a stored row ------------------------------------------------------------------------------------------------

def test_no_raw_agent_address_or_document_byte_reaches_a_log_or_an_audit_row(client, seeded):
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    loggers = [logging.getLogger(n) for n in ("", "hub", "esign", "uvicorn", "uvicorn.access", "httpx")]
    handler = Capture(level=logging.DEBUG)
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    try:
        document = b"%PDF-1.4 " + DOC_MARK
        view = create(client, seeded, document=document).json()
        call(client, "get", "/api/esign/requests/%s" % view["id"], seeded["keys"]["a_viewer"])
        call(client, "get", "/api/esign/requests/%s/document" % view["id"], seeded["keys"]["a_viewer"])
        assert sign(client, seeded, view).status_code == 200
        call(client, "post", "/api/esign/requests/%s/verify" % view["id"], seeded["keys"]["a_viewer"])
        call(client, "get", "/api/esign/requests/%s/certificate" % view["id"], seeded["keys"]["a_operator"])
        # a second signing with no forwarded header, so the CONNECTION's address is the one that is hashed: it must not be logged either
        direct = create(client, seeded).json()
        assert call(client, "post", "/api/esign/requests/%s/sign" % direct["id"], seeded["keys"]["a_viewer"], json=sign_body(direct), headers={"User-Agent": RAW_AGENT}).status_code == 200
    finally:
        for lg, level in zip(loggers, levels):
            lg.removeHandler(handler)
            lg.setLevel(level)
    texts = []
    for r in records:
        texts.append(r.getMessage())
        texts.extend(str(v) for k, v in r.__dict__.items() if k not in ("args", "msg"))
    log_dump = "\n".join(texts)
    assert any("esign signed" in t for t in texts), "the capture saw no esign log line, so it proves nothing"
    for raw in (RAW_AGENT, "secret-marker-ua-5521", RAW_ADDR, "127.0.0.1", DOC_MARK.decode(), base64.b64encode(document).decode()[:24]):
        assert raw not in log_dump, raw
    rows = " ".join(str(c) for r in admin_rows("select * from esign_event where request_id = %s", (view["id"],)) for c in r)
    rows += " " + " ".join(str(c) for r in admin_rows("select * from esign_signature where request_id = %s", (view["id"],)) for c in r)
    for raw in (RAW_AGENT, "secret-marker-ua-5521", RAW_ADDR, DOC_MARK.decode()):
        assert raw not in rows, raw


def test_the_address_is_the_first_forwarded_entry_when_trusted_and_the_connection_otherwise(client, seeded):
    from app.api import esign as routes
    from app.esign import chain

    view = create(client, seeded).json()
    assert sign(client, seeded, view).status_code == 200
    stored = admin_rows("select client_addr_hash, user_agent_hash from esign_signature where request_id = %s", (view["id"],))[0]
    key = b"api-test-key-not-a-secret"
    assert stored == (chain.pii_hash(key, "client-address", RAW_ADDR), chain.pii_hash(key, "user-agent", RAW_AGENT))

    class Req:
        def __init__(self, headers, host):
            self.headers, self.client = headers, type("C", (), {"host": host})()
    os.environ["ESIGN_TRUST_FORWARDED"] = "0"
    try:
        assert routes.client_address(Req({"x-forwarded-for": "1.2.3.4"}, "127.0.0.1")) == "127.0.0.1"
    finally:
        os.environ["ESIGN_TRUST_FORWARDED"] = "1"
    assert routes.client_address(Req({"x-forwarded-for": "1.2.3.4, 5.6.7.8"}, "127.0.0.1")) == "1.2.3.4"
    assert routes.client_address(Req({}, "127.0.0.1")) == "127.0.0.1"


def test_without_a_configured_key_a_process_key_is_made_once_and_a_warning_is_logged(monkeypatch, caplog):
    from app.api import esign as routes
    monkeypatch.delenv("ESIGN_PII_KEY", raising=False)
    monkeypatch.setattr(routes, "_process_key", None)
    hub = logging.getLogger("hub")
    hub.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="hub"):
            first, second = routes.pii_key(), routes.pii_key()
    finally:
        hub.removeHandler(caplog.handler)
    assert first == second and len(first) == 32 and sum("ESIGN_PII_KEY is not set" in r.getMessage() for r in caplog.records) == 1


def test_every_refusal_type_has_an_http_status():
    from app.api.esign import _STATUS_OF
    refusals = [c for _, c in inspect.getmembers(errors, inspect.isclass) if issubclass(c, errors.EsignError) and c is not errors.EsignError]
    assert refusals and all(c in _STATUS_OF for c in refusals), [c.__name__ for c in refusals if c not in _STATUS_OF]
    assert all(400 <= v < 500 for v in _STATUS_OF.values())
    json.dumps({c.__name__: v for c, v in _STATUS_OF.items()})
