/**
 * The portal's e-signature client (lib/esign.ts) against a stand-in for the backend's fetch: what it sends (the key as a bearer token, the browser's agent and address, and no
 * tenant anywhere), what it refuses before any call (an id that is not a request id), what it says for each refusal, and that its field names are the backend's own.
 * The backend's rules are tested in Python against Postgres (tests/test_esign_*.py); nothing here needs a database.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { proxy } from "@/proxy";
import {
  declineRequest,
  esignBase,
  fetchDocument,
  isRequestId,
  isSameOrigin,
  listSigningRequests,
  openSigningRequest,
  readSignForm,
  seeOther,
  refusalMessage,
  signRequest,
  type Fetch,
  type SignForm,
} from "@/lib/esign";

const ID = "0f8fad5b-d9cb-469f-a165-70867728950e";
const KEY = "aah_portal_key_for_alpha";
const CALLER = { userAgent: "Mozilla/5.0 (PortalTest)", forwardedFor: "203.0.113.9" };
const FORM: SignForm = { typedName: "Dana Whitfield", consent: true, consentVersion: "2026-11-14.1", documentSha256: "a".repeat(64) };

type Seen = { url: string; init: RequestInit };

function standIn(status: number, body: unknown): { fetchImpl: Fetch; seen: Seen[] } {
  const seen: Seen[] = [];
  const fetchImpl = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    seen.push({ url: String(url), init: init ?? {} });
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }) as unknown as Fetch;
  return { fetchImpl, seen };
}

function form(fields: Record<string, string>): FormData {
  const data = new FormData();
  for (const [k, v] of Object.entries(fields)) data.set(k, v);
  return data;
}

const GOOD = { typed_name: "Dana Whitfield", consent: "on", consent_version: "2026-11-14.1", document_sha256: "a".repeat(64) };

describe("request ids", () => {
  it("accepts a uuid and nothing that could change the path or the query", () => {
    expect(isRequestId(ID)).toBe(true);
    expect(isRequestId(ID.toUpperCase())).toBe(true);
    for (const bad of ["", "abc", `${ID}/document`, `${ID}?x=1`, `../${ID}`, `${ID}%2f..`, " " + ID, ID + "\n", "'; drop table esign_request; --"]) {
      expect(isRequestId(bad), JSON.stringify(bad)).toBe(false);
    }
  });

  it("makes no backend call for an id that is not one", async () => {
    const { fetchImpl, seen } = standIn(200, {});
    expect((await openSigningRequest(KEY, "../admin", CALLER, fetchImpl)).ok).toBe(false);
    expect((await signRequest(KEY, `${ID}/../x`, FORM, CALLER, fetchImpl)).ok).toBe(false);
    expect((await declineRequest(KEY, "x", CALLER, fetchImpl)).ok).toBe(false);
    expect(await fetchDocument(KEY, "x", fetchImpl)).toBeNull();
    expect(seen).toHaveLength(0);
  });
});

describe("the sign form", () => {
  it("reads a complete form", () => {
    const r = readSignForm(form(GOOD));
    expect(r).toEqual({ ok: true, form: { typedName: "Dana Whitfield", consent: true, consentVersion: "2026-11-14.1", documentSha256: "a".repeat(64) } });
  });

  it("keeps the name as typed apart from trimming the ends", () => {
    const r = readSignForm(form({ ...GOOD, typed_name: "  dana   WHITFIELD " }));
    expect(r.ok && r.form.typedName).toBe("dana   WHITFIELD");
  });

  it("refuses an empty name, a box not ticked, and a page with no version or hash, each with its own words", () => {
    const empty = readSignForm(form({ ...GOOD, typed_name: "   " }));
    const unticked = readSignForm(form({ ...GOOD, consent: "" }));
    const missing = readSignForm(form({ typed_name: "Dana Whitfield", consent: "on" }));
    expect([empty, unticked, missing].every((r) => !r.ok)).toBe(true);
    const words = [empty, unticked, missing].map((r) => (r.ok ? "" : r.message));
    expect(new Set(words).size).toBe(3);
    expect(words[0]).toMatch(/name/i);
    expect(words[1]).toMatch(/box/i);
  });

  it("does not treat anything but the checkbox's own value as a tick", () => {
    for (const v of ["true", "1", "yes", "off"]) expect(readSignForm(form({ ...GOOD, consent: v })).ok).toBe(false);
  });
});

describe("what is sent", () => {
  it("signs with the key as a bearer token, the browser's agent and address, and a body with no tenant in it", async () => {
    const { fetchImpl, seen } = standIn(200, { request_id: ID, signature_id: "s1", signed_at: "2026-11-14T12:00:00.000000Z", signature_hash: "h".repeat(64), replayed: false });
    const r = await signRequest(KEY, ID, FORM, CALLER, fetchImpl);
    expect(r).toEqual({ ok: true, value: { requestId: ID, signatureId: "s1", signedAt: "2026-11-14T12:00:00.000000Z", signatureHash: "h".repeat(64), replayed: false } });
    expect(seen).toHaveLength(1);
    const { url, init } = seen[0];
    const sent = init.headers as Record<string, string>;
    expect(url).toBe(`${esignBase()}/api/esign/requests/${ID}/sign`);
    expect(init.method).toBe("POST");
    expect(sent.Authorization).toBe(`Bearer ${KEY}`);
    expect(sent["User-Agent"]).toBe(CALLER.userAgent);
    expect(sent["X-Forwarded-For"]).toBe(CALLER.forwardedFor);
    expect(Object.keys(JSON.parse(String(init.body))).sort()).toEqual(["consent", "consent_version", "document_sha256", "typed_name"]);
    expect(url).not.toContain(KEY);
    expect(String(init.body)).not.toContain(KEY);
    expect(init.cache).toBe("no-store");
  });

  it("names the backend's own fields: the body keys are the ones app/api/esign.py declares", () => {
    const py = readFileSync(fileURLToPath(new URL("../../app/api/esign.py", import.meta.url)), "utf-8");
    const block = py.slice(py.indexOf("class SignBody"), py.indexOf("@router.post", py.indexOf("class SignBody")));
    const declared = [...block.matchAll(/^    (\w+):/gm)].map((m) => m[1]).sort();
    expect(declared).toEqual(["consent", "consent_version", "document_sha256", "typed_name"]);
  });

  it("leaves the agent and address headers off when the browser sent none", async () => {
    const { fetchImpl, seen } = standIn(200, { status: "declined" });
    await declineRequest(KEY, ID, { userAgent: null, forwardedFor: null }, fetchImpl);
    const sent = seen[0].init.headers as Record<string, string>;
    expect(Object.keys(sent)).toEqual(["Authorization"]);
  });

  it("lists, opens and declines with the same bearer key", async () => {
    const list = standIn(200, { requests: [{ id: ID, document_name: "Letter.pdf" }] });
    const listed = await listSigningRequests(KEY, list.fetchImpl);
    expect(listed.ok && listed.value).toEqual([{ id: ID, document_name: "Letter.pdf" }]);
    const open = standIn(200, { id: ID, consent_text: "text" });
    expect((await openSigningRequest(KEY, ID, CALLER, open.fetchImpl)).ok).toBe(true);
    const decline = standIn(200, { status: "declined" });
    const declined = await declineRequest(KEY, ID, CALLER, decline.fetchImpl);
    expect(declined).toEqual({ ok: true, value: "declined" });
    for (const s of [list.seen[0], open.seen[0], decline.seen[0]]) expect((s.init.headers as Record<string, string>).Authorization).toBe(`Bearer ${KEY}`);
  });

  it("takes the backend address from ESIGN_API_BASE, then NEXT_PUBLIC_API_BASE, without a trailing slash", () => {
    const keep = { a: process.env.ESIGN_API_BASE, b: process.env.NEXT_PUBLIC_API_BASE };
    try {
      process.env.ESIGN_API_BASE = "http://esign.internal:9000///";
      expect(esignBase()).toBe("http://esign.internal:9000");
      delete process.env.ESIGN_API_BASE;
      process.env.NEXT_PUBLIC_API_BASE = "http://api.internal:8000/";
      expect(esignBase()).toBe("http://api.internal:8000");
    } finally {
      if (keep.a === undefined) delete process.env.ESIGN_API_BASE;
      else process.env.ESIGN_API_BASE = keep.a;
      if (keep.b === undefined) delete process.env.NEXT_PUBLIC_API_BASE;
      else process.env.NEXT_PUBLIC_API_BASE = keep.b;
    }
  });
});

describe("what a refusal says", () => {
  it.each([
    [404, undefined, /not found/i],
    [410, undefined, /expired/i],
    [409, "this request is already signed", /already signed/i],
    [409, "the document is not the one that was shown", /reload/i],
    [403, "role viewer may not write:esign", /key/i],
    [422, "the typed name is not the signer's name", /typed name/i],
    [500, undefined, /could not be reached/i],
  ])("%i %s", (status, detail, expected) => {
    expect(refusalMessage(status, detail)).toMatch(expected);
  });

  it("does not echo a long or missing 422 detail", () => {
    expect(refusalMessage(422, "x".repeat(300))).toBe("The form is not complete.");
    expect(refusalMessage(422, undefined)).toBe("The form is not complete.");
  });

  it("turns each backend status into a failed result carrying the person's words", async () => {
    for (const [status, detail] of [
      [404, "signing request not found"],
      [409, "this request is already signed"],
      [410, "this request has expired"],
      [422, "tick the consent box to sign"],
    ] as const) {
      const { fetchImpl } = standIn(status, { detail });
      const r = await signRequest(KEY, ID, FORM, CALLER, fetchImpl);
      expect(r).toEqual({ ok: false, status, message: refusalMessage(status, detail) });
    }
  });

  it("reports an unreachable backend as a failure and never throws", async () => {
    const down = (async () => {
      throw new TypeError("fetch failed");
    }) as unknown as Fetch;
    expect(await signRequest(KEY, ID, FORM, CALLER, down)).toEqual({ ok: false, status: 0, message: refusalMessage(0, undefined) });
    expect(await fetchDocument(KEY, ID, down)).toBeNull();
  });

  it("reads a retry after a lost answer as a success that says it is a replay", async () => {
    const { fetchImpl } = standIn(200, { request_id: ID, signature_id: "s1", signed_at: "t", signature_hash: "h", replayed: true });
    const r = await signRequest(KEY, ID, FORM, CALLER, fetchImpl);
    expect(r.ok && r.value.replayed).toBe(true);
  });

  it("hands the document back only on a 200", async () => {
    const ok = (async () => new Response("bytes", { status: 200 })) as unknown as Fetch;
    const missing = (async () => new Response("", { status: 404 })) as unknown as Fetch;
    expect(await (await fetchDocument(KEY, ID, ok))?.text()).toBe("bytes");
    expect(await fetchDocument(KEY, ID, missing)).toBeNull();
  });
});

describe("the signing pages sit behind the portal proxy", () => {
  it("rewrites /sign/<id> on a tenant host to the portal route, and refuses the internal path from outside", () => {
    process.env.PORTAL_BASE_DOMAIN = "portal.example.com";
    try {
      const rewritten = proxy(new NextRequest(`http://alpha.portal.example.com/sign/${ID}`, { headers: { host: "alpha.portal.example.com" } }));
      expect(rewritten.headers.get("x-middleware-rewrite")).toContain(`/portal/alpha/sign/${ID}`);
      const direct = proxy(new NextRequest(`http://alpha.portal.example.com/portal/bravo/sign/${ID}`, { headers: { host: "alpha.portal.example.com" } }));
      expect(direct.status).toBe(404);
    } finally {
      delete process.env.PORTAL_BASE_DOMAIN;
    }
  });
});

describe("the form post and its redirect", () => {
  it("answers 303 with a relative address, so the browser follows it on the portal's own host", () => {
    const r = seeOther(`/sign/${ID}?done=signed`);
    expect(r.status).toBe(303);
    expect(r.headers.get("Location")).toBe(`/sign/${ID}?done=signed`);
  });

  it("never sends the browser to another site, whatever path it is given", () => {
    for (const path of ["//evil.test/x", "https://evil.test/", "evil", ""]) expect(seeOther(path).headers.get("Location"), path).toBe("/");
  });

  it("takes a post from the portal's own origin and refuses one from another site", () => {
    expect(isSameOrigin("http://alpha.portal.example.com:3200", "alpha.portal.example.com:3200")).toBe(true);
    expect(isSameOrigin("https://ALPHA.portal.example.com", "alpha.portal.example.com")).toBe(true);
    expect(isSameOrigin(null, "alpha.portal.example.com")).toBe(true);
    for (const origin of ["https://evil.test", "https://alpha.portal.example.com.evil.test", "https://bravo.portal.example.com", "null", "not a url"]) {
      expect(isSameOrigin(origin, "alpha.portal.example.com"), origin).toBe(false);
    }
    expect(isSameOrigin("https://alpha.portal.example.com", null)).toBe(false);
  });
});
