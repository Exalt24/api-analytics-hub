/**
 * Shared body of the two route handlers: read the JSON, call the handler, answer with its status.
 * A database that is not configured, or a role that sees through row level security, is a 503 with
 * no detail, never a stack trace.
 */
import { getDb } from "@/lib/server/db";
import type { Db } from "@/lib/server/db";
import type { HandlerResult } from "@/lib/replicache/server";

export async function respond(request: Request, run: (db: Db, authorization: string | null, body: unknown) => Promise<HandlerResult>): Promise<Response> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return Response.json({ error: "body is not JSON" }, { status: 400 });
  }
  let db: Db;
  try {
    db = await getDb();
  } catch {
    return Response.json({ error: "Sync is not set up on this server." }, { status: 503 });
  }
  try {
    const result = await run(db, request.headers.get("authorization"), body);
    return Response.json(result.body, { status: result.status });
  } catch {
    return Response.json({ error: "sync failed" }, { status: 500 });
  }
}
