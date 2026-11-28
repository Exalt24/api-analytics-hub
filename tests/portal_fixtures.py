# LIFESPAN: permanent (the shared setup of the ordering portal tests: bank debits, invoices, commissions, the access matrix and partner fulfilment)
"""Shared setup for the ordering portal database tests: the billing schema with migrations 022 to 024 on it, two tenants, and in each a small portal: three clinics, a clinic user for
two of them, a prescriber, two sales reps (one covers two clinics, one covers the third) and an admin, each with a hashed API key.

Everything here is invented: clinic codes, user labels and keys. Like the other database suites this DROPS and recreates the `public` schema of the database it points at
(ANALYTICS_TEST_DSN, default the compose Postgres on 5433), so aim it at a scratch database.

`PortalRig` is the billing `Rig` (the fake Stripe, the signed event builder, the request role `db` and the narrow `router`) plus the fulfilment router and a helper that opens the
billing engine as one portal user (`acting`). Seeding is done as the owner, which is how an admin tool would do it: the request role cannot write portal_credential at all.
"""
from __future__ import annotations

import hashlib
import secrets
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from billing_fixtures import (CURRENCY, DSN, SECRETS, MOCK_API_KEY, STRIPE_MOCK_URL, Rig, add_tenant, needs_db, needs_stripe_mock, psycopg, reset_billing_schema,  # noqa: E402,F401
                              run, stripe_mock_reachable)

from app.access.engine import ActingDatabase  # noqa: E402
from app.db import Database  # noqa: E402

#: bank debits are in US dollars only, so the portal tests work in USD
USD = "USD"
ACH_PM = "pm_fake_ach_ok"
CUSTOMER = "cus_fake_clinic_account"
MANDATE = "mandate_fake_accepted"


def key_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class Portal:
    """One tenant's portal: ids by name. `clinics` c1 c2 c3; `users` by label; `keys` by label (the raw API keys, for the HTTP tests)."""

    def __init__(self, tenant: str):
        self.tenant = tenant
        self.clinics: dict[str, str] = {}
        self.users: dict[str, str] = {}
        self.keys: dict[str, str] = {}

    def user(self, label: str) -> str:
        return self.users[label]


def seed_portal(slug: str, *, rep_rates: tuple[int, int] = (500, 1000)) -> Portal:
    """A portal for one new tenant. rep1 covers c1 at rep_rates[0] and c2 at rep_rates[1] basis points; rep2 covers c3 at 250; c1 and c2 have a clinic user, c1 also a prescriber."""
    tenant = str(add_tenant(slug))
    p = Portal(tenant)
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        for code in ("c1", "c2", "c3"):
            cur.execute("insert into clinic (tenant_id, code, name) values (%s, %s, %s) returning id", (tenant, "%s-%s" % (slug, code), "Clinic %s" % code))
            p.clinics[code] = str(cur.fetchone()[0])
        for label, role, clinic in (("clinic1", "clinic", "c1"), ("clinic2", "clinic", "c2"), ("prescriber1", "prescriber", "c1"), ("rep1", "sales_rep", None),
                                    ("rep2", "sales_rep", None), ("admin", "admin", None)):
            cur.execute("insert into portal_user (tenant_id, role, clinic_id, label) values (%s, %s, %s, %s) returning id", (tenant, role, p.clinics[clinic] if clinic else None, label))
            p.users[label] = str(cur.fetchone()[0])
            raw = "pk_test_" + secrets.token_urlsafe(24)
            p.keys[label] = raw
            cur.execute("insert into portal_credential (tenant_id, user_id, key_hash) values (%s, %s, %s)", (tenant, p.users[label], key_hash(raw)))
        for rep, clinic, bp in (("rep1", "c1", rep_rates[0]), ("rep1", "c2", rep_rates[1]), ("rep2", "c3", 250)):
            cur.execute("insert into sales_rep_assignment (tenant_id, rep_user_id, clinic_id, commission_bp) values (%s, %s, %s, %s)", (tenant, p.users[rep], p.clinics[clinic], bp))
    return p


def reset_world(*slugs: str) -> list[Portal]:
    reset_billing_schema()
    return [seed_portal(s) for s in slugs]


class PortalRig(Rig):
    def __init__(self, db: Database, router: Database, frouter: Database, sdk: bool = False):
        super().__init__(db, router, sdk)
        self.frouter = frouter

    async def close(self) -> None:
        await self.frouter.close()
        await super().close()

    def acting(self, portal: Portal, label: str) -> ActingDatabase:
        """The request role, opened as one of the portal's users: every transaction the billing or fulfilment code opens through it carries that user's role and id."""
        role = {"clinic1": "clinic", "clinic2": "clinic", "prescriber1": "prescriber", "rep1": "sales_rep", "rep2": "sales_rep", "admin": "admin"}[label]
        return ActingDatabase(self.db, role, portal.user(label))


def with_portal(fn, *, size: int = 6, sdk: bool = False):
    """Run `fn(rig)` with connected databases (request role, billing router, fulfilment router) and a fake provider, closing everything after."""
    async def go():
        db, router, frouter = Database(DSN, max_size=size), Database(DSN, max_size=2, role="billing_router"), Database(DSN, max_size=2, role="fulfillment_router")
        await db.connect()
        await router.connect()
        await frouter.connect()
        rig = PortalRig(db, router, frouter, sdk)
        try:
            return await fn(rig)
        finally:
            await rig.close()
    return run(go())


async def as_user(rig: PortalRig, portal: Portal, label: str, sql: str, *args):
    """One statement as a portal user, on a transaction that is rolled back if it raises. Returns the rows."""
    async with rig.acting(portal, label).tenant(portal.tenant) as conn:
        return await conn.fetch(sql, *args)


async def as_owner_count(sql: str, *args) -> int:
    """A count taken as the schema owner (which bypasses row level security): what is really in the table, whatever a role can see."""
    import asyncpg
    conn = await asyncpg.connect(DSN)
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


def new_key() -> str:
    return "k-%s" % uuid.uuid4()
