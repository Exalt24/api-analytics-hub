"""Who is asking, and the API layer of the access matrix: the first of the two places it is enforced (the second is row level security, migration 022 onwards).

THE CREDENTIAL IS THE ONLY SOURCE OF WHO. A portal user's API key is looked up by its SHA-256 hash (portal_resolve_credential), and the tenant, the user, the role and the clinic come back from the
database. Nothing about who the caller is is read from a header, a query parameter or a body, so a request that names another tenant, another clinic or another role changes nothing.

`requires_access(resource, action)` is a route dependency, as `requires(permission)` is for the older roles: it looks the cell up in app/access/matrix.py (the one table) BEFORE the route body
runs and answers 403 for a role that holds `none` there. Passing it is only the first check: the route then opens its transactions as this user (`Principal.acting`), so the row level security
policies, which read the same table, filter and refuse every statement again. A route that forgot the dependency would still be refused by the database, and a database policy that was
loosened would still be refused by the route.

An unknown role (a typo in a migration, a role the table has no row for) fails closed: 403, never a default. A missing, unknown or revoked key is 401 with one answer for all three.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import matrix
from .engine import ActingDatabase

bearer = HTTPBearer(auto_error=False)


def hash_key(raw: str) -> str:
    """SHA-256 hex of a portal API key; keys are high entropy, so no salt (the lookup is an index hit), as for api_key."""
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class PortalPrincipal:
    tenant_id: str
    user_id: str
    role: str
    clinic_id: str | None

    def may(self, resource: str, action: str) -> bool:
        return matrix.allows(self.role, resource, action)

    def acting(self, db) -> ActingDatabase:
        """The database opened as this user: every transaction carries their tenant, role and id, so row level security applies to everything the route does."""
        return ActingDatabase(db, self.role, self.user_id)


class PortalAuthenticator:
    """Resolves a bearer token to a PortalPrincipal against the database."""

    def __init__(self, db):
        self._db = db

    async def resolve(self, raw_key: str) -> PortalPrincipal:
        async with self._db.unscoped() as conn:
            row = await conn.fetchrow("select * from portal_resolve_credential($1)", hash_key(raw_key))
        if row is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
        if row["role"] not in matrix.ROLES:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="unknown role")
        return PortalPrincipal(str(row["tenant_id"]), str(row["user_id"]), row["role"], str(row["clinic_id"]) if row["clinic_id"] else None)


async def current_portal_principal(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> PortalPrincipal:
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    principal = await request.app.state.portal.authenticator.resolve(creds.credentials)
    request.state.tenant_id = principal.tenant_id          # read by the request log line: an opaque uuid, never the token
    return principal


def requires_access(resource: str, action: str):
    """Route dependency enforcing one cell of the matrix. A factory, so the cell is visible in the route's signature."""
    if resource not in matrix.RESOURCES or action not in matrix.ACTIONS:
        raise ValueError("no such cell: %s %s" % (resource, action))

    async def guard(principal: PortalPrincipal = Depends(current_portal_principal)) -> PortalPrincipal:
        if not principal.may(resource, action):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="role %s may not %s %s" % (principal.role, action, resource))
        return principal

    guard.cell = (resource, action)         # read by tests/test_access_api_db.py, which lists the cell each route is meant to ask for
    return guard
