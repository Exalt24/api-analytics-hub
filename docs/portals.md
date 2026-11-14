# Subdomain client portals

`alpha.<PORTAL_BASE_DOMAIN>` is Alpha's portal and only Alpha's. The host picks the tenant, a key proves who may enter, and the database does the filtering.

## How a request travels

1. `web/proxy.ts` reads the Host header. `tenantSlugFromHost` (`web/lib/portal.ts`) accepts exactly one DNS label in front of the base domain and refuses the product's own labels (`www`, `api`, `admin`, `app`, `portal`, `mail`, `static`, `assets`).
2. A matching host is rewritten to the internal route `/portal/<slug>`. The browser keeps the subdomain URL. Any other host passes through untouched, and with `PORTAL_BASE_DOMAIN` unset the portals are off.
3. `/portal/...` asked for from outside is a 404 on every host, so a path can never pick a tenant. The rewrite is internal and does not pass through the proxy again.
4. The page reads the sign-in cookie. `authorizePortal` resolves the key first (`api_key` is the one table read before a tenant is known), then reads the tenant's own row with that tenant set, where the forced row level security on `tenant` shows it only its own row. The slug on that row must equal the host's slug.
5. The page then reads its figures through `withTenant`, so nothing in this path writes a `WHERE tenant_id`.

## What each failure looks like

| Case | Result |
|------|--------|
| No key, wrong key, revoked key | 401, the sign-in form |
| Alpha's key at Bravo's portal | the same refusal, no tenant name, no figures |
| A valid key at a subdomain no tenant owns | exactly the same refusal, so the portal never says which subdomains exist |
| `/portal/bravo` typed on any host | 404 |
| `alpha.portal.example.com.evil.test`, `evilportal.example.com`, `a.b.portal.example.com` | not a portal, the ordinary pages |

The sign-in cookie has no Domain attribute, so the browser sends it only to the subdomain that set it.

## Tests

Run on 2026-11-14 against a throwaway Postgres 17 with pgvector (the scratch database of the web suite, fixture tenants alpha and bravo) and a built `next start`:

| Part | Count |
|------|-------|
| Host to slug rule (about 25 hosts that must be refused, the reserved labels, case, port, trailing dot) | 5 tests |
| The proxy through `NextRequest` (rewrite keeps the path, the tenant comes from the host, `/portal` is a 404 on four hosts, lookalike hosts pass through, off when unset) | 5 tests |
| The key check against Postgres (own key opens, cross key is 403, 401 cases, unknown subdomain answers like another tenant's, exact slug match, figures counted per tenant) | 6 tests |
| A live server driven over HTTP with different Host headers (`web/scripts/portal-e2e.mjs`) | 14 checks |
| Deliberate breakages of the portal and proxy code, each turning a test red (`node scripts/mutation_check.mjs --only portal,proxy`) | 8 of 8 caught |

## Limits

- Built and run on `*.portal.example.com` names sent as Host headers to a local server. No wildcard DNS record, no wildcard certificate and no deployed portal were used, so the DNS and TLS side is not shown here.
- The portal home shows the tenant's name, the key's role and a count of stored figures. It is the isolation path, not a finished client area.
- A viewer key is typed into a form and kept in a host-only cookie. There is no password, SSO or session expiry.
