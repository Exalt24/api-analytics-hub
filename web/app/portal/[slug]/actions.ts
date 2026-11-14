"use server";

/**
 * Sign in to one client's portal. The cookie holds the key and is HOST ONLY (no Domain
 * attribute), so the browser never sends Alpha's key to Bravo's subdomain. The page checks the
 * key against the tenant of the host again on every load, so the cookie alone proves nothing.
 */
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { PORTAL_COOKIE, authorizePortal } from "@/lib/portal";
import { getDb } from "@/lib/server/db";

export async function portalSignIn(slug: string, formData: FormData): Promise<void> {
  const key = String(formData.get("key") ?? "").trim();
  const back = `/?${new URLSearchParams({ error: "1" })}`;
  let ok = false;
  try {
    const access = await authorizePortal(await getDb(), String(slug), key);
    ok = access.ok;
  } catch {
    ok = false;
  }
  if (!ok) redirect(back);
  (await cookies()).set(PORTAL_COOKIE, key, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
  });
  redirect("/");
}

export async function portalSignOut(): Promise<void> {
  (await cookies()).delete(PORTAL_COOKIE);
  redirect("/");
}
