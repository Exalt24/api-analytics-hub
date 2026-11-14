/**
 * Subdomain client portals: the host to tenant rule, the proxy that rewrites by host, and the
 * key check that a tenant's key opens only that tenant's portal. The first two groups need no
 * database; the last runs against the scratch database like the other row level security tests.
 */
import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import { NextRequest } from "next/server";
import { proxy } from "@/proxy";
import { RESERVED_LABELS, authorizePortal, readPortalSummary, tenantSlugFromHost } from "@/lib/portal";
import { createDb, pgDialect, type Db } from "@/lib/server/db";
import { KEY_A, KEY_A_REVOKED, KEY_B, TENANT_A, TENANT_B } from "./fixtures";

const BASE = "portal.example.com";

describe("tenantSlugFromHost", () => {
  it("takes exactly one label in front of the base domain", () => {
    expect(tenantSlugFromHost("alpha.portal.example.com", BASE)).toBe("alpha");
    expect(tenantSlugFromHost("bravo-bakery.portal.example.com", BASE)).toBe("bravo-bakery");
    expect(tenantSlugFromHost("a1.portal.example.com", BASE)).toBe("a1");
    expect(tenantSlugFromHost("x.portal.example.com", BASE)).toBe("x");
  });

  it("ignores case, a port and one trailing dot, and nothing else", () => {
    expect(tenantSlugFromHost("ALPHA.Portal.Example.COM", BASE)).toBe("alpha");
    expect(tenantSlugFromHost("alpha.portal.example.com:3000", BASE)).toBe("alpha");
    expect(tenantSlugFromHost("alpha.portal.example.com.", BASE)).toBe("alpha");
    expect(tenantSlugFromHost("alpha.portal.example.com..", BASE)).toBeNull();
    expect(tenantSlugFromHost("alpha.portal.example.com", `${BASE.toUpperCase()}.`)).toBe("alpha");
  });

  it("refuses every host that only looks like a portal", () => {
    const refused = [
      "portal.example.com", // the base itself
      "a.b.portal.example.com", // two labels
      "alpha.portal.example.com.evil.test", // base is not the end
      "alphaportal.example.com", // no dot before the base
      "evilportal.example.com",
      "alpha.example.com", // another suffix
      "alpha.portal.example.org",
      ".portal.example.com", // empty label
      "-alpha.portal.example.com",
      "alpha-.portal.example.com",
      "al_pha.portal.example.com",
      "alpha@evil.test.portal.example.com", // userinfo is not a host
      "alpha.portal.example.com/evil",
      "alpha.portal.example.com, evil.test",
      "[::1]:3000",
      "localhost",
      "127.0.0.1",
      "",
      " ",
      `${"a".repeat(64)}.portal.example.com`, // a label over 63
    ];
    for (const h of refused) expect(tenantSlugFromHost(h, BASE), JSON.stringify(h)).toBeNull();
    expect(tenantSlugFromHost(`${"a".repeat(63)}.portal.example.com`, BASE)).toBe("a".repeat(63));
  });

  it("never names a tenant for the product's own subdomains", () => {
    for (const label of RESERVED_LABELS) {
      expect(tenantSlugFromHost(`${label}.portal.example.com`, BASE), label).toBeNull();
    }
    expect(tenantSlugFromHost("www.portal.example.com", BASE)).toBeNull();
    expect(tenantSlugFromHost("wwwx.portal.example.com", BASE)).toBe("wwwx"); // positive control
  });

  it("is off when no base domain is set, and when no host arrives", () => {
    expect(tenantSlugFromHost("alpha.portal.example.com", undefined)).toBeNull();
    expect(tenantSlugFromHost("alpha.portal.example.com", "")).toBeNull();
    expect(tenantSlugFromHost(null, BASE)).toBeNull();
    expect(tenantSlugFromHost(undefined, BASE)).toBeNull();
  });
});

function request(host: string, path = "/") {
  return new NextRequest(`http://internal.test${path}`, { headers: { host } });
}
const rewrittenTo = (res: Response) => res.headers.get("x-middleware-rewrite");
const passedThrough = (res: Response) => res.headers.get("x-middleware-next") === "1";

describe("the proxy", () => {
  beforeAll(() => {
    process.env.PORTAL_BASE_DOMAIN = BASE;
  });
  afterAll(() => {
    delete process.env.PORTAL_BASE_DOMAIN;
  });

  it("rewrites a client's subdomain to that client's portal route, keeping the path", () => {
    expect(rewrittenTo(proxy(request("alpha.portal.example.com")))).toMatch(/\/portal\/alpha$/);
    expect(rewrittenTo(proxy(request("bravo.portal.example.com", "/")))).toMatch(/\/portal\/bravo$/);
    expect(rewrittenTo(proxy(request("alpha.portal.example.com", "/files/q3")))).toMatch(/\/portal\/alpha\/files\/q3$/);
  });

  it("takes the tenant from the host and never from the path", () => {
    const res = proxy(request("alpha.portal.example.com", "/files/bravo"));
    expect(rewrittenTo(res)).toMatch(/\/portal\/alpha\/files\/bravo$/);
    expect(rewrittenTo(res)).not.toMatch(/\/portal\/bravo/);
  });

  it("refuses /portal asked for from outside on every host, so a path cannot pick a tenant", async () => {
    for (const host of ["alpha.portal.example.com", "portal.example.com", "example.com", "evil.test"]) {
      for (const path of ["/portal/bravo", "/portal", "/portal/alpha/x"]) {
        const res = proxy(request(host, path));
        expect(res.status, `${host}${path}`).toBe(404);
        expect(rewrittenTo(res)).toBeNull();
      }
    }
  });

  it("passes everything else through untouched", () => {
    for (const host of ["portal.example.com", "www.portal.example.com", "alpha.portal.example.com.evil.test", "localhost:3000"]) {
      expect(passedThrough(proxy(request(host, "/reports"))), host).toBe(true);
    }
  });

  it("does nothing while PORTAL_BASE_DOMAIN is unset", () => {
    delete process.env.PORTAL_BASE_DOMAIN;
    try {
      expect(passedThrough(proxy(request("alpha.portal.example.com")))).toBe(true);
    } finally {
      process.env.PORTAL_BASE_DOMAIN = BASE;
    }
  });
});

const adminDsn = inject("adminDsn");
const run = describe.skipIf(!adminDsn);

run("a key opens only its own tenant's portal", () => {
  let db: Db;
  beforeAll(() => {
    db = createDb(pgDialect(adminDsn, 4));
  });
  afterAll(async () => {
    await db.destroy();
  });

  it("opens Alpha's portal with Alpha's key and Bravo's with Bravo's", async () => {
    const a = await authorizePortal(db, "alpha", KEY_A);
    expect(a.ok && [a.tenant.id, a.tenant.name, a.principal.role]).toEqual([TENANT_A, "Alpha Outfitters", "viewer"]);
    const b = await authorizePortal(db, "bravo", KEY_B);
    expect(b.ok && [b.tenant.id, b.tenant.name, b.principal.role]).toEqual([TENANT_B, "Bravo Bakery", "operator"]);
  });

  it("matches the slug exactly: another case, a lookalike or an injection string opens nothing", async () => {
    for (const slug of ["ALPHA", "alph", "alpha ", "alpha' or '1'='1", "bravo", ""]) {
      expect(await authorizePortal(db, slug, KEY_A), JSON.stringify(slug)).toMatchObject({ ok: false, status: 403 });
    }
  });

  it("refuses Bravo's key at Alpha's portal and Alpha's key at Bravo's, with a 403", async () => {
    expect(await authorizePortal(db, "alpha", KEY_B)).toMatchObject({ ok: false, status: 403 });
    expect(await authorizePortal(db, "bravo", KEY_A)).toMatchObject({ ok: false, status: 403 });
  });

  it("refuses a missing, wrong or revoked key with a 401", async () => {
    expect(await authorizePortal(db, "alpha", "")).toMatchObject({ ok: false, status: 401 });
    expect(await authorizePortal(db, "alpha", "not-a-key")).toMatchObject({ ok: false, status: 401 });
    expect(await authorizePortal(db, "alpha", KEY_A_REVOKED)).toMatchObject({ ok: false, status: 401 });
  });

  it("answers a subdomain no tenant owns exactly like another tenant's, so existence is not leaked", async () => {
    const other = await authorizePortal(db, "bravo", KEY_A);
    const nobody = await authorizePortal(db, "nobody", KEY_A);
    expect(nobody).toEqual(other);
  });

  it("counts only the signed in tenant's figures", async () => {
    const a = await readPortalSummary(db, TENANT_A);
    const b = await readPortalSummary(db, TENANT_B);
    expect(a).toEqual({ snapshots: 15, latestDay: "2026-11-01" }); // the fixture rows of tenant A
    expect(b).toEqual({ snapshots: 3, latestDay: "2026-10-02" }); // the fixture rows of tenant B
  });
});
