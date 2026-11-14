import type { Metadata } from "next";
import { cookies, headers } from "next/headers";
import { PORTAL_COOKIE, authorizePortal } from "@/lib/portal";
import { isRequestId, openSigningRequest } from "@/lib/esign";
import { getDb } from "@/lib/server/db";

export const metadata: Metadata = { title: "Sign a document" };
export const dynamic = "force-dynamic";

const DONE: Record<string, string> = {
  signed: "Signed. Your signature is recorded against the document fingerprint below.",
  again: "This request was already signed with those details. Nothing was added.",
  declined: "You declined this request.",
};

/**
 * One signing request in one client's portal. The key is checked against the host's tenant first; the request is then read from the backend with that key, which writes a
 * 'viewed' row to its audit log. The page shows the document's SHA-256 and the consent text the signature will carry, and takes the typed name and the tick. The form posts to
 * ./submit (a route handler that answers 303), see the note there.
 */
export default async function Page({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string; id: string }>;
  searchParams: Promise<{ error?: string; done?: string }>;
}) {
  const { slug, id } = await params;
  const { error, done } = await searchParams;

  let access: Awaited<ReturnType<typeof authorizePortal>> | null = null;
  const key = (await cookies()).get(PORTAL_COOKIE)?.value ?? "";
  try {
    access = key ? await authorizePortal(await getDb(), slug, key) : null;
  } catch {
    access = null;
  }
  if (!access || !access.ok) {
    return (
      <main>
        <h1>Client portal</h1>
        <p className="sub">Sign in with your viewer key on the portal home page first.</p>
      </main>
    );
  }

  const h = await headers();
  const found = isRequestId(id)
    ? await openSigningRequest(key, id, { userAgent: h.get("user-agent"), forwardedFor: h.get("x-forwarded-for") })
    : null;
  if (!found || !found.ok) {
    return (
      <main>
        <h1>{access.tenant.name}</h1>
        <p role="alert">That signing request was not found.</p>
        <p>
          <a href="/">Back to the portal</a>
        </p>
      </main>
    );
  }

  const req = found.value;
  return (
    <main>
      <header className="top">
        <div>
          <h1>{req.document_name}</h1>
          <p className="sub">
            For {req.signer_name}. Status: {req.status}. Expires {req.expires_at}.
          </p>
        </div>
      </header>
      {done && DONE[done] ? <p role="status">{DONE[done]}</p> : null}
      {error ? <p role="alert">{error}</p> : null}
      <p>
        Document fingerprint (SHA-256): <code>{req.document_sha256}</code> ({req.document_bytes} bytes). <a href={`/sign/${id}/document`}>Download the document</a>
      </p>
      <h2>Consent</h2>
      <pre style={{ whiteSpace: "pre-wrap" }}>{req.consent_text}</pre>
      {req.signature ? (
        <p>
          Signed by {req.signature.signer_name_typed} at {req.signature.signed_at}. Signature hash: <code>{req.signature.signature_hash}</code>
        </p>
      ) : null}
      {req.status === "pending" ? (
        <form method="post" action={`/sign/${id}/submit`}>
          <input type="hidden" name="consent_version" value={req.consent_version} />
          <input type="hidden" name="document_sha256" value={req.document_sha256} />
          <label>
            Type your full name
            <input name="typed_name" autoComplete="off" required />
          </label>
          <label>
            <input type="checkbox" name="consent" required /> I agree to sign electronically and to use electronic records, as written above.
          </label>
          <button type="submit" name="intent" value="sign">
            Sign
          </button>
          <button type="submit" name="intent" value="decline" formNoValidate className="linkish">
            Decline
          </button>
        </form>
      ) : null}
    </main>
  );
}
