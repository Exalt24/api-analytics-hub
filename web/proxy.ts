import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { tenantSlugFromHost } from "@/lib/portal";

/**
 * Subdomain client portals. A request to `alpha.<PORTAL_BASE_DOMAIN>` is rewritten to the
 * internal route `/portal/alpha/...`; the browser keeps the subdomain URL.
 *
 * Two rules keep the route honest:
 * 1. Only a host that names a tenant reaches `/portal/<slug>`. The slug comes from the Host
 *    header here, never from the URL, so `/portal/bravo` typed on Alpha's host is refused.
 * 2. `/portal/...` asked for from outside is a 404 on every host. The rewrite below is
 *    internal and does not pass through this function again, so nothing legitimate is lost.
 *
 * With PORTAL_BASE_DOMAIN unset the portals are off and every request passes through.
 */
export function proxy(request: NextRequest) {
  const base = process.env.PORTAL_BASE_DOMAIN;
  const path = request.nextUrl.pathname;

  if (path === "/portal" || path.startsWith("/portal/")) {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }

  const slug = tenantSlugFromHost(request.headers.get("host"), base);
  if (!slug) return NextResponse.next();

  const url = request.nextUrl.clone();
  url.pathname = `/portal/${slug}${path === "/" ? "" : path}`;
  return NextResponse.rewrite(url);
}

export const config = {
  // Pages only: the framework's own files and the API routes are never portal pages.
  matcher: ["/((?!_next/|api/|favicon.ico).*)"],
};
