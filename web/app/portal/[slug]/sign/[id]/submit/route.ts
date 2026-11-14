import { cookies } from "next/headers";
import { PORTAL_COOKIE, authorizePortal } from "@/lib/portal";
import { declineRequest, isRequestId, isSameOrigin, readSignForm, seeOther, signRequest } from "@/lib/esign";
import { getDb } from "@/lib/server/db";

/**
 * Sign or decline one request: the page's form posts here, with `intent` set by the button pressed.
 *
 * A PLAIN FORM POST AND A 303, NOT A SERVER ACTION. The portal lives behind a rewrite by host (proxy.ts). A server action that redirects renders the target itself without the
 * browser's Host header, so the rewrite does not apply and the browser is shown the ordinary dashboard until the page is reloaded (found by driving the built portal in
 * web/scripts/esign-e2e.mjs). A 303 with a relative address makes the browser fetch the next page itself, on the portal's own host. It also works with scripts off.
 *
 * The portal key is checked against the host's tenant again here, a post from another origin is refused, and the backend is the one that enforces the rules: its refusal is
 * shown on the page, and a retry after a lost answer comes back as the first result.
 */
export async function POST(request: Request, { params }: { params: Promise<{ slug: string; id: string }> }): Promise<Response> {
  const { slug, id } = await params;
  if (!isRequestId(id)) return seeOther("/");
  if (!isSameOrigin(request.headers.get("origin"), request.headers.get("host"))) return new Response(null, { status: 403 });

  const key = (await cookies()).get(PORTAL_COOKIE)?.value ?? "";
  let allowed = false;
  try {
    allowed = key !== "" && (await authorizePortal(await getDb(), slug, key)).ok;
  } catch {
    allowed = false;
  }
  if (!allowed) return seeOther("/");

  const back = (params: Record<string, string>) => seeOther(`/sign/${id}?${new URLSearchParams(params)}`);
  const caller = { userAgent: request.headers.get("user-agent"), forwardedFor: request.headers.get("x-forwarded-for") };
  const form = await request.formData();

  if (form.get("intent") === "decline") {
    const result = await declineRequest(key, id, caller);
    return result.ok ? back({ done: "declined" }) : back({ error: result.message });
  }
  const read = readSignForm(form);
  if (!read.ok) return back({ error: read.message });
  const result = await signRequest(key, id, read.form, caller);
  return result.ok ? back({ done: result.value.replayed ? "again" : "signed" }) : back({ error: result.message });
}
