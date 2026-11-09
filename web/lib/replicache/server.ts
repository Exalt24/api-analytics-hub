/**
 * The server half of Replicache sync for saved report presets: what the push and pull route handlers
 * (app/api/replicache/push and pull) call. Each takes the raw Authorization header and the parsed JSON
 * body as `unknown`, and returns a status and a JSON body, so tests can call them without HTTP.
 *
 * THE TENANT. The header carries the API key (`Bearer <key>`). It resolves to a tenant exactly as the
 * exports do (lib/server/auth.ts), and every query below runs inside `withTenant`, so row level security
 * filters it. No query here writes `WHERE tenant_id = ...` to isolate tenants; the only `tenant_id` in
 * this file is the value an insert must carry, which the policy's `with check` verifies.
 *
 * THE PROTOCOL, the "global version" strategy of the Replicache docs: the pull cookie is the tenant's
 * version number; a push that changes anything raises it by one inside one transaction, with the
 * tenant's counter row locked; a pull sends the rows whose version is above the cookie and the last
 * mutation id of each browser in the caller's client group.
 */
import { AuthError, resolvePrincipal } from "@/lib/server/auth";
import { withTenant, type Db, type TenantTx } from "@/lib/server/db";
import {
  MUTATIONS,
  PRESET_PREFIX,
  deleteArgsSchema,
  presetSchema,
  pullRequestSchema,
  pushRequestSchema,
  type PushRequest,
} from "@/lib/replicache/preset";

export type HandlerResult = { status: number; body: unknown };

class BadRequest extends Error {}

const bad = (message: string): HandlerResult => ({ status: 400, body: { error: message } });

async function tenantOf(db: Db, authorization: string | null): Promise<string | HandlerResult> {
  const match = /^Bearer (.+)$/.exec(authorization ?? "");
  try {
    return (await resolvePrincipal(db, match?.[1] ?? "")).tenantId;
  } catch (err) {
    if (err instanceof AuthError) return { status: err.status, body: { error: err.message } };
    throw err;
  }
}

async function applyMutation(trx: TenantTx, tenantId: string, name: string, args: unknown, version: number): Promise<void> {
  if (name === MUTATIONS.save) {
    const parsed = presetSchema.safeParse(args);
    // An invalid mutation is acknowledged and has no effect, so one bad one cannot block the queue behind it.
    if (!parsed.success) return;
    const p = parsed.data;
    await trx
      .insertInto("report_preset")
      .values({ tenant_id: tenantId, id: p.id, name: p.name, format: p.format, include_summary: p.includeSummary, deleted: false, version: String(version) })
      .onConflict((oc) =>
        oc.columns(["tenant_id", "id"]).doUpdateSet({ name: p.name, format: p.format, include_summary: p.includeSummary, deleted: false, version: String(version) }),
      )
      .execute();
    return;
  }
  if (name === MUTATIONS.remove) {
    const parsed = deleteArgsSchema.safeParse(args);
    if (!parsed.success) return;
    await trx.updateTable("report_preset").set({ deleted: true, version: String(version) }).where("id", "=", parsed.data.id).execute();
  }
}

async function push(trx: TenantTx, tenantId: string, req: PushRequest): Promise<void> {
  await trx.insertInto("replicache_space").values({ tenant_id: tenantId, version: "0" }).onConflict((oc) => oc.column("tenant_id").doNothing()).execute();
  // Locks the tenant's counter row until this transaction ends, so two pushes for one tenant take turns.
  const space = await trx.selectFrom("replicache_space").select("version").forUpdate().executeTakeFirstOrThrow();
  const next = Number(space.version) + 1;
  let changed = false;
  for (const m of req.mutations) {
    let client = await trx.selectFrom("replicache_client").select(["client_group_id", "last_mutation_id"]).where("client_id", "=", m.clientID).executeTakeFirst();
    if (!client) {
      client = { client_group_id: req.clientGroupID, last_mutation_id: "0" };
      await trx
        .insertInto("replicache_client")
        .values({ tenant_id: tenantId, client_id: m.clientID, client_group_id: req.clientGroupID, last_mutation_id: "0", version: "0" })
        .execute();
    }
    if (client.client_group_id !== req.clientGroupID) throw new BadRequest("client belongs to another client group");
    const expected = Number(client.last_mutation_id) + 1;
    if (m.id < expected) continue; // already applied: a retried push must not apply it again
    if (m.id > expected) throw new BadRequest(`mutation ${m.id} arrived before ${expected}`);
    await applyMutation(trx, tenantId, m.name, m.args, next);
    await trx.updateTable("replicache_client").set({ last_mutation_id: String(m.id), version: String(next) }).where("client_id", "=", m.clientID).execute();
    changed = true;
  }
  if (changed) await trx.updateTable("replicache_space").set({ version: String(next) }).execute();
}

export async function handlePush(db: Db, authorization: string | null, raw: unknown): Promise<HandlerResult> {
  const tenant = await tenantOf(db, authorization);
  if (typeof tenant !== "string") return tenant;
  const parsed = pushRequestSchema.safeParse(raw);
  if (!parsed.success) return bad("not a Replicache push request");
  try {
    await withTenant(db, tenant, (trx) => push(trx, tenant, parsed.data));
  } catch (err) {
    if (err instanceof BadRequest) return bad(err.message);
    throw err;
  }
  return { status: 200, body: {} };
}

export async function handlePull(db: Db, authorization: string | null, raw: unknown): Promise<HandlerResult> {
  const tenant = await tenantOf(db, authorization);
  if (typeof tenant !== "string") return tenant;
  const parsed = pullRequestSchema.safeParse(raw);
  if (!parsed.success) return bad("not a Replicache pull request");
  const req = parsed.data;
  const body = await withTenant(db, tenant, async (trx) => {
    const space = await trx.selectFrom("replicache_space").select("version").executeTakeFirst();
    const current = space ? Number(space.version) : 0;
    // A cookie from the future (the server's data was reset) is answered with the whole state, like a first pull.
    const since = req.cookie === null || req.cookie > current ? 0 : req.cookie;
    const full = since === 0;
    const rows = await trx
      .selectFrom("report_preset")
      .select(["id", "name", "format", "include_summary", "deleted"])
      .where("version", ">", String(since))
      .orderBy("version")
      .orderBy("id")
      .execute();
    const clients = await trx
      .selectFrom("replicache_client")
      .select(["client_id", "last_mutation_id"])
      .where("client_group_id", "=", req.clientGroupID)
      .where("version", ">", String(since))
      .execute();
    const patch: Array<{ op: "clear" } | { op: "put"; key: string; value: { id: string; name: string; format: string; includeSummary: boolean } } | { op: "del"; key: string }> = full ? [{ op: "clear" }] : [];
    for (const r of rows) {
      const key = `${PRESET_PREFIX}${r.id}`;
      if (r.deleted) {
        if (!full) patch.push({ op: "del", key });
      } else {
        patch.push({ op: "put", key, value: { id: r.id, name: r.name, format: r.format, includeSummary: r.include_summary } });
      }
    }
    const lastMutationIDChanges: Record<string, number> = {};
    for (const c of clients) lastMutationIDChanges[c.client_id] = Number(c.last_mutation_id);
    return { cookie: current, lastMutationIDChanges, patch };
  });
  return { status: 200, body };
}
