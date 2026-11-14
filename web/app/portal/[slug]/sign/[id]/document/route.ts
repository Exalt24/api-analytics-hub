import { cookies } from "next/headers";
import { PORTAL_COOKIE, authorizePortal } from "@/lib/portal";
import { fetchDocument, isRequestId } from "@/lib/esign";
import { getDb } from "@/lib/server/db";

/**
 * The document as the client reads it, fetched from the backend with the portal key (which stays on the server). Without a key for this portal, or for a request that is not
 * this tenant's, the answer is the same empty 404, so the route says nothing about which ids exist.
 */
export async function GET(_request: Request, { params }: { params: Promise<{ slug: string; id: string }> }): Promise<Response> {
  const { slug, id } = await params;
  const notFound = () => new Response(null, { status: 404 });
  if (!isRequestId(id)) return notFound();
  const key = (await cookies()).get(PORTAL_COOKIE)?.value ?? "";
  if (!key) return notFound();
  try {
    const access = await authorizePortal(await getDb(), slug, key);
    if (!access.ok) return notFound();
  } catch {
    return notFound();
  }
  const upstream = await fetchDocument(key, id);
  if (!upstream) return notFound();
  return new Response(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": "application/octet-stream",
      "Content-Disposition": upstream.headers.get("content-disposition") ?? "attachment",
      "X-Document-SHA256": upstream.headers.get("x-document-sha256") ?? "",
      "Cache-Control": "no-store",
    },
  });
}
