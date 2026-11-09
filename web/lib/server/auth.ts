import { createHash } from "node:crypto";
import { unscoped, type Db } from "@/lib/server/db";

export type Role = "viewer" | "operator" | "admin";

export type Principal = { tenantId: string; role: Role; keyId: string };

/** Every role app/api/auth.py's PERMISSIONS gives `read:report`. An unknown role is refused. */
const MAY_READ_REPORT: ReadonlySet<string> = new Set(["viewer", "operator", "admin"]);

/** SHA-256 hex of the key, the same digest app/api/auth.py stores in api_key.key_hash. */
export function hashKey(raw: string): string {
  return createHash("sha256").update(raw).digest("hex");
}

export class AuthError extends Error {
  constructor(
    readonly status: 401 | 403,
    message: string,
  ) {
    super(message);
    this.name = "AuthError";
  }
}

/**
 * Turn an API key into a tenant and a role. The lookup is by hash of a 256 bit secret
 * and happens before any tenant is known, which is why api_key has no row level
 * security (the note on its table in migrations/003 says so). A revoked key and an
 * unknown role both fail closed.
 */
export async function resolvePrincipal(db: Db, rawKey: string): Promise<Principal> {
  if (!rawKey) throw new AuthError(401, "missing API key");
  const digest = hashKey(rawKey);
  const row = await unscoped(db, (trx) =>
    trx
      .selectFrom("api_key")
      .select(["id", "tenant_id", "role", "revoked_at"])
      .where("key_hash", "=", digest)
      .executeTakeFirst(),
  );
  if (!row) throw new AuthError(401, "invalid API key");
  if (row.revoked_at !== null) throw new AuthError(401, "API key revoked");
  if (!MAY_READ_REPORT.has(row.role)) throw new AuthError(403, "unknown role");
  return { tenantId: row.tenant_id, role: row.role, keyId: row.id };
}
