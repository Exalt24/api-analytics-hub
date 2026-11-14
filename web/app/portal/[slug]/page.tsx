import type { Metadata } from "next";
import { cookies } from "next/headers";
import { PORTAL_COOKIE, authorizePortal, readPortalSummary } from "@/lib/portal";
import { getDb } from "@/lib/server/db";
import { portalSignIn, portalSignOut } from "./actions";

export const metadata: Metadata = { title: "Client portal" };
export const dynamic = "force-dynamic";

/**
 * One client's portal. The slug is the one the proxy took from the Host header; the page
 * reaches this route only through that rewrite. Without a key for THIS tenant it shows the
 * sign-in form and nothing from the database, not even whether the subdomain belongs to a tenant.
 */
export default async function Page({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{ error?: string }>;
}) {
  const { slug } = await params;
  const { error } = await searchParams;

  let db;
  try {
    db = await getDb();
  } catch {
    return (
      <main>
        <h1>Client portal</h1>
        <p className="sub">The portal is not set up on this server.</p>
      </main>
    );
  }

  const key = (await cookies()).get(PORTAL_COOKIE)?.value ?? "";
  const access = key ? await authorizePortal(db, slug, key) : null;

  if (!access || !access.ok) {
    return (
      <main>
        <header className="top">
          <div>
            <h1>Client portal</h1>
            <p className="sub">Sign in with your viewer key.</p>
          </div>
        </header>
        {error || (access && !access.ok) ? <p role="alert">That key does not open this portal.</p> : null}
        <form action={portalSignIn.bind(null, slug)}>
          <label>
            Viewer key
            <input name="key" type="password" autoComplete="off" required />
          </label>
          <button type="submit">Open portal</button>
        </form>
      </main>
    );
  }

  const summary = await readPortalSummary(db, access.tenant.id);
  return (
    <main>
      <header className="top">
        <div>
          <h1>{access.tenant.name}</h1>
          <p className="sub">Signed in as {access.principal.role}.</p>
        </div>
        <form action={portalSignOut}>
          <button type="submit" className="linkish">
            Sign out
          </button>
        </form>
      </header>
      <p>{`${summary.snapshots} stored figures${summary.latestDay ? `, latest day ${summary.latestDay}` : ""}.`}</p>
    </main>
  );
}
