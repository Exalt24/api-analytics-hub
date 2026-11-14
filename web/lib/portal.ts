/**
 * Subdomain client portals: `alpha.portal.example.com` is Alpha's portal, and only Alpha's.
 *
 * THE HOST DECIDES WHICH PORTAL, THE KEY DECIDES WHO MAY ENTER. The host header picks a
 * tenant slug (`tenantSlugFromHost`), and a viewer key is accepted there only when the key
 * belongs to that same tenant (`authorizePortal`). A key for tenant B typed into tenant A's
 * portal is refused with 403, so a client who learns another client's subdomain gets a
 * sign-in page and nothing else. Data is then read through `withTenant`, so the row level
 * security from migrations/001 does the filtering and this file never writes a tenant
 * WHERE clause.
 */
import { sql } from "kysely";
import { resolvePrincipal, type Principal } from "@/lib/server/auth";
import { withTenant, type Db } from "@/lib/server/db";

/** The sign-in cookie. Host only, so one portal's key is never sent to another subdomain. */
export const PORTAL_COOKIE = "portal_key";

/** Subdomains that are the product's own, never a client's portal. */
export const RESERVED_LABELS: ReadonlySet<string> = new Set([
  "www",
  "api",
  "admin",
  "app",
  "portal",
  "mail",
  "static",
  "assets",
]);

/** One DNS label: letters, digits and inner hyphens, 63 characters at most. */
const LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

/** Lower case, one trailing dot and the port removed. Whatever else is in it is refused later by the one label rule. */
function normaliseHost(raw: string): string {
  let h = raw.trim().toLowerCase();
  h = h.replace(/:\d{1,5}$/, "");
  if (h.endsWith(".")) h = h.slice(0, -1);
  return h;
}

/**
 * The tenant slug a Host header names, or null. Exactly one label in front of the base
 * domain: `alpha.portal.example.com` is `alpha`, while `portal.example.com`,
 * `a.b.portal.example.com`, `alpha.portal.example.com.evil.test` and `evilportal.example.com`
 * are all null. An empty or missing base domain turns portals off.
 */
export function tenantSlugFromHost(host: string | null | undefined, baseDomain: string | undefined): string | null {
  if (!host || !baseDomain) return null;
  const base = normaliseHost(baseDomain);
  const h = normaliseHost(host);
  if (!base || !h) return null;
  const suffix = `.${base}`;
  if (!h.endsWith(suffix)) return null;
  const label = h.slice(0, -suffix.length);
  if (!LABEL.test(label) || RESERVED_LABELS.has(label)) return null;
  return label;
}

export type PortalTenant = { id: string; name: string };

export type PortalAccess =
  | { ok: true; tenant: PortalTenant; principal: Principal }
  | { ok: false; status: 401 | 403; message: string };

/**
 * Open the portal for `slug` with a key. The key is resolved first (api_key is the one table
 * read before a tenant is known), then the tenant's own row is read with that tenant set, where
 * the `tenant` table's forced row level security shows it only its own row. The slug on that row
 * must equal the host's slug. So a bad key is 401, and a valid key at another tenant's portal, or
 * at a subdomain no tenant owns, is the same 403: the portal never says which subdomains exist.
 */
export async function authorizePortal(db: Db, slug: string, rawKey: string): Promise<PortalAccess> {
  let principal: Principal;
  try {
    principal = await resolvePrincipal(db, rawKey);
  } catch (err) {
    const status = (err as { status?: number }).status === 403 ? 403 : 401;
    return { ok: false, status, message: err instanceof Error ? err.message : "not signed in" };
  }
  const tenant = await withTenant(db, principal.tenantId, (trx) =>
    trx.selectFrom("tenant").select(["id", "name", "slug"]).executeTakeFirst(),
  );
  if (!tenant || tenant.slug !== slug) {
    return { ok: false, status: 403, message: "this key does not open this portal" };
  }
  return { ok: true, tenant: { id: tenant.id, name: tenant.name }, principal };
}

export type PortalSummary = { snapshots: number; latestDay: string | null };

/** What the portal home shows: read with the tenant set, so only this tenant's rows are counted. */
export async function readPortalSummary(db: Db, tenantId: string): Promise<PortalSummary> {
  return withTenant(db, tenantId, async (trx) => {
    const row = await trx
      .selectFrom("metric_snapshot")
      .select((eb) => [eb.fn.countAll<string>().as("n"), sql<string | null>`max(observed_on)::text`.as("latest")])
      .executeTakeFirst();
    return { snapshots: Number(row?.n ?? 0), latestDay: row?.latest ?? null };
  });
}
