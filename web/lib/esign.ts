/**
 * The portal's side of e-signatures: list, open, sign, decline and fetch a document through the backend's routes (app/api/esign.py).
 *
 * THE BACKEND DECIDES, THIS FILE ONLY CARRIES. Every call sends the portal key as a bearer token and nothing about a tenant: the backend takes the tenant from the key, filters
 * in Postgres under forced row level security, and answers a missing request and another tenant's request with the same 404. The portal page has already checked that the key
 * belongs to the host's tenant (`authorizePortal`), so a key for another portal never gets this far.
 *
 * WHAT THE BACKEND RECORDS ABOUT THE SIGNER comes from here: the browser's user agent and address are forwarded as `User-Agent` and `X-Forwarded-For`, because the backend sees
 * this server and not the browser. The backend hashes them with a keyed hash before storing and trusts the forwarded address only when ESIGN_TRUST_FORWARDED=1.
 */

/** A request id as the backend makes them. Anything else is refused before a URL is built, so an id cannot add a path segment or a query. */
const REQUEST_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isRequestId(value: string): boolean {
  return REQUEST_ID.test(value);
}

export function esignBase(): string {
  return (process.env.ESIGN_API_BASE ?? process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
}

export type SigningSummary = {
  id: string;
  document_name: string;
  document_sha256: string;
  signer_name: string;
  status: "pending" | "signed" | "declined" | "expired";
  created_at: string;
  expires_at: string;
};

export type SigningRequest = SigningSummary & {
  document_bytes: number;
  signer_email: string;
  consent_version: string;
  consent_text: string;
  signature: { id: string; signed_at: string; signature_hash: string; signer_name_typed: string } | null;
};

export type SignForm = { typedName: string; consent: boolean; consentVersion: string; documentSha256: string };

export type Caller = { userAgent: string | null; forwardedFor: string | null };

export type Fetch = typeof fetch;

export type Result<T> = { ok: true; value: T } | { ok: false; status: number; message: string };

export type SignOutcome = { requestId: string; signatureId: string; signedAt: string; signatureHash: string; replayed: boolean };

/** What the person is told for each refusal. The 422 text of the backend is kept: it says what to fix without naming anyone's data. */
export function refusalMessage(status: number, detail: string | undefined): string {
  switch (status) {
    case 401:
    case 403:
      return "That key does not open this request.";
    case 404:
      return "That signing request was not found.";
    case 409:
      return detail && /already signed/i.test(detail) ? "This request is already signed." : "This request cannot be signed as it stands. Reload the page and read it again.";
    case 410:
      return "This request has expired.";
    case 422:
      return detail && detail.length < 200 ? detail : "The form is not complete.";
    default:
      return "The signing service could not be reached. Try again in a moment.";
  }
}

/** Read the sign form. The name and the tick are checked here only to save a trip; the backend checks them again and is the one that counts. */
export function readSignForm(data: FormData): { ok: true; form: SignForm } | { ok: false; message: string } {
  const typedName = String(data.get("typed_name") ?? "").trim();
  if (!typedName) return { ok: false, message: "Type your full name to sign." };
  if (data.get("consent") !== "on") return { ok: false, message: "Tick the box to agree before you sign." };
  const consentVersion = String(data.get("consent_version") ?? "");
  const documentSha256 = String(data.get("document_sha256") ?? "");
  if (!consentVersion || !documentSha256) return { ok: false, message: "The page is out of date. Reload it and try again." };
  return { ok: true, form: { typedName, consent: true, consentVersion, documentSha256 } };
}

function headers(key: string, caller?: Caller): Record<string, string> {
  const out: Record<string, string> = { Authorization: `Bearer ${key}` };
  if (caller?.userAgent) out["User-Agent"] = caller.userAgent;
  if (caller?.forwardedFor) out["X-Forwarded-For"] = caller.forwardedFor;
  return out;
}

async function detailOf(res: Response): Promise<string | undefined> {
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string") return body.detail;
  } catch {
    /* a body that is not JSON has no detail to show */
  }
  return undefined;
}

async function send<T>(fetchImpl: Fetch, url: string, init: RequestInit, parse: (body: unknown) => T): Promise<Result<T>> {
  let res: Response;
  try {
    res = await fetchImpl(url, { ...init, cache: "no-store" });
  } catch {
    return { ok: false, status: 0, message: refusalMessage(0, undefined) };
  }
  if (!res.ok) return { ok: false, status: res.status, message: refusalMessage(res.status, await detailOf(res)) };
  return { ok: true, value: parse(await res.json()) };
}

export function listSigningRequests(key: string, fetchImpl: Fetch = fetch): Promise<Result<SigningSummary[]>> {
  return send(fetchImpl, `${esignBase()}/api/esign/requests`, { headers: headers(key) }, (b) => (b as { requests: SigningSummary[] }).requests);
}

/** Opening a request writes a 'viewed' row in the audit log, so the page calls this once for each load and not in a loop. */
export function openSigningRequest(key: string, id: string, caller: Caller, fetchImpl: Fetch = fetch): Promise<Result<SigningRequest>> {
  if (!isRequestId(id)) return Promise.resolve({ ok: false, status: 404, message: refusalMessage(404, undefined) });
  return send(fetchImpl, `${esignBase()}/api/esign/requests/${id}`, { headers: headers(key, caller) }, (b) => b as SigningRequest);
}

export function signRequest(key: string, id: string, form: SignForm, caller: Caller, fetchImpl: Fetch = fetch): Promise<Result<SignOutcome>> {
  if (!isRequestId(id)) return Promise.resolve({ ok: false, status: 404, message: refusalMessage(404, undefined) });
  const body = JSON.stringify({
    typed_name: form.typedName,
    consent: form.consent,
    consent_version: form.consentVersion,
    document_sha256: form.documentSha256,
  });
  return send(
    fetchImpl,
    `${esignBase()}/api/esign/requests/${id}/sign`,
    { method: "POST", headers: { ...headers(key, caller), "Content-Type": "application/json" }, body },
    (b) => {
      const r = b as { request_id: string; signature_id: string; signed_at: string; signature_hash: string; replayed: boolean };
      return { requestId: r.request_id, signatureId: r.signature_id, signedAt: r.signed_at, signatureHash: r.signature_hash, replayed: r.replayed };
    },
  );
}

export function declineRequest(key: string, id: string, caller: Caller, fetchImpl: Fetch = fetch): Promise<Result<string>> {
  if (!isRequestId(id)) return Promise.resolve({ ok: false, status: 404, message: refusalMessage(404, undefined) });
  return send(fetchImpl, `${esignBase()}/api/esign/requests/${id}/decline`, { method: "POST", headers: headers(key, caller) }, (b) => (b as { status: string }).status);
}

/** The document, streamed back to the browser by the portal's route so the key never leaves the server. */
export async function fetchDocument(key: string, id: string, fetchImpl: Fetch = fetch): Promise<Response | null> {
  if (!isRequestId(id)) return null;
  try {
    const res = await fetchImpl(`${esignBase()}/api/esign/requests/${id}/document`, { headers: headers(key), cache: "no-store" });
    return res.ok ? res : null;
  } catch {
    return null;
  }
}

/** A 303 to a path on the same host. The address is relative, so the browser follows it on the portal's own host. */
export function seeOther(path: string): Response {
  return new Response(null, { status: 303, headers: { Location: path.startsWith("/") && !path.startsWith("//") ? path : "/" } });
}

/** A post is from this site when its Origin names the host it was sent to. A request with no Origin header (not a browser form post) is let through to the key check. */
export function isSameOrigin(origin: string | null, host: string | null): boolean {
  if (origin === null) return true;
  if (!host) return false;
  try {
    return new URL(origin).host.toLowerCase() === host.toLowerCase();
  } catch {
    return false;
  }
}
