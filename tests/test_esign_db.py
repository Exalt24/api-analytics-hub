"""The signing rules against real Postgres as the REQUEST role: the migration, forced row level security with negative tests, append only, one signature for each request
(a second attempt, a retry after a lost answer, a race), expiry and decline, the name, consent and document rules, tenant isolation that does not tell a missing id from
someone else's, what verify() reports for each kind of tampering, and the certificate.

Every refusal is paired with the same call succeeding for the caller who should have it, because a service that refused everything would pass every negative test.
Skips cleanly when Postgres is not running; like the other database suites it drops and recreates the `public` schema it points at.
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from esign_fixtures import (DOCUMENT, DSN, PII_KEY, RAW_ADDR, RAW_AGENT, T0, admin_rows, counts, make_request, needs_db, new_tenants, psycopg, reset_esign_schema, run,  # noqa: E402
                            sign_as, tamper, with_db)

pytestmark = needs_db

from app.esign import chain, certificate, service  # noqa: E402
from app.esign.consent import CURRENT_VERSION  # noqa: E402
from app.esign.errors import (AlreadySigned, CertificateRefused, ConsentRequired, ConsentVersionMismatch, DocumentChanged, InvalidRequest, NameMismatch, NameRequired,  # noqa: E402
                              NotSigned, RequestDeclined, RequestExpired, RequestNotFound)

TABLES = ("esign_request", "esign_signature", "esign_event")
HOUR = timedelta(hours=1)


class World:
    pass


@pytest.fixture()
def world():
    reset_esign_schema()
    w = World()
    w.a, w.b = new_tenants("firm-a", "firm-b")
    return w


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def kinds(request_id: str) -> list[str]:
    return [r[0] for r in admin_rows("select kind from esign_event where request_id = %s order by seq", (request_id,))]


# ---- the migration and the roles -------------------------------------------------------------------------------------------------------

def test_the_migration_is_safe_to_run_twice(world):
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute((ROOT / "migrations" / "016_esign.sql").read_text(encoding="utf-8"))


def test_every_esign_table_has_row_level_security_enabled_forced_and_a_policy(world):
    rows = admin_rows("select relname, relrowsecurity, relforcerowsecurity from pg_class where relname = any(%s)", (list(TABLES),))
    policies = dict(admin_rows("select tablename, count(*) from pg_policies where schemaname = 'public' and tablename = any(%s) group by tablename", (list(TABLES),)))
    assert {r[0] for r in rows} == set(TABLES)
    assert all(r[1] and r[2] for r in rows), rows
    assert all(policies.get(t, 0) >= 1 for t in TABLES), policies


def test_the_connection_really_is_the_request_role_and_not_a_superuser(world):
    async def go(db):
        async with db.tenant(world.a) as conn:
            return await conn.fetchrow("select current_user as who, r.rolsuper, r.rolbypassrls from pg_roles r where r.rolname = current_user")
    row = with_db(go)
    assert (row["who"], row["rolsuper"], row["rolbypassrls"]) == ("app_request", False, False)


# ---- append only -----------------------------------------------------------------------------------------------------------------------

def test_the_request_role_cannot_update_or_delete_an_audit_row_or_a_signature(world):
    async def go(db):
        view = await make_request(db, world.a)
        await sign_as(db, world.a, view)
        refused = {}
        for label, sql in (("event update", "update esign_event set kind = 'viewed'"), ("event delete", "delete from esign_event"),
                           ("signature update", "update esign_signature set signer_name_typed = 'Eve'"), ("signature delete", "delete from esign_signature"),
                           ("request delete", "delete from esign_request"), ("document update", "update esign_request set document = '\\x00'"),
                           ("hash update", "update esign_request set document_sha256 = repeat('0', 64)"), ("signer update", "update esign_request set signer_name = 'Eve'")):
            try:
                async with db.tenant(world.a) as conn:
                    await conn.execute(sql)
                refused[label] = None
            except asyncpg.PostgresError as exc:
                refused[label] = type(exc).__name__
        return view, refused
    view, refused = with_db(go)
    assert all(v == "InsufficientPrivilegeError" for v in refused.values()), refused
    assert counts(view["id"]) == {"events": 3, "signatures": 1}


def test_even_the_owner_cannot_update_or_delete_them_because_of_the_triggers(world):
    async def go(db):
        view = await make_request(db, world.a)
        await sign_as(db, world.a, view)
        return view
    view = with_db(go)
    with psycopg.connect(DSN, autocommit=True) as conn:
        for sql in ("update esign_event set kind = 'viewed'", "delete from esign_event", "update esign_signature set signer_name_typed = 'Eve'", "delete from esign_signature",
                    "delete from esign_request", "update esign_request set document_name = 'x'", "update esign_request set status = 'pending'"):
            with pytest.raises(psycopg.errors.RestrictViolation):
                conn.execute(sql)
    assert counts(view["id"]) == {"events": 3, "signatures": 1}


def test_a_request_status_moves_forward_from_pending_only(world):
    async def go(db):
        view = await make_request(db, world.a)
        async with db.tenant(world.a) as conn:
            await conn.execute("update esign_request set status = 'declined' where id = $1::uuid", view["id"])     # the one change the request role may make
        return view
    view = with_db(go)
    with psycopg.connect(DSN, autocommit=True) as conn:
        for target in ("signed", "expired", "pending"):
            with pytest.raises(psycopg.errors.RestrictViolation):
                conn.execute("update esign_request set status = %s", (target,))


def test_a_second_signature_row_for_one_request_is_refused_by_the_database_itself(world):
    async def go(db):
        view = await make_request(db, world.a)
        await sign_as(db, world.a, view)
        return view
    view = with_db(go)
    with psycopg.connect(DSN, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("""insert into esign_signature (tenant_id, request_id, document_sha256, signer_name_typed, consent_version, consent_text_sha256, signed_at, user_agent_hash,
                                client_addr_hash, prev_hash, signature_hash)
                            select tenant_id, request_id, document_sha256, signer_name_typed, consent_version, consent_text_sha256, signed_at, user_agent_hash, client_addr_hash,
                                prev_hash, signature_hash from esign_signature""")
    assert counts(view["id"])["signatures"] == 1


# ---- creating a request ----------------------------------------------------------------------------------------------------------------

def test_creating_a_request_stores_the_exact_bytes_and_their_sha256_and_pins_them_in_the_created_row(world):
    binary = bytes(range(256)) * 40 + b"\x00\xff"
    async def go(db):
        return await make_request(db, world.a, document=binary, name="every-byte.bin")
    view = with_db(go)
    stored, digest, size = admin_rows("select document, document_sha256, octet_length(document) from esign_request where id = %s", (view["id"],))[0]
    assert bytes(stored) == binary and digest == sha(binary) == view["document_sha256"] and size == len(binary) == view["document_bytes"]
    (detail,) = admin_rows("select detail from esign_event where request_id = %s and kind = 'created'", (view["id"],))[0]
    assert detail["document_sha256"] == sha(binary) and detail["document_bytes"] == len(binary) and detail["consent_version"] == CURRENT_VERSION
    assert view["status"] == "pending" and view["signature"] is None and kinds(view["id"]) == ["created"]


@pytest.mark.parametrize("what,args", [
    ("an empty document", {"document": b""}),
    ("a document over 5 MiB", {"document": b"x" * (5 * 1024 * 1024 + 1)}),
    ("a blank document name", {"name": "   "}),
    ("a blank signer", {"signer": "  "}),
    ("an address with no at sign", {"email": "dana.example.com"}),
    ("an address with a space", {"email": "dana @example.com"}),
    ("an expiry in the past", {"hours": -1}),
    ("an expiry at this very moment", {"hours": 0}),
    ("an expiry beyond a year", {"hours": 24 * 366}),
])
def test_a_request_that_cannot_be_stored_is_refused_and_leaves_nothing(world, what, args):
    async def go(db):
        with pytest.raises(InvalidRequest):
            await make_request(db, world.a, **args)
        ok = await make_request(db, world.a)                                  # the paired control
        return ok
    ok = with_db(go)
    assert admin_rows("select count(*) from esign_request")[0][0] == 1 and ok["status"] == "pending"


def test_an_expiry_without_a_time_zone_is_refused(world):
    from datetime import datetime
    async def go(db):
        with pytest.raises(InvalidRequest):
            await service.create_request(db, world.a, actor="k", document=DOCUMENT, document_name="x", signer_name="Dana", signer_email="d@example.com",
                                         expires_at=datetime(2030, 1, 1), now=T0)
    with_db(go)


# ---- opening and downloading -----------------------------------------------------------------------------------------------------------

def test_opening_a_request_shows_the_hash_and_the_consent_text_and_writes_a_viewed_row(world):
    async def go(db):
        view = await make_request(db, world.a)
        return view, await service.view_request(db, world.a, view["id"], actor="key-client-1", role="viewer", now=T0 + HOUR)
    created, opened = with_db(go)
    assert opened["document_sha256"] == sha(DOCUMENT) and "Consent to sign electronically" in opened["consent_text"] and opened["consent_version"] == CURRENT_VERSION
    assert "document" not in opened and opened["status"] == "pending"
    assert kinds(created["id"]) == ["created", "viewed"]
    (detail,) = admin_rows("select detail from esign_event where request_id = %s and kind = 'viewed'", (created["id"],))[0]
    assert detail == {"what": "request", "role": "viewer"}


def test_the_document_download_returns_the_exact_bytes_and_writes_a_viewed_row(world):
    async def go(db):
        view = await make_request(db, world.a)
        return view, await service.get_document(db, world.a, view["id"], actor="key-client-1", role="viewer", now=T0 + HOUR)
    view, (name, data, digest) = with_db(go)
    assert (name, data, digest) == ("Engagement letter.pdf", DOCUMENT, sha(DOCUMENT)) and kinds(view["id"]) == ["created", "viewed"]


def test_a_replaced_document_is_not_handed_out_and_no_row_is_written(world):
    async def go(db):
        view = await make_request(db, world.a)
        tamper(("update esign_request set document = %s where id = %s", (b"another file", view["id"])))
        with pytest.raises(DocumentChanged):
            await service.get_document(db, world.a, view["id"], actor="k", role="viewer", now=T0)
        return view
    view = with_db(go)
    assert kinds(view["id"]) == ["created"]


def test_listing_shows_only_the_tenants_own_requests_newest_first(world):
    async def go(db):
        first = await make_request(db, world.a, name="one.pdf")
        second = await make_request(db, world.a, name="two.pdf", now=T0 + HOUR)
        await make_request(db, world.b, name="theirs.pdf")
        return first, second, await service.list_requests(db, world.a), await service.list_requests(db, world.b)
    first, second, mine, theirs = with_db(go)
    assert [r["id"] for r in mine] == [second["id"], first["id"]] and [r["document_name"] for r in theirs] == ["theirs.pdf"]
    assert all("consent_text" not in r for r in mine)


# ---- signing ---------------------------------------------------------------------------------------------------------------------------

def test_signing_records_every_field_and_only_hashes_of_the_agent_and_the_address(world):
    async def go(db):
        view = await make_request(db, world.a)
        return view, await sign_as(db, world.a, view, typed_name="dana  WHITFIELD")
    view, result = with_db(go)
    (row,) = admin_rows("select * from esign_signature where request_id = %s", (view["id"],))
    with psycopg.connect(DSN) as probe:
        cols = [d[0] for d in probe.execute("select * from esign_signature limit 0").description]
    sig = dict(zip(cols, row))
    assert str(sig["request_id"]) == view["id"] and str(sig["tenant_id"]) == world.a and sig["document_sha256"] == sha(DOCUMENT)
    assert sig["signer_name_typed"] == "dana  WHITFIELD" and sig["consent_version"] == CURRENT_VERSION and sig["signed_at"] == T0 + HOUR
    assert sig["user_agent_hash"] == chain.pii_hash(PII_KEY, "user-agent", RAW_AGENT) and sig["client_addr_hash"] == chain.pii_hash(PII_KEY, "client-address", RAW_ADDR)
    assert sig["signature_hash"] == result.signature_hash and result.replayed is False
    for stored in sig.values():
        assert RAW_AGENT not in str(stored) and RAW_ADDR not in str(stored)
    assert kinds(view["id"]) == ["created", "consented", "signed"]
    assert sig["prev_hash"] == admin_rows("select entry_hash from esign_event where request_id = %s and kind = 'consented'", (view["id"],))[0][0]
    assert admin_rows("select status from esign_request where id = %s", (view["id"],))[0][0] == "signed"


def test_a_second_attempt_with_a_different_name_is_refused_and_no_second_signature_exists(world):
    async def go(db):
        view = await make_request(db, world.a)
        first = await sign_as(db, world.a, view)
        before = counts(view["id"])
        with pytest.raises(AlreadySigned):
            await sign_as(db, world.a, view, typed_name="Dana Whitfield-Smith")
        with pytest.raises(AlreadySigned):
            await sign_as(db, world.a, view, document_sha256="0" * 64)
        with pytest.raises(AlreadySigned):
            await sign_as(db, world.a, view, consent=False)
        return view, first, before
    view, first, before = with_db(go)
    assert counts(view["id"]) == before == {"events": 3, "signatures": 1}


def test_a_retry_after_a_lost_answer_returns_the_first_result_and_writes_nothing(world):
    async def go(db):
        view = await make_request(db, world.a)
        first = await sign_as(db, world.a, view)
        before = counts(view["id"])
        retry = await sign_as(db, world.a, view, now=T0 + 5 * HOUR, user_agent="another browser", client_addr="198.51.100.9")
        loud = await sign_as(db, world.a, view, now=T0 + 400 * HOUR, typed_name="DANA WHITFIELD")      # long after the expiry, still a retry of a success
        return view, first, retry, loud, before
    view, first, retry, loud, before = with_db(go)
    assert retry.replayed and loud.replayed and not first.replayed
    assert (retry.signature_id, retry.signature_hash, retry.signed_at) == (first.signature_id, first.signature_hash, first.signed_at) == (loud.signature_id, loud.signature_hash, loud.signed_at)
    assert counts(view["id"]) == before


def test_concurrent_attempts_make_exactly_one_signature(world):
    async def go(db):
        view = await make_request(db, world.a)
        attempts = [sign_as(db, world.a, view, typed_name=n) for n in ["Dana Whitfield"] * 4 + ["dana whitfield", "Eve Mallory", "Dana Whitfield Jr"]]
        return view, await asyncio.gather(*attempts, return_exceptions=True)
    view, outcomes = with_db(go, size=8)
    wins = [o for o in outcomes if isinstance(o, service.SignResult)]
    assert len({w.signature_id for w in wins}) == 1 and sum(1 for w in wins if not w.replayed) == 1 and len(wins) == 5
    assert all(isinstance(o, (NameMismatch, AlreadySigned)) for o in outcomes if not isinstance(o, service.SignResult)), outcomes
    assert counts(view["id"])["signatures"] == 1 and kinds(view["id"]).count("signed") == 1


def test_an_expired_request_cannot_be_signed_and_the_expiry_is_written_once(world):
    async def go(db):
        view = await make_request(db, world.a, hours=2)
        late = T0 + 3 * HOUR
        with pytest.raises(RequestExpired):
            await sign_as(db, world.a, view, now=late)
        with pytest.raises(RequestExpired):
            await sign_as(db, world.a, view, now=late + HOUR)
        ok_view = await make_request(db, world.a, hours=2)
        ok = await sign_as(db, world.a, ok_view, now=T0 + HOUR)                                # the paired control
        return view, ok
    view, ok = with_db(go)
    assert kinds(view["id"]) == ["created", "expired"] and admin_rows("select status from esign_request where id = %s", (view["id"],))[0][0] == "expired"
    assert counts(view["id"])["signatures"] == 0 and not ok.replayed


def test_the_expiry_boundary_is_the_instant_itself(world):
    async def go(db):
        a = await make_request(db, world.a, hours=2)
        b = await make_request(db, world.a, hours=2)
        just_before = await sign_as(db, world.a, a, now=T0 + 2 * HOUR - timedelta(microseconds=1))
        with pytest.raises(RequestExpired):
            await sign_as(db, world.a, b, now=T0 + 2 * HOUR)
        return just_before
    assert not with_db(go).replayed


def test_opening_a_request_past_its_expiry_marks_it_expired_and_it_then_refuses_a_signature(world):
    async def go(db):
        view = await make_request(db, world.a, hours=1)
        opened = await service.view_request(db, world.a, view["id"], actor="k", role="viewer", now=T0 + 2 * HOUR)
        with pytest.raises(RequestExpired):
            await sign_as(db, world.a, view, now=T0 + 2 * HOUR)
        return view, opened
    view, opened = with_db(go)
    assert opened["status"] == "expired" and kinds(view["id"]) == ["created", "expired", "viewed"]


def test_expire_due_marks_only_the_pending_requests_past_their_expiry(world):
    async def go(db):
        due = await make_request(db, world.a, hours=1)
        later = await make_request(db, world.a, hours=10)
        signed = await make_request(db, world.a, hours=1)
        await sign_as(db, world.a, signed, now=T0 + timedelta(minutes=10))
        other = await make_request(db, world.b, hours=1)
        n = await service.expire_due(db, world.a, now=T0 + 2 * HOUR)
        again = await service.expire_due(db, world.a, now=T0 + 3 * HOUR)
        return due, later, signed, other, n, again
    due, later, signed, other, n, again = with_db(go)
    status = lambda r: admin_rows("select status from esign_request where id = %s", (r["id"],))[0][0]  # noqa: E731
    assert (n, again) == (1, 0) and [status(r) for r in (due, later, signed, other)] == ["expired", "pending", "signed", "pending"]


def test_a_declined_request_cannot_be_signed(world):
    async def go(db):
        view = await make_request(db, world.a)
        assert await service.decline(db, world.a, view["id"], actor="k", now=T0 + HOUR) == "declined"
        with pytest.raises(RequestDeclined):
            await sign_as(db, world.a, view)
        return view
    view = with_db(go)
    assert kinds(view["id"]) == ["created", "declined"] and counts(view["id"])["signatures"] == 0


def test_declining_twice_changes_nothing_and_a_signed_or_expired_request_cannot_be_declined(world):
    async def go(db):
        view = await make_request(db, world.a)
        await service.decline(db, world.a, view["id"], actor="k", now=T0 + HOUR)
        await service.decline(db, world.a, view["id"], actor="k", now=T0 + 2 * HOUR)
        signed = await make_request(db, world.a)
        await sign_as(db, world.a, signed)
        with pytest.raises(AlreadySigned):
            await service.decline(db, world.a, signed["id"], actor="k", now=T0 + 2 * HOUR)
        expiring = await make_request(db, world.a, hours=1)
        with pytest.raises(RequestExpired):
            await service.decline(db, world.a, expiring["id"], actor="k", now=T0 + 2 * HOUR)
        with pytest.raises(RequestExpired):
            await service.decline(db, world.a, expiring["id"], actor="k", now=T0 + 3 * HOUR)
        return view, signed, expiring
    view, signed, expiring = with_db(go)
    assert kinds(view["id"]) == ["created", "declined"] and kinds(signed["id"]) == ["created", "consented", "signed"] and kinds(expiring["id"]) == ["created", "expired"]


@pytest.mark.parametrize("typed,error", [("", NameRequired), ("   ", NameRequired), ("Eve Mallory", NameMismatch), ("Dana", NameMismatch), ("Dana M Whitfield", NameMismatch),
                                         ("Dana Whitfield" + " x" * 300, NameMismatch), ("Dana Whitfield" + " " * 500, NameMismatch)])
def test_an_empty_or_mismatched_typed_name_is_refused_and_a_matching_one_signs(world, typed, error):
    async def go(db):
        view = await make_request(db, world.a)
        with pytest.raises(error):
            await sign_as(db, world.a, view, typed_name=typed)
        before = counts(view["id"])
        ok = await sign_as(db, world.a, view, typed_name="  DANA   whitfield ")             # the paired control
        return view, before, ok
    view, before, ok = with_db(go)
    assert before == {"events": 1, "signatures": 0} and ok.signer_name_typed == "  DANA   whitfield "


def test_no_consent_means_no_signature(world):
    async def go(db):
        view = await make_request(db, world.a)
        with pytest.raises(ConsentRequired):
            await sign_as(db, world.a, view, consent=False)
        with pytest.raises(ConsentRequired):
            await sign_as(db, world.a, view, consent=None)
        with pytest.raises(ConsentRequired):
            await sign_as(db, world.a, view, consent="yes")                                   # only the boolean True counts
        return view
    view = with_db(go)
    assert counts(view["id"]) == {"events": 1, "signatures": 0}


def test_a_consent_version_other_than_the_requests_is_refused(world):
    async def go(db):
        view = await make_request(db, world.a)
        with pytest.raises(ConsentVersionMismatch):
            await sign_as(db, world.a, view, consent_version="2020-01-01.1")
        return view, await sign_as(db, world.a, view)
    view, ok = with_db(go)
    assert not ok.replayed


def test_a_replaced_document_is_refused_at_signing_even_when_its_hash_column_was_replaced_too(world):
    async def go(db):
        flipped = await make_request(db, world.a)
        tamper(("update esign_request set document = %s where id = %s", (DOCUMENT + b"!", flipped["id"])))
        with pytest.raises(DocumentChanged):
            await sign_as(db, world.a, flipped)
        swapped = await make_request(db, world.a)
        other = b"a different file"
        tamper(("update esign_request set document = %s, document_sha256 = %s where id = %s", (other, sha(other), swapped["id"])))
        with pytest.raises(DocumentChanged):                                                   # the signer saw the old hash, so this is not what they read
            await sign_as(db, world.a, swapped)
        return flipped, swapped
    flipped, swapped = with_db(go)
    assert counts(flipped["id"])["signatures"] == 0 and counts(swapped["id"])["signatures"] == 0


def test_a_signer_who_saw_a_different_hash_is_refused(world):
    async def go(db):
        view = await make_request(db, world.a)
        for bad in ("0" * 64, "", sha(b"x")):
            with pytest.raises(DocumentChanged):
                await sign_as(db, world.a, view, document_sha256=bad)
        return view, await sign_as(db, world.a, view, document_sha256="  " + view["document_sha256"].upper() + " ")
    view, ok = with_db(go)
    assert ok.document_sha256 == view["document_sha256"]


def test_a_refused_signing_leaves_no_row_behind(world):
    async def go(db):
        view = await make_request(db, world.a)
        for over in ({"consent": False}, {"typed_name": "Eve"}, {"document_sha256": "1" * 64}, {"consent_version": "x"}):
            before = counts(view["id"])
            with pytest.raises((ConsentRequired, NameMismatch, DocumentChanged, ConsentVersionMismatch)):
                await sign_as(db, world.a, view, **over)
            assert counts(view["id"]) == before
        return view
    with_db(go)


# ---- tenant isolation ------------------------------------------------------------------------------------------------------------------

def test_another_tenants_key_cannot_open_download_sign_decline_verify_or_certify_and_the_refusal_matches_a_missing_id(world):
    async def go(db):
        view = await make_request(db, world.a)
        signed = await make_request(db, world.a)
        await sign_as(db, world.a, signed)
        ghost = str(uuid.uuid4())
        outcomes = {}
        for name, call in (
            ("open", lambda rid: service.view_request(db, world.b, rid, actor="k", role="admin")),
            ("document", lambda rid: service.get_document(db, world.b, rid, actor="k", role="admin")),
            ("sign", lambda rid: service.sign(db, world.b, rid, actor="k", typed_name="Dana Whitfield", consent=True, consent_version=CURRENT_VERSION,
                                              document_sha256=sha(DOCUMENT), user_agent="u", client_addr="a", pii_key=PII_KEY)),
            ("decline", lambda rid: service.decline(db, world.b, rid, actor="k")),
            ("verify", lambda rid: service.verify(db, world.b, rid, actor="k")),
            ("certificate", lambda rid: service.issue_certificate(db, world.b, rid, actor="k")),
        ):
            pair = []
            for rid in (view["id"], signed["id"], ghost):
                with pytest.raises(RequestNotFound) as exc:
                    await call(rid)
                pair.append((type(exc.value), str(exc.value), exc.value.args))
            outcomes[name] = pair
        return view, signed, outcomes
    view, signed, outcomes = with_db(go)
    for name, (theirs, signed_theirs, ghost) in outcomes.items():
        assert theirs == signed_theirs == ghost, name                                         # someone else's id and an id nobody has look identical
    assert counts(view["id"]) == {"events": 1, "signatures": 0} and kinds(signed["id"]) == ["created", "consented", "signed"]


def test_a_malformed_request_id_is_the_same_refusal(world):
    async def go(db):
        for bad in ("not-a-uuid", "", "1" * 40, "../../etc/passwd", "' or 1=1 --"):
            with pytest.raises(RequestNotFound) as exc:
                await service.view_request(db, world.a, bad, actor="k", role="viewer")
            assert str(exc.value) == "signing request not found"
    with_db(go)


def test_the_owner_tenant_gets_the_same_calls_through(world):
    async def go(db):
        view = await make_request(db, world.a)
        return await service.view_request(db, world.a, view["id"], actor="k", role="viewer"), await service.verify(db, world.a, view["id"], actor="k")
    opened, report = with_db(go)
    assert opened["status"] == "pending" and report.ok


def test_tenant_a_sees_none_of_tenant_bs_rows_in_any_esign_table_and_all_of_its_own(world):
    async def go(db):
        mine = await make_request(db, world.a)
        theirs = await make_request(db, world.b)
        await sign_as(db, world.a, mine)
        await sign_as(db, world.b, theirs)
        out = {}
        for t in TABLES:
            async with db.tenant(world.a) as conn:
                out[t] = (await conn.fetchval("select count(*) from %s where tenant_id = $1::uuid" % t, world.b), await conn.fetchval("select count(*) from %s" % t),
                          await conn.fetchval("select count(*) from %s where tenant_id = $1::uuid" % t, world.a))
        return out
    for t, (of_b, visible, of_a) in with_db(go).items():
        assert of_b == 0 and visible == of_a and of_a > 0, t


def test_a_connection_with_no_tenant_set_sees_nothing_at_all(world):
    async def go(db):
        view = await make_request(db, world.a)
        await sign_as(db, world.a, view)
        async with db.unscoped() as conn:
            return [await conn.fetchval("select count(*) from %s" % t) for t in TABLES]
    assert with_db(go) == [0, 0, 0]


def test_tenant_a_cannot_write_a_row_for_tenant_b_in_any_esign_table(world):
    async def go(db):
        mine = await make_request(db, world.a)
        theirs = await make_request(db, world.b)
        failures = {}
        attempts = {
            "request": ("insert into esign_request (tenant_id, document_name, document, document_sha256, signer_name, signer_email, consent_version, created_by, created_at, expires_at) "
                        "values ($1::uuid, 'x', '\\x01', repeat('a', 64), 'n', 'n@example.com', 'v', 'k', now(), now() + interval '1 day')", (world.b,)),
            "event": ("insert into esign_event (tenant_id, request_id, seq, kind, at, actor, prev_hash, entry_hash) values ($1::uuid, $2::uuid, 9, 'viewed', now(), 'k', repeat('a', 64), repeat('b', 64))",
                      (world.b, theirs["id"])),
            "signature": ("insert into esign_signature (tenant_id, request_id, document_sha256, signer_name_typed, consent_version, consent_text_sha256, signed_at, user_agent_hash, client_addr_hash, "
                          "prev_hash, signature_hash) values ($1::uuid, $2::uuid, repeat('a', 64), 'n', 'v', repeat('a', 64), now(), repeat('a', 64), repeat('a', 64), repeat('a', 64), repeat('a', 64))",
                          (world.b, theirs["id"])),
        }
        for label, (sql, args) in attempts.items():
            try:
                async with db.tenant(world.a) as conn:
                    await conn.execute(sql, *args)
                failures[label] = None
            except asyncpg.PostgresError as exc:
                failures[label] = type(exc).__name__
        return theirs, failures
    theirs, failures = with_db(go)
    assert failures == {"request": "InsufficientPrivilegeError", "event": "InsufficientPrivilegeError", "signature": "InsufficientPrivilegeError"}, failures
    assert counts(theirs["id"]) == {"events": 1, "signatures": 0}


# ---- verify ----------------------------------------------------------------------------------------------------------------------------

def test_an_untouched_record_verifies_in_every_state_and_each_run_writes_a_verified_row(world):
    async def go(db):
        pending = await make_request(db, world.a)
        signed = await make_request(db, world.a)
        await sign_as(db, world.a, signed)
        declined = await make_request(db, world.a)
        await service.decline(db, world.a, declined["id"], actor="k", now=T0 + HOUR)
        expired = await make_request(db, world.a, hours=1)
        await service.expire_due(db, world.a, now=T0 + 2 * HOUR)
        reports = [await service.verify(db, world.a, r["id"], actor="k") for r in (pending, signed, declined, expired)]
        again = await service.verify(db, world.a, signed["id"], actor="k")
        return signed, reports, again
    signed, reports, again = with_db(go)
    assert all(r.ok and not r.problems for r in reports) and again.ok
    assert kinds(signed["id"]) == ["created", "consented", "signed", "verified", "verified"]
    (detail,) = admin_rows("select detail from esign_event where request_id = %s and kind = 'verified' order by seq limit 1", (signed["id"],))[0]
    assert detail["ok"] is True and detail["problems"] == [] and detail["chain_head"] == reports[1].chain_head
    assert reports[1].document_sha256 == sha(DOCUMENT) and reports[1].events_checked == 3


def _signed_request(world):
    async def go(db):
        view = await make_request(db, world.a)
        await service.view_request(db, world.a, view["id"], actor="k", role="viewer", now=T0 + timedelta(minutes=5))
        await sign_as(db, world.a, view)
        await service.view_request(db, world.a, view["id"], actor="k", role="viewer", now=T0 + 2 * HOUR)
        return view
    return with_db(go)


def _verify(world, request_id):
    async def go(db):
        return await service.verify(db, world.a, request_id, actor="k")
    return with_db(go)


TAMPERS = {
    "a changed byte in the document": (lambda w, v: [("update esign_request set document = set_byte(document, 5, get_byte(document, 5) # 1) where id = %s", (v["id"],))], {"document_changed"}),
    "the document and its hash both replaced": (lambda w, v: [("update esign_request set document = %s, document_sha256 = %s where id = %s", (b"swapped", sha(b"swapped"), v["id"]))], {"request_changed"}),
    "the signer's email edited": (lambda w, v: [("update esign_request set signer_email = 'eve@example.com' where id = %s", (v["id"],))], {"request_changed"}),
    "the expiry moved": (lambda w, v: [("update esign_request set expires_at = expires_at + interval '1 day' where id = %s", (v["id"],))], {"request_changed"}),
    "an audit row edited": (lambda w, v: [("update esign_event set actor = 'someone-else' where request_id = %s and kind = 'viewed' and seq = 2", (v["id"],))], {"row_edited"}),
    "an audit detail edited": (lambda w, v: [("update esign_event set detail = detail || '{\"x\": 1}' where request_id = %s and kind = 'consented'", (v["id"],))], {"row_edited"}),
    "an audit row deleted": (lambda w, v: [("delete from esign_event where request_id = %s and seq = 2", (v["id"],))], {"row_missing"}),
    "the first audit row deleted": (lambda w, v: [("delete from esign_event where request_id = %s and seq = 1", (v["id"],))], {"row_missing"}),
    "the signed row deleted": (lambda w, v: [("delete from esign_event where request_id = %s and kind = 'signed'", (v["id"],))], {"signed_row_missing", "row_missing"}),
    "two audit rows reordered": (lambda w, v: [("update esign_event set seq = 100 where request_id = %s and seq = 2", (v["id"],)), ("update esign_event set seq = 2 where request_id = %s and seq = 3", (v["id"],)),
                                              ("update esign_event set seq = 3 where request_id = %s and seq = 100", (v["id"],))], {"rows_reordered"}),
    "an audit row edited and its hash recomputed": (lambda w, v: [("update esign_event set detail = '{\"what\": \"request\", \"role\": \"admin\"}', "
                                                                   "entry_hash = %s where request_id = %s and seq = 2", (_rehash(v["id"], 2, {"what": "request", "role": "admin"}), v["id"]))], {"link_broken"}),
    "the typed name edited": (lambda w, v: [("update esign_signature set signer_name_typed = 'Eve Mallory' where request_id = %s", (v["id"],))], {"signature_edited"}),
    "the signature's document hash edited": (lambda w, v: [("update esign_signature set document_sha256 = repeat('0', 64) where request_id = %s", (v["id"],))], {"signature_document_mismatch"}),
    "the address hash edited": (lambda w, v: [("update esign_signature set client_addr_hash = repeat('0', 64) where request_id = %s", (v["id"],))], {"signature_edited"}),
    "the signature deleted": (lambda w, v: [("delete from esign_signature where request_id = %s", (v["id"],))], {"signature_missing"}),
    "the signature relabelled to another tenant": (lambda w, v: [("update esign_signature set tenant_id = %s where request_id = %s", (w.b, v["id"]))], {"signature_missing"}),
    "the status flipped back to pending": (lambda w, v: [("update esign_request set status = 'pending' where id = %s", (v["id"],))], {"status_mismatch"}),
}


def _rehash(request_id, seq, detail):
    (kind, at, actor, prev, tenant) = admin_rows("select kind, at, actor, prev_hash, tenant_id from esign_event where request_id = %s and seq = %s", (request_id, seq))[0]
    return chain.event_hash(tenant_id=str(tenant), request_id=request_id, seq=seq, kind=kind, at=at, actor=actor, detail=detail, prev_hash=prev)


@pytest.mark.parametrize("what", sorted(TAMPERS), ids=lambda w: w.replace(" ", "_").replace("'", ""))
def test_verify_reports_each_kind_of_tampering_by_its_code_and_passed_before_it(world, what):
    view = _signed_request(world)
    assert _verify(world, view["id"]).ok                                                       # the paired control: the same record before the change
    build, expected = TAMPERS[what]
    statements = build(world, view)
    tamper(*statements)
    report = _verify(world, view["id"])
    found = {p.code for p in report.problems}
    assert not report.ok and found & expected, (what, found)


def test_verify_says_exactly_which_row_and_which_hash_changed(world):
    view = _signed_request(world)
    tamper(("update esign_event set actor = 'someone-else' where request_id = %s and seq = 2", (view["id"],)))
    report = _verify(world, view["id"])
    edited = [p for p in report.problems if p.code == "row_edited"]
    assert [(p.seq, "audit row 2 (viewed)" in p.message) for p in edited] == [(2, True)]
    tamper(("update esign_request set document = set_byte(document, 0, 0) where id = %s", (view["id"],)))
    flipped = [p for p in _verify(world, view["id"]).problems if p.code == "document_changed"]
    assert len(flipped) == 1 and sha(DOCUMENT) in flipped[0].message


def test_a_signature_from_another_tenant_is_not_taken_as_this_requests_and_not_visible_to_this_tenant(world):
    async def go(db):
        mine = await make_request(db, world.a)
        theirs = await make_request(db, world.b, document=b"their document")
        await sign_as(db, world.a, mine)
        await sign_as(db, world.b, theirs, typed_name="Dana Whitfield")
        return mine, theirs
    mine, theirs = with_db(go)
    assert _verify(world, mine["id"]).ok
    # B's signature record is made to claim A's request (rows and keys rewritten behind the application's back): A's check must not accept it
    tamper(("delete from esign_signature where request_id = %s", (mine["id"],)),
           ("update esign_signature set request_id = %s, tenant_id = %s where request_id = %s", (mine["id"], world.a, theirs["id"])))
    report = _verify(world, mine["id"])
    assert not report.ok and {p.code for p in report.problems} & {"signature_document_mismatch", "signature_edited", "signature_link_broken", "signature_not_in_chain"}


def test_a_truncated_tail_is_invisible_to_the_chain_alone_and_caught_by_the_head_in_the_certificate(world):
    async def go(db):
        view = await make_request(db, world.a)
        await sign_as(db, world.a, view)
        return view, await service.issue_certificate(db, world.a, view["id"], actor="k", now=T0 + 2 * HOUR)
    view, cert = with_db(go)
    tamper(("delete from esign_event where request_id = %s and kind = 'verified'", (view["id"],)))                # the newest rows, gone
    assert _verify(world, view["id"]).ok                                                       # nothing in the store shows it: the stated limit
    assert certificate.verify_certificate(cert, DOCUMENT, cert["chain"]["head"]) == []
    tamper(("delete from esign_event where request_id = %s and kind = 'signed'", (view["id"],)))
    assert not _verify(world, view["id"]).ok
    assert [p.code for p in certificate.verify_certificate(cert, DOCUMENT, chain.head_of([]) or "0" * 64)] == ["head_differs_from_copy"]


# ---- what is stored --------------------------------------------------------------------------------------------------------------------

def test_audit_rows_and_the_signature_hold_no_raw_agent_address_or_document_bytes(world):
    async def go(db):
        view = await make_request(db, world.a)
        await service.view_request(db, world.a, view["id"], actor="key-client-1", role="viewer", now=T0 + HOUR)
        await sign_as(db, world.a, view)
        await service.verify(db, world.a, view["id"], actor="key-client-1")
        await service.issue_certificate(db, world.a, view["id"], actor="key-client-1")
        return view
    view = with_db(go)
    dump = " ".join(str(c) for r in admin_rows("select * from esign_event") for c in r) + " " + " ".join(str(c) for r in admin_rows("select * from esign_signature") for c in r)
    for raw in (RAW_AGENT, "RawAgentMarker", RAW_ADDR, "DOCMARKER-4471", DOCUMENT.decode("latin-1")[:20], "dana@example.com", "Engagement letter for"):
        assert raw not in dump, raw


# ---- the certificate -------------------------------------------------------------------------------------------------------------------

def test_a_certificate_of_a_signed_request_verifies_offline_and_lists_the_chain_head(world):
    async def go(db):
        view = await make_request(db, world.a)
        await service.view_request(db, world.a, view["id"], actor="k", role="viewer", now=T0 + timedelta(minutes=5))
        await sign_as(db, world.a, view)
        return view, await service.issue_certificate(db, world.a, view["id"], actor="k", now=T0 + 3 * HOUR)
    view, cert = with_db(go)
    assert certificate.verify_certificate(cert, DOCUMENT) == []
    signed_hash = admin_rows("select entry_hash from esign_event where request_id = %s and kind = 'signed'", (view["id"],))[0][0]
    assert cert["chain"]["head"] == signed_hash and cert["chain"]["length"] == 4 and len(cert["chain"]["events"]) == 4
    assert cert["request"]["id"] == view["id"] and cert["signature"]["document_sha256"] == sha(DOCUMENT) and cert["signature"]["signer_name_typed"] == "Dana Whitfield"
    assert "not a certified e-signature service" in cert["register"]
    text = certificate.render_text(cert)
    assert signed_hash in text and sha(DOCUMENT) in text
    assert RAW_AGENT not in str(cert) and RAW_ADDR not in str(cert) and "DOCMARKER" not in str(cert)


def test_no_certificate_for_an_unsigned_request_and_none_for_a_record_that_does_not_verify(world):
    async def go(db):
        view = await make_request(db, world.a)
        with pytest.raises(NotSigned):
            await service.issue_certificate(db, world.a, view["id"], actor="k")
        signed = await make_request(db, world.a)
        await sign_as(db, world.a, signed)
        return view, signed
    view, signed = with_db(go)
    tamper(("update esign_request set document = set_byte(document, 0, 1) where id = %s", (signed["id"],)))
    async def go2(db):
        with pytest.raises(CertificateRefused) as exc:
            await service.issue_certificate(db, world.a, signed["id"], actor="k")
        return exc.value
    refused = with_db(go2)
    assert {p.code for p in refused.problems} == {"document_changed"}
