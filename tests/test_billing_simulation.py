"""A seeded simulation of billing traffic against real Postgres and the fake Stripe, checked against invariants instead of examples.

THE DIMENSIONS, varied together in every seeded world: two tenants; top-ups that succeed or are declined; the provider failing before it runs (429, 5xx) or its
response being lost after it ran, with search lagging or not; requests retried with the same key and requests repeated; provider events delivered late, out of
order, duplicated, as a second event object for the same payment, tampered with or stale; refunds in pieces, asked while earlier ones are still undelivered;
usage charged, retried and charged in a concurrent burst against a wallet that cannot cover all of it; admin adjustments and service credits.

THE RULES (each one a way a billing system loses or invents money):
  1. After every step both tenants reconcile: stored balance equals the sum of the entries, every transaction sums to zero, the trial balance is zero.
  2. A wallet is never below zero, and no command raised a ledger invariant violation.
  3. A tampered or stale event changes nothing.
  4. At the end, once every pending event is delivered and every unsettled payment is asked about with its own key: the provider holds exactly one charge for each
     payment we asked for (a retry, a repeat or a lost response never made a second one), and each was credited exactly once.
  5. The wallet equals what came in (credits, adjustments, service credits) minus what went out (applied usage, reversed refunds). A refund the wallet could not cover
     is held with a notice and is the only way the provider's refunded total may exceed ours.
"""
from __future__ import annotations

import asyncio
import random
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from billing_fixtures import CURRENCY, SECRETS, needs_db, new_tenants, reset_billing_schema, set_price, with_rig  # noqa: E402

pytestmark = needs_db

from app.billing import payments, reports, wallet, webhooks  # noqa: E402
from app.billing.errors import BillingError, LedgerInvariantViolation  # noqa: E402
from app.billing.fake_provider import Events  # noqa: E402
from app.billing.provider import ProviderError  # noqa: E402

SEEDS = range(10)
STEPS = 34


class Sim:
    def __init__(self, rig, rng, tenants):
        self.rig, self.rng, self.tenants = rig, rng, tenants
        self.payments: list[dict] = []              # {tenant, key, amount, method}
        self.pending: list[tuple[bytes, str]] = []  # signed deliveries not yet made
        self.applied_usage = {t: 0 for t in tenants}
        self.applied_other = {t: 0 for t in tenants}    # adjustments and service credits that were applied
        self.used_keys = {t: [] for t in tenants}
        self.steps_taken = 0

    # -- provider failure injection ------------------------------------------------------------------------------------------------------

    def inject(self):
        r, fake = self.rng, self.rig.fake
        roll = r.random()
        if roll < 0.2:
            fake.fail_before_execution(*[r.choice([429, 500, 503]) for _ in range(r.randrange(1, 4))])
        elif roll < 0.4:
            fake.lose_response_after_execution(r.randrange(1, 5))
        fake.search_lag = r.random() < 0.4

    def clear_injection(self):
        self.rig.fake._before.clear()
        self.rig.fake._lose = 0
        self.rig.fake.search_lag = False

    # -- events, always built from what the provider really holds ---------------------------------------------------------------------------

    def emit(self, pi_id: str):
        fake, ev, r = self.rig.fake, self.rig.events, self.rng
        pi = fake.payment_intents.get(pi_id)
        if pi is None:
            return
        made = []
        if pi["status"] == "succeeded":
            made.append(ev.payment_intent("payment_intent.succeeded", pi))
            total = sum(x["amount"] for x in fake.refunds.values() if x["payment_intent"] == pi_id)
            if total:
                made.append(ev.charge_refunded(pi_id, pi["amount"], total, "aud"))
        elif pi.get("last_payment_error"):
            made.append(ev.payment_intent("payment_intent.payment_failed", pi))
        for e in made:
            body, header = ev.signed(e, timestamp=int(time.time()))
            self.pending.append((body, header))
            while r.random() < 0.3:                                        # the provider sends some twice
                self.pending.append((body, header))
            if r.random() < 0.2:                                           # and sometimes as a second event object for the same thing
                twin = dict(e, id=e["id"] + "_twin")
                self.pending.append(ev.signed(twin, timestamp=int(time.time())))

    async def deliver_some(self, everything=False):
        r = self.rng
        r.shuffle(self.pending)
        take = len(self.pending) if everything else r.randrange(0, len(self.pending) + 1)
        for body, header in [self.pending.pop() for _ in range(take)]:
            if r.random() < 0.1 and not everything:
                await self.tampered(body, header)
            try:
                await webhooks.receive(self.rig.db, self.rig.router, body, header, SECRETS, verify=self.rig.verify)
            except webhooks.WebhookUnroutable:
                if not everything:      # a refund that beat the payment row: the provider would send it again, so it goes back in the queue
                    self.pending.append((body, header))

    async def tampered(self, body, header):
        before = await self.snapshot()
        for bad_body, bad_header in ((body + b" ", header), (body, header.replace("v1=", "v1=0"))):
            code, _ = await webhooks.handle_http(self.rig.db, self.rig.router, bad_body, bad_header, SECRETS, verify=self.rig.verify)
            assert code == 400
        stale_body, stale_header = Events(SECRETS).signed(Events(SECRETS).event("payment_intent.succeeded", {"id": "x"}), timestamp=int(time.time()) - 5000)
        assert (await webhooks.handle_http(self.rig.db, self.rig.router, stale_body, stale_header, SECRETS, verify=self.rig.verify))[0] == 400
        assert await self.snapshot() == before

    async def snapshot(self):
        out = []
        for t in self.tenants:
            async with self.rig.db.tenant(t) as conn:
                out.append(tuple([await conn.fetchval("select count(*) from %s" % tb) for tb in ("billing_transaction", "billing_entry", "billing_webhook_event", "billing_payment")]))
        return out

    # -- steps ------------------------------------------------------------------------------------------------------------------------------------

    async def step(self):
        r = self.rng
        kind = r.choices(["topup", "retry_topup", "usage", "retry_usage", "burst", "refund", "adjust", "service", "deliver", "refresh", "reprocess"],
                         [24, 8, 16, 8, 5, 12, 5, 4, 14, 5, 3])[0]
        if len(self.payments) < 2 and self.steps_taken < 3:
            kind = "topup"                       # every world has money to move
        self.steps_taken += 1
        t = r.choice(self.tenants)
        if kind == "topup":
            amount, method = r.choice([500, 1000, 2500, 4000]), r.choice(["pm_fake_ok"] * 3 + ["pm_fake_declined"])
            key = "pay-%d" % len(self.payments)
            self.payments.append(dict(tenant=t, key=key, amount=amount, method=method))
            await self.ask(self.payments[-1])
        elif kind == "retry_topup" and self.payments:
            await self.ask(r.choice(self.payments))
        elif kind == "usage":
            await self.use(t, r.randrange(1, 12), "u-%s-%d" % (t[:4], len(self.used_keys[t])), new=True)
        elif kind == "retry_usage" and self.used_keys[t]:
            key, qty = r.choice(self.used_keys[t])
            await self.use(t, qty, key, new=False)
        elif kind == "burst":
            base = len(self.used_keys[t])
            qtys = [r.randrange(1, 8) for _ in range(5)]
            results = await asyncio.gather(*[self.use(t, q, "b-%s-%d-%d" % (t[:4], base, i), new=True) for i, q in enumerate(qtys)])
            assert results is not None
        elif kind == "refund":
            await self.refund()
        elif kind == "adjust":
            amount, key = r.choice([-300, -50, 100, 400, 25]), "adj-%s-%d" % (t[:4], r.randrange(10**6))
            async with self.rig.db.tenant(t) as conn:
                out = await wallet.adjust(conn, t, amount_minor=amount, currency=CURRENCY, key=key, actor="sim", reason="simulated correction")
            if out.status == "applied":
                self.applied_other[t] += amount
        elif kind == "service":
            amount, key = r.choice([10, 80]), "svc-%s-%d" % (t[:4], r.randrange(10**6))
            async with self.rig.db.tenant(t) as conn:
                out = await wallet.refund_usage(conn, t, amount_minor=amount, currency=CURRENCY, key=key, actor="sim", reason="simulated service credit")
            if out.status == "applied":
                self.applied_other[t] += amount
        elif kind == "deliver" and self.pending:
            await self.deliver_some()
        elif kind == "refresh" and self.payments:
            p = r.choice(self.payments)
            async with self.rig.db.tenant(p["tenant"]) as conn:
                row = await conn.fetchrow("select id, status from billing_payment where idempotency_key = $1", p["key"])
            if row is not None and row["status"] in ("unknown", "processing", "created"):
                self.clear_injection()
                try:
                    await payments.refresh_payment(self.rig.db, self.rig.provider, p["tenant"], str(row["id"]))
                except (ProviderError, BillingError):
                    pass
        elif kind == "reprocess":
            await webhooks.reprocess_held(self.rig.db, t)

    async def ask(self, p):
        self.inject()
        try:
            res = await payments.start_topup(self.rig.db, self.rig.provider, p["tenant"], amount_minor=p["amount"], currency=CURRENCY, payment_method=p["method"], key=p["key"])
        except (ProviderError, BillingError):
            return
        for pi_id in list(self.rig.fake.payment_intents):
            if res.status in ("succeeded", "failed") and self.rng.random() < 0.7:
                self.emit(pi_id)

    async def use(self, t, qty, key, new):
        if new:
            self.used_keys[t].append((key, qty))
        async with self.rig.db.tenant(t) as conn:
            out = await wallet.debit_usage(conn, t, event_type="sync_run", quantity=qty, currency=CURRENCY, key=key)
        if out.status == "applied":
            self.applied_usage[t] += out.amount_minor
        return out

    async def refund(self):
        r = self.rng
        ok = [p for p in self.payments if p["method"] == "pm_fake_ok"]
        if not ok:
            return
        p = r.choice(ok)
        async with self.rig.db.tenant(p["tenant"]) as conn:
            row = await conn.fetchrow("select id, status, credited_minor, refunded_minor from billing_payment where idempotency_key = $1", p["key"])
        if row is None or row["status"] != "succeeded":
            return
        left = row["credited_minor"] - row["refunded_minor"]
        if left <= 0:
            return
        self.inject()
        try:
            await payments.request_refund(self.rig.db, self.rig.provider, p["tenant"], str(row["id"]), amount_minor=r.randrange(1, left + 1), key="rf-%d" % r.randrange(10**6))
        except (ProviderError, BillingError):
            pass
        async with self.rig.db.tenant(p["tenant"]) as conn:
            pi_id = await conn.fetchval("select provider_payment_id from billing_payment where id = $1::uuid", str(row["id"]))
        self.emit(pi_id)

    # -- the rules ---------------------------------------------------------------------------------------------------------------------------------

    async def check_books(self):
        for t in self.tenants:
            async with self.rig.db.tenant(t) as conn:
                rec = await reports.reconcile(conn, t)
                assert rec.ok, [d for d in rec.discrepancies]
                assert await conn.fetchval("select coalesce(sum(balance_minor), 0) from billing_account where kind = 'wallet'") <= 0

    async def settle_and_check_end(self):
        self.clear_injection()
        for p in self.payments:                                   # ask again with the same key until the provider has answered
            for _ in range(3):
                res = await payments.start_topup(self.rig.db, self.rig.provider, p["tenant"], amount_minor=p["amount"], currency=CURRENCY, payment_method=p["method"], key=p["key"])
                if res.status in ("succeeded", "failed"):
                    break
                await payments.refresh_payment(self.rig.db, self.rig.provider, p["tenant"], res.payment_id)
        for pi_id in list(self.rig.fake.payment_intents):
            self.emit(pi_id)
        for _ in range(4):                                        # until nothing moves: a held refund waits for credit that a later event brings
            await self.deliver_some(everything=True)
            for t in self.tenants:
                await webhooks.reprocess_held(self.rig.db, t)
        await self.check_books()
        fake = self.rig.fake
        ok = [p for p in self.payments if p["method"] == "pm_fake_ok"]
        assert len(fake.charges) == len(ok) and fake.executions >= len(ok), "a retry, a repeat or a lost response made a second charge or lost one"
        for t in self.tenants:
            mine = [p for p in ok if p["tenant"] == t]
            async with self.rig.db.tenant(t) as conn:
                rows = {r["idempotency_key"]: r for r in await conn.fetch("select * from billing_payment")}
                reversed_total = sum(r["refunded_minor"] for r in rows.values())
                spend = -await conn.fetchval("select coalesce(sum(balance_minor), 0) from billing_account where kind = 'wallet'")
                topups = await conn.fetch("select provider_ref, count(*) n from billing_transaction where kind = 'topup' group by provider_ref")
                held = await conn.fetch("select payload from billing_webhook_event where status = 'held'")
            assert all(row["n"] == 1 for row in topups)
            for p in mine:
                assert rows[p["key"]]["status"] == "succeeded" and rows[p["key"]]["credited_minor"] == p["amount"], p
            for p in self.payments:
                if p["tenant"] == t and p["method"] != "pm_fake_ok":
                    assert rows[p["key"]]["credited_minor"] == 0 and rows[p["key"]]["status"] == "failed"
            credited = sum(p["amount"] for p in mine)
            assert spend == credited + self.applied_other[t] - self.applied_usage[t] - reversed_total, (t, spend, credited, self.applied_other[t], self.applied_usage[t], reversed_total)
            mine_refunds = sum(x["amount"] for x in fake.refunds.values() if any(r["provider_payment_id"] == x["payment_intent"] for r in rows.values()))
            assert mine_refunds >= reversed_total
            if mine_refunds > reversed_total:
                assert held, "the provider refunded more than we reversed and no event is held to say why"


# the hand-written client and verifier on every seed, and the official stripe SDK and its construct_event on the first five: the SDK retries inside itself, so a world
# that ends in an unknown outcome or a lost response is a different world, and rule 4 (one charge for each payment, each credited once) has to hold in it too
@pytest.mark.parametrize("seed, sdk", [(seed, False) for seed in SEEDS] + [(seed, True) for seed in range(5)])
def test_seeded_billing_traffic_keeps_every_invariant_after_every_step(seed, sdk):
    reset_billing_schema()
    tenants = new_tenants("sim-a-%d" % seed, "sim-b-%d" % seed)

    async def go(rig):
        rng = random.Random(seed * 7919 + 13)
        for t in tenants:
            await set_price(rig.db, t, "sync_run", 15)
        sim = Sim(rig, rng, tenants)
        for _ in range(STEPS):
            try:
                await sim.step()
            except LedgerInvariantViolation:
                raise AssertionError("the database refused a write the module should never have attempted")
            await sim.check_books()
        await sim.settle_and_check_end()
        return len(sim.payments)

    assert with_rig(go, size=8, sdk=sdk) >= 0
