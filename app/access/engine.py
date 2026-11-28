"""Two small tools for code that runs money and state changes on behalf of a portal user.

ActingDatabase. The billing engine opens its transactions with `db.tenant(tenant_id)`, which sets the tenant and nothing else, so a portal user's payment would run with no role. An
ActingDatabase wraps a Database and answers `tenant()` with `Database.session(tenant, role, user)`: the engine is unchanged and every statement it runs is filtered by the matrix for
that user. The API layer hands the engine an ActingDatabase built from the caller's credential.

engine_actor. Some steps follow an action that was already authorised and must write rows the user may not write: the commission that a paid invoice earns, the payment row a provider
event settles. The engine runs those inside `engine_actor`, which sets the role to admin and the user to a fixed system id for the length of the block and then puts the caller's own
settings back. It is a visible, narrow elevation inside our own code, used in one place for each such step; a request role can never reach it from SQL because it is not a function in the database.
"""
from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

import asyncpg

from app.db import Database

#: the id the engine acts under when a provider event (no person) moves an invoice or a commission
SYSTEM_USER_ID = "00000000-0000-0000-0000-000000000000"


class ActingDatabase:
    """A Database that opens every tenant transaction as one portal user. It has the two methods the billing engine calls."""

    def __init__(self, db: Database, role: str, user_id: str):
        if not role or not user_id:
            raise ValueError("an acting database needs a role and a user")
        self._db, self.role, self.user_id = db, role, user_id

    @property
    def engine(self) -> Database:
        """The Database underneath, for the steps that are the engine's own and not the user's (settling a payment from the provider's answer)."""
        return self._db

    def tenant(self, tenant_id: str):
        return self._db.session(tenant_id, self.role, self.user_id)

    def unscoped(self):
        return self._db.unscoped()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


@contextlib.asynccontextmanager
async def engine_actor(conn: asyncpg.Connection) -> AsyncIterator[None]:
    """Run the block as admin and the system user, on this transaction, and restore the caller's role and user afterwards (also when the block raises)."""
    before = await conn.fetchrow("select current_setting('app.role', true) as role, current_setting('app.user_id', true) as uid")
    await conn.execute("select set_config('app.role', 'admin', true), set_config('app.user_id', $1, true)", SYSTEM_USER_ID)
    try:
        yield
    finally:
        await conn.execute("select set_config('app.role', $1, true), set_config('app.user_id', $2, true)", before["role"] or "", before["uid"] or "")
