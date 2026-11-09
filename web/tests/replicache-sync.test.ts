/**
 * Replicache sync for saved report presets, against real Postgres under forced row level security.
 *
 * Two kinds of test. The protocol tests call the push and pull handlers with hand-built request bodies, so
 * each rule (replay, order, validation, tenant) is checked on its own with literal expected rows. The client
 * tests run the real Replicache 15 client (in memory, since Node has no IndexedDB) with `fetch` replaced by a
 * bridge to those handlers, and a switch per device that makes the network fail, to show a change made offline
 * reaching another device after reconnect. The bridge is the only fake: the client library, the handlers and
 * the database are the real ones.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, inject, it, vi } from "vitest";
import pg from "pg";
import { createDb, pgDialect, type Db } from "@/lib/server/db";
import { handlePull, handlePush, type HandlerResult } from "@/lib/replicache/server";
import { createPresetStore, listPresets, type PresetStore } from "@/lib/replicache/client";
import { KEY_A, KEY_A_REVOKED, KEY_B, TENANT_A, TENANT_B } from "./fixtures";

const adminDsn = inject("adminDsn");
const appDsn = inject("appDsn");
const run = describe.skipIf(!adminDsn);

const CLIENT_1 = "client-aaaa-0001";
const GROUP_1 = "group-aaaa-0001";
const GROUP_2 = "group-bbbb-0002";
const bearer = (key: string) => `Bearer ${key}`;

type Mutation = { id: number; name: string; args: unknown; clientID?: string };
const pushBody = (mutations: Mutation[], group = GROUP_1) => ({
  pushVersion: 1,
  schemaVersion: "1",
  profileID: "profile-0001",
  clientGroupID: group,
  mutations: mutations.map((m) => ({ timestamp: 1, clientID: CLIENT_1, ...m })),
});
const pullBody = (cookie: number | null, group = GROUP_1) => ({ pullVersion: 1, schemaVersion: "1", profileID: "profile-0001", clientGroupID: group, cookie });
const save = (id: string, name: string, format = "csv", includeSummary = false) => ({ name: "savePreset", args: { id, name, format, includeSummary } });
const pullOk = (r: HandlerResult) => {
  expect(r.status).toBe(200);
  return r.body as { cookie: number; lastMutationIDChanges: Record<string, number>; patch: Array<{ op: string; key?: string; value?: unknown }> };
};

run("saved report presets sync through Replicache push and pull", () => {
  let db: Db;
  let admin: pg.Pool;
  beforeAll(() => {
    db = createDb(pgDialect(adminDsn, 10));
    admin = new pg.Pool({ connectionString: adminDsn, max: 2 });
  });
  afterAll(async () => {
    await db.destroy();
    await admin.end();
  });
  beforeEach(async () => {
    // The superuser connection skips row level security, so it can clear and read every tenant's rows.
    await admin.query("delete from report_preset; delete from replicache_client; delete from replicache_space");
  });

  const rows = async (tenant: string) =>
    (await admin.query("select id, name, format, include_summary, deleted, version::int as version from report_preset where tenant_id = $1 order by id", [tenant])).rows;
  const spaceVersion = async (tenant: string) =>
    (await admin.query("select version::int as v from replicache_space where tenant_id = $1", [tenant])).rows[0]?.v;

  describe("the protocol", () => {
    it("a push stores the presets and raises the tenant version once, a pull returns them with the client's last mutation id", async () => {
      const r = await handlePush(db, bearer(KEY_A), pushBody([{ id: 1, ...save("preset-0001", "Monthly CSV") }, { id: 2, ...save("preset-0002", "Board PDF", "pdf", true) }]));
      expect(r.status).toBe(200);
      expect(await rows(TENANT_A)).toEqual([
        { id: "preset-0001", name: "Monthly CSV", format: "csv", include_summary: false, deleted: false, version: 1 },
        { id: "preset-0002", name: "Board PDF", format: "pdf", include_summary: true, deleted: false, version: 1 },
      ]);
      expect(await spaceVersion(TENANT_A)).toBe(1);
      const pulled = pullOk(await handlePull(db, bearer(KEY_A), pullBody(null)));
      expect(pulled.cookie).toBe(1);
      expect(pulled.lastMutationIDChanges).toEqual({ [CLIENT_1]: 2 });
      expect(pulled.patch).toEqual([
        { op: "clear" },
        { op: "put", key: "preset/preset-0001", value: { id: "preset-0001", name: "Monthly CSV", format: "csv", includeSummary: false } },
        { op: "put", key: "preset/preset-0002", value: { id: "preset-0002", name: "Board PDF", format: "pdf", includeSummary: true } },
      ]);
    });

    it("replaying the same push applies nothing twice and does not raise the version", async () => {
      const body = pushBody([{ id: 1, ...save("preset-0001", "Monthly CSV") }]);
      await handlePush(db, bearer(KEY_A), body);
      await handlePush(db, bearer(KEY_A), body);
      expect(await spaceVersion(TENANT_A)).toBe(1);
      expect((await rows(TENANT_A)).map((r) => r.version)).toEqual([1]);
      // an old id after a rename: the replay of mutation 1 must not undo mutation 2
      await handlePush(db, bearer(KEY_A), pushBody([{ id: 1, ...save("preset-0001", "Monthly CSV") }, { id: 2, ...save("preset-0001", "Renamed") }]));
      await handlePush(db, bearer(KEY_A), pushBody([{ id: 1, ...save("preset-0001", "Monthly CSV") }]));
      expect((await rows(TENANT_A)).map((r) => r.name)).toEqual(["Renamed"]);
    });

    it("a mutation id that skips ahead is refused and leaves no trace, even for the ones before it", async () => {
      const r = await handlePush(db, bearer(KEY_A), pushBody([{ id: 1, ...save("preset-0001", "One") }, { id: 3, ...save("preset-0003", "Three") }]));
      expect(r.status).toBe(400);
      expect(await rows(TENANT_A)).toEqual([]);
      expect(await spaceVersion(TENANT_A)).toBeUndefined();
      expect((await admin.query("select count(*)::int as n from replicache_client")).rows[0].n).toBe(0);
    });

    it.each([
      ["a format that is not csv, xlsx or pdf", save("preset-0001", "Bad", "docx")],
      ["an empty name", save("preset-0001", "   ")],
      ["a name over 60 characters", save("preset-0001", "x".repeat(61))],
      ["an id with a slash", save("a/b/c/d/e/f", "Bad")],
      ["a mutator that does not exist", { name: "dropEverything", args: {} }],
    ])("%s is acknowledged and changes nothing, so the queue behind it still moves", async (_name, m) => {
      await handlePush(db, bearer(KEY_A), pushBody([{ id: 1, ...m }, { id: 2, ...save("preset-0009", "Good") }]));
      expect((await rows(TENANT_A)).map((r) => r.id)).toEqual(["preset-0009"]);
      const pulled = pullOk(await handlePull(db, bearer(KEY_A), pullBody(null)));
      expect(pulled.lastMutationIDChanges).toEqual({ [CLIENT_1]: 2 });
    });

    it("a delete is a tombstone: the next incremental pull says del, a first pull says nothing", async () => {
      await handlePush(db, bearer(KEY_A), pushBody([{ id: 1, ...save("preset-0001", "One") }, { id: 2, ...save("preset-0002", "Two") }]));
      const first = pullOk(await handlePull(db, bearer(KEY_A), pullBody(null)));
      await handlePush(db, bearer(KEY_A), pushBody([{ id: 3, name: "deletePreset", args: { id: "preset-0001" } }]));
      const incremental = pullOk(await handlePull(db, bearer(KEY_A), pullBody(first.cookie)));
      expect(incremental.patch).toEqual([{ op: "del", key: "preset/preset-0001" }]);
      expect(incremental.cookie).toBe(2);
      const fresh = pullOk(await handlePull(db, bearer(KEY_A), pullBody(null)));
      expect(fresh.patch.map((p) => p.key ?? p.op)).toEqual(["clear", "preset/preset-0002"]);
    });

    it("an incremental pull sends only what changed, and nothing when the cookie is current", async () => {
      await handlePush(db, bearer(KEY_A), pushBody([{ id: 1, ...save("preset-0001", "One") }]));
      const first = pullOk(await handlePull(db, bearer(KEY_A), pullBody(null)));
      await handlePush(db, bearer(KEY_A), pushBody([{ id: 2, ...save("preset-0002", "Two") }]));
      const second = pullOk(await handlePull(db, bearer(KEY_A), pullBody(first.cookie)));
      expect(second.patch).toEqual([{ op: "put", key: "preset/preset-0002", value: { id: "preset-0002", name: "Two", format: "csv", includeSummary: false } }]);
      expect(second.lastMutationIDChanges).toEqual({ [CLIENT_1]: 2 });
      const third = pullOk(await handlePull(db, bearer(KEY_A), pullBody(second.cookie)));
      expect(third.patch).toEqual([]);
      expect(third.lastMutationIDChanges).toEqual({});
    });

    it("a cookie from the future (server data was reset) is answered with the whole state", async () => {
      await handlePush(db, bearer(KEY_A), pushBody([{ id: 1, ...save("preset-0001", "One") }]));
      const pulled = pullOk(await handlePull(db, bearer(KEY_A), pullBody(99)));
      expect(pulled.patch.map((p) => p.op)).toEqual(["clear", "put"]);
      expect(pulled.cookie).toBe(1);
    });

    it("eight pushes at the same moment for one tenant all land, each on its own version", async () => {
      // The counter row exists first, so the pushes must queue on its lock and not on its creation.
      await handlePush(db, bearer(KEY_A), pushBody([{ id: 1, ...save("preset-0000", "First") }]));
      const results = await Promise.all(
        Array.from({ length: 8 }, (_, i) =>
          handlePush(db, bearer(KEY_A), pushBody([{ id: 1, clientID: `client-conc-000${i}`, ...save(`preset-conc-${i}`, `From ${i}`) }], `group-conc-000${i}`)),
        ),
      );
      expect(results.map((r) => r.status)).toEqual(Array(8).fill(200));
      expect(await spaceVersion(TENANT_A)).toBe(9);
      expect((await rows(TENANT_A)).map((r) => r.version).sort((a, b) => a - b)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);
    });

    it("a client id that already belongs to another client group is refused", async () => {
      await handlePush(db, bearer(KEY_A), pushBody([{ id: 1, ...save("preset-0001", "One") }], GROUP_1));
      const r = await handlePush(db, bearer(KEY_A), pushBody([{ id: 2, ...save("preset-0002", "Two") }], GROUP_2));
      expect(r.status).toBe(400);
      expect((await rows(TENANT_A)).map((x) => x.id)).toEqual(["preset-0001"]);
    });
  });

  describe("who may call it", () => {
    it.each([
      ["no header", null],
      ["a header that is not Bearer", "Basic abc"],
      ["a key nobody has", bearer("not-a-real-key-0000")],
      ["a revoked key", bearer(KEY_A_REVOKED)],
    ])("%s is a 401 on push and on pull and writes nothing", async (_n, header) => {
      expect((await handlePush(db, header, pushBody([{ id: 1, ...save("preset-0001", "One") }]))).status).toBe(401);
      expect((await handlePull(db, header, pullBody(null))).status).toBe(401);
      expect(await rows(TENANT_A)).toEqual([]);
    });

    it.each([["a push", handlePush], ["a pull", handlePull]])("%s whose body is not a Replicache request is a 400", async (_n, handler) => {
      expect((await handler(db, bearer(KEY_A), { nope: true })).status).toBe(400);
      expect((await handler(db, bearer(KEY_A), null)).status).toBe(400);
    });
  });

  describe("the tenant boundary", () => {
    it("two tenants may use the same preset id and never see or change each other's", async () => {
      await handlePush(db, bearer(KEY_A), pushBody([{ id: 1, ...save("shared-id-01", "Alpha only") }]));
      await handlePush(db, bearer(KEY_B), pushBody([{ id: 1, ...save("shared-id-01", "Bravo only", "pdf") }]));
      expect((await rows(TENANT_A)).map((r) => r.name)).toEqual(["Alpha only"]);
      expect((await rows(TENANT_B)).map((r) => r.name)).toEqual(["Bravo only"]);
      const a = pullOk(await handlePull(db, bearer(KEY_A), pullBody(null)));
      expect(JSON.stringify(a.patch)).not.toContain("Bravo");
      const b = pullOk(await handlePull(db, bearer(KEY_B), pullBody(null)));
      expect(JSON.stringify(b.patch)).not.toContain("Alpha");
    });

    it("tenant B deleting an id tenant A also has does not delete A's", async () => {
      await handlePush(db, bearer(KEY_A), pushBody([{ id: 1, ...save("shared-id-01", "Alpha only") }]));
      await handlePush(db, bearer(KEY_B), pushBody([{ id: 1, ...save("shared-id-01", "Bravo only") }, { id: 2, name: "deletePreset", args: { id: "shared-id-01" } }]));
      expect((await rows(TENANT_A)).map((r) => [r.name, r.deleted])).toEqual([["Alpha only", false]]);
      expect((await rows(TENANT_B)).map((r) => [r.name, r.deleted])).toEqual([["Bravo only", true]]);
    });

    it("the same client id and group id under two tenants stay two clients with two mutation counters", async () => {
      await handlePush(db, bearer(KEY_A), pushBody([{ id: 1, ...save("preset-0001", "A1") }, { id: 2, ...save("preset-0002", "A2") }]));
      const r = await handlePush(db, bearer(KEY_B), pushBody([{ id: 1, ...save("preset-0001", "B1") }]));
      expect(r.status).toBe(200);
      expect(pullOk(await handlePull(db, bearer(KEY_B), pullBody(null))).lastMutationIDChanges).toEqual({ [CLIENT_1]: 1 });
      expect(pullOk(await handlePull(db, bearer(KEY_A), pullBody(null))).lastMutationIDChanges).toEqual({ [CLIENT_1]: 2 });
    });

    it("a login that is not a superuser sees nothing with no tenant set and cannot insert a row for another tenant", async () => {
      await handlePush(db, bearer(KEY_A), pushBody([{ id: 1, ...save("preset-0001", "Alpha only") }]));
      const app = new pg.Client({ connectionString: appDsn });
      await app.connect();
      try {
        expect((await app.query("select count(*)::int as n from report_preset")).rows[0].n).toBe(0);
        await app.query("begin");
        await app.query("select set_config('app.tenant_id', $1, true)", [TENANT_B]);
        await expect(
          app.query("insert into report_preset (tenant_id, id, name, format, version) values ($1, 'preset-0777', 'Smuggled', 'csv', 1)", [TENANT_A]),
        ).rejects.toThrow(/row-level security/);
        await app.query("rollback");
      } finally {
        await app.end();
      }
    });
  });

  describe("the real Replicache client", () => {
    // A bridge in place of the network: a request to http://<device>.test/push|pull goes to the handler under
    // that device's key, unless that device is switched offline, when fetch fails the way a dead network does.
    const offline = new Set<string>();
    const keyOf: Record<string, string> = {};
    const stores: PresetStore[] = [];
    const realFetch = globalThis.fetch;
    let seq = 0;

    beforeEach(() => {
      offline.clear();
      vi.stubGlobal("fetch", async (input: string | URL | Request, init?: RequestInit) => {
        const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
        const device = url.hostname.replace(".test", "");
        if (offline.has(device)) throw new TypeError("Failed to fetch");
        // Replicache calls fetch with a Request; a plain (url, init) call is read the same way.
        const req = input instanceof Request ? input : new Request(url, init);
        const body: unknown = JSON.parse(await req.text());
        const header = req.headers.get("authorization");
        const handler = url.pathname === "/push" ? handlePush : handlePull;
        const result = await handler(db, header, body);
        return new Response(JSON.stringify(result.body), { status: result.status, headers: { "content-type": "application/json" } });
      });
    });
    afterEach(async () => {
      vi.stubGlobal("fetch", realFetch);
      await Promise.all(stores.splice(0).map((s) => s.close()));
    });

    function device(name: string, key: string): PresetStore {
      keyOf[name] = key;
      const store = createPresetStore({
        apiKey: key,
        kvStore: "mem",
        device: `${name}-${(seq += 1)}`,
        pushURL: `http://${name}.test/push`,
        pullURL: `http://${name}.test/pull`,
        pullInterval: null,
      });
      stores.push(store);
      return store;
    }
    const names = (s: PresetStore) => s.query(listPresets).then((l) => l.map((p) => p.name));
    const waitFor = async (check: () => Promise<boolean>) => {
      for (let i = 0; i < 400; i += 1) {
        if (await check()) return;
        await new Promise((r) => setTimeout(r, 50));
      }
      throw new Error("condition not reached in 20 s");
    };

    it("presets saved with no network show at once, reach the server after reconnect, and appear on a second device", async () => {
      const laptop = device("laptop", KEY_A);
      offline.add("laptop");
      await laptop.mutate.savePreset({ id: "preset-0001", name: "Monthly CSV", format: "csv", includeSummary: false });
      await laptop.mutate.savePreset({ id: "preset-0002", name: "Board PDF", format: "pdf", includeSummary: true });
      // local first: both are readable on this device with the network down, and the server has none
      expect(await names(laptop)).toEqual(["Board PDF", "Monthly CSV"]);
      await laptop.push();
      expect(await rows(TENANT_A)).toEqual([]);
      expect((await laptop.experimentalPendingMutations()).map((m) => m.name)).toEqual(["savePreset", "savePreset"]);

      offline.delete("laptop");
      await waitFor(async () => {
        await laptop.push();
        return (await rows(TENANT_A)).length === 2;
      });
      await laptop.pull();
      expect((await rows(TENANT_A)).map((r) => r.name)).toEqual(["Monthly CSV", "Board PDF"]);
      await waitFor(async () => (await laptop.experimentalPendingMutations()).length === 0);
      expect(await names(laptop)).toEqual(["Board PDF", "Monthly CSV"]);

      const phone = device("phone", KEY_A);
      await phone.pull();
      await waitFor(async () => (await names(phone)).length === 2);
      expect(await names(phone)).toEqual(["Board PDF", "Monthly CSV"]);
      expect(await phone.query(listPresets)).toContainEqual({ id: "preset-0002", name: "Board PDF", format: "pdf", includeSummary: true });
    });

    it("a delete made offline on one device removes the preset from the other after both sync", async () => {
      const laptop = device("laptop", KEY_A);
      const phone = device("phone", KEY_A);
      await laptop.mutate.savePreset({ id: "preset-0001", name: "Monthly CSV", format: "csv", includeSummary: false });
      await laptop.push();
      await phone.pull();
      await waitFor(async () => (await names(phone)).length === 1);

      offline.add("phone");
      await phone.mutate.deletePreset({ id: "preset-0001" });
      expect(await names(phone)).toEqual([]);
      offline.delete("phone");
      await phone.push();
      await laptop.pull();
      await waitFor(async () => (await names(laptop)).length === 0);
      expect((await rows(TENANT_A)).map((r) => r.deleted)).toEqual([true]);
    });

    it("another tenant's device never receives these presets", async () => {
      const a = device("laptop", KEY_A);
      const b = device("bravo", KEY_B);
      await a.mutate.savePreset({ id: "preset-0001", name: "Alpha only", format: "csv", includeSummary: false });
      await a.push();
      await b.pull();
      await new Promise((r) => setTimeout(r, 200));
      expect(await names(b)).toEqual([]);
    });

    it("the client refuses an invalid preset before it is stored or queued", async () => {
      const laptop = device("laptop", KEY_A);
      // @ts-expect-error a format the type does not allow, to prove the runtime check
      await expect(laptop.mutate.savePreset({ id: "preset-0001", name: "Bad", format: "docx", includeSummary: false })).rejects.toThrow();
      expect(await names(laptop)).toEqual([]);
    });
  });
});
