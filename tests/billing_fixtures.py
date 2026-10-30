"""Shared setup for the billing tests: a fresh schema with 001, 002, 011 and 012, two tenants, the two demo plans, and a Database for each role.

Like the other database suites this DROPS and recreates the `public` schema of the database it points at (ANALYTICS_TEST_DSN, default the compose
Postgres on 5433), so aim it at a scratch database. It applies the migrations billing needs and not pgvector. `db` connects as the request role through
`Database`, which refuses to start as a superuser; `router` connects as billing_router, the narrow role that finds the owner of a provider event.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from price_watch_fixtures import DSN, add_tenant, needs_db, psycopg, reset_schema, run  # noqa: E402,F401

from app.billing.demo import CURRENCY, PLANS, SECRETS  # noqa: E402,F401
from app.billing.fake_provider import FAKE_API_KEY, Events, FakeStripe  # noqa: E402,F401
from app.billing.provider import StripeStyleClient  # noqa: E402
from app.db import Database  # noqa: E402


def reset_billing_schema() -> None:
    reset_schema(("012_billing.sql",))
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        for p in PLANS:
            cur.execute("insert into billing_plan (code, name, currency, monthly_price_minor, included_credits_minor, provider_price_id) values (%s, %s, %s, %s, %s, %s)",
                        (p["code"], p["name"], p["currency"], p["monthly_price_minor"], p["included_credits_minor"], p["provider_price_id"]))


class Rig:
    """One connected request-role Database, one router Database, a fake provider with its client, and a signed-event builder."""

    def __init__(self, db: Database, router: Database):
        self.db, self.router = db, router
        self.fake = FakeStripe()
        self.http = self.fake.client()
        self.sleeps: list[float] = []
        self.provider = StripeStyleClient(self.http, FAKE_API_KEY, sleep=self._sleep)
        self.events = Events(SECRETS)

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    async def close(self) -> None:
        await self.http.aclose()
        await self.router.close()
        await self.db.close()


def with_rig(fn, *, size: int = 4):
    """Run `fn(rig)` with connected databases and a fake provider, closing everything after."""
    async def go():
        db, router = Database(DSN, max_size=size), Database(DSN, max_size=2, role="billing_router")
        await db.connect()
        await router.connect()
        rig = Rig(db, router)
        try:
            return await fn(rig)
        finally:
            await rig.close()
    return run(go())


def new_tenants(*slugs: str) -> list[str]:
    return [str(add_tenant(s)) for s in slugs]


async def set_price(db: Database, tenant: str, event_type: str = "sync_run", unit: int = 25, currency: str = CURRENCY) -> None:
    async with db.tenant(tenant) as conn:
        await conn.execute("insert into billing_usage_price (tenant_id, event_type, currency, unit_price_minor) values ($1::uuid, $2, $3, $4) "
                           "on conflict (tenant_id, event_type, currency) do update set unit_price_minor = excluded.unit_price_minor", tenant, event_type, currency, unit)


async def counts(db: Database, tenant: str) -> dict[str, int]:
    out = {}
    async with db.tenant(tenant) as conn:
        for t in ("billing_transaction", "billing_entry", "billing_usage_event", "billing_payment", "billing_webhook_event", "billing_notification"):
            out[t] = await conn.fetchval("select count(*) from %s" % t)
        out["spendable"] = -(await conn.fetchval("select coalesce(sum(balance_minor), 0) from billing_account where kind = 'wallet'"))
    return out


async def topup_via_provider(rig: Rig, tenant: str, amount: int, key: str, *, method: str = "pm_fake_ok", deliver: bool = True, created: int | None = None):
    """Ask the fake provider for money and deliver its signed succeeded event. Returns (PaymentResult, Received or None)."""
    from app.billing import payments, webhooks
    res = await payments.start_topup(rig.db, rig.provider, tenant, amount_minor=amount, currency=CURRENCY, payment_method=method, key=key)
    got = None
    if deliver and res.status == "succeeded":
        event = rig.events.payment_intent("payment_intent.succeeded", rig.fake.payment_intents[res.provider_payment_id], **({"created": created} if created else {}))
        body, header = rig.events.signed(event)
        got = await webhooks.receive(rig.db, rig.router, body, header, SECRETS)
    return res, got


__all__ = ["DSN", "needs_db", "psycopg", "run", "uuid"]
