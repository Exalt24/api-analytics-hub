"""A seeded simulation of signing traffic against real Postgres, checked against invariants instead of examples.

THE DIMENSIONS, varied together in every seeded world: two tenants; requests with short and long expiries and a clock that moves between steps; the right name in a
different case and spacing, a wrong name, no name, no consent, a wrong consent version, a wrong document hash; attempts repeated after a success (a retry after a
lost answer, a different attempt after a success); bursts of attempts at the same moment, some right and some wrong; decline; opening and downloading; the other
tenant's key trying every call on every request; and, once the traffic ends, tampering with a random part of a random record.

THE RULES:
  1. A request has at most one signature, whatever was retried or raced, and the model's idea of which requests are signed is the database's.
  2. Every call returns what the model says it must (success, a replay of the first success, or one named refusal); a burst of right attempts makes exactly one new signature.
  3. The other tenant's key never reads, signs, declines, verifies or certifies a request, and the refusal equals the one for an id nobody has.
  4. verify() is true for every record the traffic left untouched and false for every record that was tampered with, and names one of the codes that tampering must produce.
  5. No audit row and no signature row holds a raw user agent, client address or document byte.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import sys
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from esign_fixtures import PII_KEY, T0, admin_rows, needs_db, new_tenants, reset_esign_schema, run, tamper  # noqa: E402

pytestmark = needs_db

from app.db import Database  # noqa: E402
from app.esign import chain, service  # noqa: E402
from app.esign.consent import CURRENT_VERSION  # noqa: E402
from app.esign.errors import (AlreadySigned, ConsentRequired, ConsentVersionMismatch, DocumentChanged, NameMismatch, NameRequired, RequestDeclined,  # noqa: E402
                              RequestExpired, RequestNotFound)
from esign_fixtures import DSN  # noqa: E402

SEEDS = range(50)
SIGNERS = ["Dana Whitfield", "Ravi Anand-Okafor", "Maria de la Cruz", "Li Wei", "Søren Kjær"]
HOUR = timedelta(hours=1)


@dataclass
class Req:
    id: str
    tenant: str
    signer: str
    document: bytes
    digest: str
    expires: object
    state: str = "pending"                      # the model's idea: pending, signed, declined, expired
    signature_id: str | None = None
    tampered: set | None = None
    raw: list = field(default_factory=list)


def vary_case(rng, name):
    out = rng.choice([name, name.upper(), name.lower(), "  " + name.replace(" ", "   ") + " "])
    return out


def sha(data):
    return hashlib.sha256(data).hexdigest()


class World:
    def __init__(self, seed, db):
        self.seed, self.rng, self.db = seed, random.Random(seed), db
        self.a, self.b = new_tenants("sim-a-%d" % seed, "sim-b-%d" % seed)
        self.now = T0
        self.reqs: list[Req] = []
        self.failures: list[str] = []
        self.raw_markers: list[str] = []
        self.cross_attempts = 0

    def expect(self, ok, what):
        if not ok:
            self.failures.append("seed %d: %s" % (self.seed, what))

    # ---- model ----
    def due(self, r):
        return r.state == "pending" and self.now >= r.expires

    async def create(self):
        tenant = self.rng.choice([self.a, self.b])
        signer = self.rng.choice(SIGNERS)
        document = b"%%PDF-1.4 DOC-%d-%d " % (self.seed, len(self.reqs)) + bytes(self.rng.randrange(256) for _ in range(self.rng.randrange(1, 200)))
        hours = self.rng.choice([1, 2, 3, 40, 100])
        view = await service.create_request(self.db, tenant, actor="firm", document=document, document_name="doc-%d.pdf" % len(self.reqs), signer_name=signer,
                                            signer_email="s%d@example.com" % len(self.reqs), expires_at=self.now + timedelta(hours=hours), now=self.now)
        r = Req(view["id"], tenant, signer, document, view["document_sha256"], self.now + timedelta(hours=hours))
        r.raw.append(b"DOC-%d-%d" % (self.seed, len(self.reqs)))
        self.reqs.append(r)

    def attempt_args(self, r, kind, n):
        ua, addr = "SimAgent-%d-%d/1.0" % (self.seed, n), "192.0.2.%d" % (n % 250 + 1)
        self.raw_markers.extend([ua, addr])
        args = dict(actor="client", typed_name=vary_case(self.rng, r.signer), consent=True, consent_version=CURRENT_VERSION, document_sha256=r.digest, user_agent=ua,
                    client_addr=addr, pii_key=PII_KEY)
        if kind == "wrong_name":
            args["typed_name"] = self.rng.choice(["Eve Mallory", r.signer.split()[0], r.signer + " Jr", "Dana"]) if r.signer != "Dana" else "Eve Mallory"
        elif kind == "empty_name":
            args["typed_name"] = self.rng.choice(["", "   "])
        elif kind == "no_consent":
            args["consent"] = self.rng.choice([False, None])
        elif kind == "bad_version":
            args["consent_version"] = "1999-01-01.1"
        elif kind == "bad_hash":
            args["document_sha256"] = sha(b"not this document")
        return args

    def model_sign(self, r, kind):
        """The outcome the model says a single attempt must have: 'ok', 'replay', or an error class. Updates the model state as the service must."""
        if r.state == "signed":
            return "replay" if kind == "ok" else AlreadySigned
        if r.state == "declined":
            return RequestDeclined
        if r.state == "expired" or self.due(r):
            r.state = "expired"
            return RequestExpired
        return {"ok": "ok", "wrong_name": NameMismatch, "empty_name": NameRequired, "no_consent": ConsentRequired, "bad_version": ConsentVersionMismatch,
                "bad_hash": DocumentChanged}[kind]

    async def one_sign(self, r, kind, n):
        want = self.model_sign(r, kind)
        try:
            got = await service.sign(self.db, r.tenant, r.id, now=self.now, **self.attempt_args(r, kind, n))
        except Exception as exc:  # noqa: BLE001
            self.expect(want not in ("ok", "replay") and isinstance(exc, want), "%s sign %s: wanted %s, got %r" % (r.id[:8], kind, getattr(want, "__name__", want), exc))
            return
        if want == "ok":
            r.state, r.signature_id = "signed", got.signature_id
            self.expect(not got.replayed, "first sign was reported as a replay")
        elif want == "replay":
            self.expect(got.replayed and got.signature_id == r.signature_id, "retry did not return the first result")
        else:
            self.expect(False, "%s sign %s succeeded but the model says %s" % (r.id[:8], kind, getattr(want, "__name__", want)))

    async def burst(self, r, n):
        """Right and wrong attempts at the same moment. The model: signable means exactly one new signature, whoever wins."""
        signable = r.state == "pending" and not self.due(r)
        if not signable:
            for i in range(3):
                await self.one_sign(r, self.rng.choice(["ok", "wrong_name"]), n + i)
            return
        kinds = [self.rng.choice(["ok", "ok", "wrong_name", "no_consent"]) for _ in range(self.rng.randrange(2, 6))]
        if "ok" not in kinds:
            kinds.append("ok")
        out = await asyncio.gather(*[service.sign(self.db, r.tenant, r.id, now=self.now, **self.attempt_args(r, k, n + i)) for i, k in enumerate(kinds)], return_exceptions=True)
        wins = [o for o, k in zip(out, kinds) if isinstance(o, service.SignResult)]
        new = [w for w in wins if not w.replayed]
        self.expect(len(new) == 1 and len({w.signature_id for w in wins}) == 1, "burst of %s made %d new signatures and %d distinct ids" % (kinds, len(new), len({w.signature_id for w in wins})))
        for o, k in zip(out, kinds):
            if not isinstance(o, service.SignResult):
                allowed = {"wrong_name": (NameMismatch, AlreadySigned), "no_consent": (ConsentRequired, AlreadySigned)}.get(k, ())
                self.expect(isinstance(o, allowed), "burst attempt %s ended in %r" % (k, o))
            elif k != "ok":
                self.expect(False, "burst attempt %s succeeded" % k)
        if new:
            r.state, r.signature_id = "signed", new[0].signature_id

    async def decline(self, r):
        if r.state == "signed":
            want = AlreadySigned
        elif r.state == "declined":
            want = "declined"
        elif r.state == "expired" or self.due(r):
            r.state, want = "expired", RequestExpired
        else:
            r.state, want = "declined", "declined"
        try:
            got = await service.decline(self.db, r.tenant, r.id, actor="client", now=self.now)
            self.expect(want == "declined" and got == "declined", "decline: wanted %s, got %s" % (want, got))
        except Exception as exc:  # noqa: BLE001
            self.expect(not isinstance(want, str) and isinstance(exc, want), "decline: wanted %s, got %r" % (want, exc))

    async def view(self, r, document):
        try:
            if document:
                _, data, _ = await service.get_document(self.db, r.tenant, r.id, actor="client", role="viewer", now=self.now)
                self.expect(data == r.document, "the document came back changed")
            else:
                if self.due(r):
                    r.state = "expired"
                v = await service.view_request(self.db, r.tenant, r.id, actor="client", role="viewer", now=self.now)
                self.expect(v["status"] == r.state, "view says %s, the model says %s" % (v["status"], r.state))
        except Exception as exc:  # noqa: BLE001
            self.expect(False, "view raised %r" % exc)

    async def cross_tenant(self, r):
        other = self.b if r.tenant == self.a else self.a
        ghost = str(uuid.uuid4())
        calls = [lambda i: service.view_request(self.db, other, i, actor="x", role="admin", now=self.now), lambda i: service.get_document(self.db, other, i, actor="x", role="admin"),
                 lambda i: service.sign(self.db, other, i, now=self.now, **{**self.attempt_args(r, "ok", 999), "typed_name": r.signer}),
                 lambda i: service.decline(self.db, other, i, actor="x", now=self.now), lambda i: service.verify(self.db, other, i, actor="x"),
                 lambda i: service.issue_certificate(self.db, other, i, actor="x")]
        for call in calls:
            self.cross_attempts += 1
            seen = []
            for target in (r.id, ghost):
                try:
                    await call(target)
                    seen.append(None)
                except RequestNotFound as exc:
                    seen.append((type(exc), str(exc), exc.args))
                except Exception as exc:  # noqa: BLE001
                    seen.append(("other", repr(exc)))
            self.expect(seen[0] is not None and seen[0] == seen[1], "another tenant's key got %r for a real id and %r for a ghost" % (seen[0], seen[1]))

    # ---- the run ----
    async def traffic(self):
        for _ in range(self.rng.randrange(3, 7)):
            await self.create()
        n = 0
        for _ in range(self.rng.randrange(25, 45)):
            n += 10
            r = self.rng.choice(self.reqs)
            step = self.rng.choices(["sign", "retry", "burst", "decline", "view", "doc", "cross", "tick", "verify"], [6, 3, 3, 2, 3, 1, 3, 3, 2])[0]
            if step == "sign":
                await self.one_sign(r, self.rng.choice(["ok", "ok", "wrong_name", "empty_name", "no_consent", "bad_version", "bad_hash"]), n)
            elif step == "retry":
                await self.one_sign(r, "ok", n)
                await self.one_sign(r, "ok", n + 1)
            elif step == "burst":
                await self.burst(r, n)
            elif step == "decline":
                await self.decline(r)
            elif step == "view":
                await self.view(r, False)
            elif step == "doc":
                await self.view(r, True)
            elif step == "cross":
                await self.cross_tenant(r)
            elif step == "tick":
                self.now += timedelta(minutes=self.rng.randrange(10, 150))
            elif step == "verify":
                rep = await service.verify(self.db, r.tenant, r.id, actor="client", now=self.now)
                self.expect(rep.ok, "verify of an untouched record said %s" % [p.code for p in rep.problems])

    def tamper_some(self):
        for r in self.reqs:
            if self.rng.random() > 0.45:
                continue
            events = admin_rows("select seq, kind from esign_event where request_id = %s order by seq", (r.id,))
            kinds = [k for _, k in events]
            options = {
                "doc_flip": ([("update esign_request set document = set_byte(document, 0, get_byte(document, 0) # 255) where id = %s", (r.id,))], {"document_changed"}),
                "doc_and_hash": ([("update esign_request set document = %s, document_sha256 = %s where id = %s", (b"swapped-%d" % self.seed, sha(b"swapped-%d" % self.seed), r.id))], {"request_changed"}),
                "email": ([("update esign_request set signer_email = 'eve@example.com' where id = %s", (r.id,))], {"request_changed"}),
                "event_edit": ([("update esign_event set actor = 'mallory' where request_id = %s and seq = %s", (r.id, self.rng.choice(events)[0]))], {"row_edited"}),
            }
            if len(events) >= 2:
                s = self.rng.choice(events[:-1])[0]
                options["event_delete"] = ([("delete from esign_event where request_id = %s and seq = %s", (r.id, s))], {"row_missing"})
                a, b = self.rng.sample([e[0] for e in events], 2)
                options["event_swap"] = ([("update esign_event set seq = 1000 where request_id = %s and seq = %s", (r.id, a)), ("update esign_event set seq = %s where request_id = %s and seq = %s", (a, r.id, b)),
                                          ("update esign_event set seq = %s where request_id = %s and seq = 1000", (b, r.id))], {"rows_reordered"})
            if r.state == "signed":
                options["sig_name"] = ([("update esign_signature set signer_name_typed = 'Eve Mallory' where request_id = %s", (r.id,))], {"signature_edited"})
                options["sig_hash"] = ([("update esign_signature set document_sha256 = repeat('0', 64) where request_id = %s", (r.id,))], {"signature_document_mismatch"})
                options["sig_delete"] = ([("delete from esign_signature where request_id = %s", (r.id,))], {"signature_missing"})
                other = self.b if r.tenant == self.a else self.a
                options["sig_relabel"] = ([("update esign_signature set tenant_id = %s where request_id = %s", (other, r.id))], {"signature_missing"})
                options["status_flip"] = ([("update esign_request set status = 'pending' where id = %s", (r.id,))], {"status_mismatch"})
                options["signed_row_delete"] = ([("delete from esign_event where request_id = %s and kind = 'signed'", (r.id,))], {"signed_row_missing", "row_missing"})
            if r.state in ("declined", "expired"):
                options["status_flip"] = ([("update esign_request set status = 'pending' where id = %s", (r.id,))], {"status_mismatch"})
            name = self.rng.choice(sorted(options))
            statements, expected = options[name]
            tamper(*statements)
            r.tampered = {name: expected}

    async def check_end(self):
        for r in self.reqs:
            rep = await service.verify(self.db, r.tenant, r.id, actor="client", now=self.now)
            codes = {p.code for p in rep.problems}
            if r.tampered is None:
                self.expect(rep.ok, "untouched %s failed verify with %s" % (r.id[:8], codes))
            else:
                (name, expected), = r.tampered.items()
                self.expect(not rep.ok and bool(codes & expected), "tampered (%s) %s verified as %s, wanted one of %s" % (name, r.id[:8], codes or "OK", expected))
        # rule 1 and the model against the database
        sigs = dict(admin_rows("select request_id::text, count(*) from esign_signature group by request_id"))
        for r in self.reqs:
            if r.tampered is not None:
                continue
            self.expect(sigs.get(r.id, 0) == (1 if r.state == "signed" else 0), "%s has %d signatures but the model says %s" % (r.id[:8], sigs.get(r.id, 0), r.state))
            status = admin_rows("select status from esign_request where id = %s", (r.id,))[0][0]
            self.expect(status == r.state or (r.state == "pending" and status == "pending"), "%s is %s, the model says %s" % (r.id[:8], status, r.state))
        self.expect(all(c <= 1 for c in sigs.values()), "a request has more than one signature")
        # rule 3, as the other tenant sees the tables
        async with self.db.tenant(self.a) as conn:
            self.expect(await conn.fetchval("select count(*) from esign_request where tenant_id = $1::uuid", self.b) == 0, "tenant A reads tenant B's requests")
            self.expect(await conn.fetchval("select count(*) from esign_event where tenant_id = $1::uuid", self.b) == 0, "tenant A reads tenant B's events")
        # rule 5
        dump = " ".join(str(c) for q in ("select * from esign_event", "select * from esign_signature") for row in admin_rows(q) for c in row)
        for marker in self.raw_markers:
            self.expect(marker not in dump, "raw %r is in the audit rows or a signature" % marker)
        for r in self.reqs:
            for raw in r.raw:
                self.expect(raw.decode() not in dump, "document marker %r is in the audit rows or a signature" % raw)


async def play(seed):
    db = Database(DSN, max_size=8)
    await db.connect()
    try:
        w = World(seed, db)
        await w.traffic()
        w.tamper_some()
        await w.check_end()
        return w
    finally:
        await db.close()


@pytest.fixture(scope="module", autouse=True)
def schema():
    reset_esign_schema()


@pytest.mark.parametrize("seed", SEEDS)
def test_simulation_world_keeps_every_signing_rule(seed):
    w = run(play(seed))
    assert not w.failures, "\n".join(w.failures[:10])


def test_the_simulation_reaches_the_cases_it_claims_to_cover():
    """A simulation that never signs, races, expires or tampers would pass every rule. Run a few worlds again and count what they did."""
    async def census():
        seen = {"signed": 0, "declined": 0, "expired": 0, "pending": 0, "tampered": 0, "cross": 0}
        for seed in range(6):
            w = await play(1000 + seed)
            for r in w.reqs:
                seen[r.state] += 1
                seen["tampered"] += r.tampered is not None
            seen["cross"] += w.cross_attempts
        return seen
    seen = run(census())
    assert all(seen[k] > 0 for k in ("signed", "declined", "expired", "pending", "tampered", "cross")), seen
