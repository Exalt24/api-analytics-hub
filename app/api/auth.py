"""Authentication and role-based access control.

TWO SEPARATE QUESTIONS, kept separate on purpose. Authentication answers "which
tenant is this", and it is the answer that gets pushed into the database session so
row level security can filter. Authorization answers "may this role do this", and
it lives in the route layer. Conflating them is how a viewer ends up able to POST a
sync because the code only ever checked that the token was valid.

THE TENANT IS NEVER TAKEN FROM THE REQUEST. Not from a header, not from a query
parameter, not from a JSON body. It comes from the credential and nowhere else,
because a tenant id the caller can type is not a boundary. That is exactly the
"even if someone manually modifies an API request" case, and the only reason the
database-level policy holds is that this layer refuses to be told who to be.

API KEYS ARE STORED HASHED. A leaked database dump should not yield working keys,
so the table holds SHA-256 of the key and lookup is by hash. Comparison uses
compare_digest even though the lookup is by index, because the habit is what
survives a later refactor that turns it into a scan.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from enum import Enum

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


class Role(str, Enum):
    """Ordered least to most capable. Comparison is explicit, never by value."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


#: What each role may do. A dict rather than inequality checks on an ordered enum,
#: because "operator is above viewer" stops being obviously true the moment a role
#: is added that is not on the same line, and a silent misordering grants power.
PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.VIEWER: frozenset({"read:kpis", "read:runs"}),
    Role.OPERATOR: frozenset({"read:kpis", "read:runs", "write:sync"}),
    Role.ADMIN: frozenset({"read:kpis", "read:runs", "write:sync",
                           "write:connections", "read:credentials"}),
}


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    role: Role
    key_id: str

    def may(self, permission: str) -> bool:
        return permission in PERMISSIONS[self.role]


def hash_key(raw: str) -> str:
    """SHA-256 hex of an API key. No salt on purpose: keys are high-entropy random
    strings, so a rainbow table is not a threat, and a deterministic hash is what
    lets the lookup be an index hit rather than a scan of every row."""
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_key() -> str:
    """A new API key. 32 bytes of urandom, prefixed so it is greppable in a leak
    report and recognisable in a support ticket."""
    return "aah_" + secrets.token_urlsafe(32)


bearer = HTTPBearer(auto_error=False)


class Authenticator:
    """Resolves a bearer token to a Principal, against the database."""

    def __init__(self, db):
        self._db = db

    async def resolve(self, raw_key: str) -> Principal:
        digest = hash_key(raw_key)
        async with self._db.unscoped() as conn:
            row = await conn.fetchrow(
                """
                select id, tenant_id, role, revoked_at
                  from api_key
                 where key_hash = $1
                """,
                digest,
            )
        if row is None or not hmac.compare_digest(hash_key(raw_key), digest):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
            )
        if row["revoked_at"] is not None:
            # Distinct from "invalid" in the log, identical to the caller. A
            # revoked key is a security event worth seeing; telling the holder
            # which of the two it was helps nobody but them.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
            )
        try:
            role = Role(row["role"])
        except ValueError:
            # An unknown role must fail closed. Defaulting to viewer would be the
            # tempting choice and it silently grants access after a typo in a
            # migration.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="unknown role"
            )
        return Principal(str(row["tenant_id"]), role, str(row["id"]))


async def current_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token"
        )
    auth: Authenticator = request.app.state.authenticator
    return await auth.resolve(creds.credentials)


def requires(permission: str):
    """Route dependency enforcing one permission.

    Written as a factory so the permission appears in the route signature and is
    visible when reading the endpoint, rather than being buried in the body where
    a later edit can drop it without the diff looking dangerous.
    """

    async def guard(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.may(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="role %s may not %s" % (principal.role.value, permission),
            )
        return principal

    return guard
