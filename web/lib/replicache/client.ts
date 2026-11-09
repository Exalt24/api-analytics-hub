/**
 * The browser half: the Replicache instance for one API key, its mutators and the read query.
 * Replicache keeps the data in IndexedDB, applies a mutator to it at once (so the page works with no
 * network), and sends the queued mutations to /api/replicache/push when it can.
 */
import { Replicache, type ReadTransaction, type WriteTransaction } from "replicache";
import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { PRESET_PREFIX, SCHEMA_VERSION, deleteArgsSchema, presetKey, presetSchema, type Preset } from "@/lib/replicache/preset";

export const mutators = {
  /** Create or overwrite a preset. The same Zod schema the server uses refuses a bad one here too. */
  async savePreset(tx: WriteTransaction, args: Preset): Promise<void> {
    const p = presetSchema.parse(args);
    await tx.set(presetKey(p.id), p);
  },
  async deletePreset(tx: WriteTransaction, args: { id: string }): Promise<void> {
    await tx.del(presetKey(deleteArgsSchema.parse(args).id));
  },
};
export type PresetMutators = typeof mutators;
export type PresetStore = Replicache<PresetMutators>;

/** The local database is named from a hash of the key, so two keys in one browser never share presets and the key itself is not in a database name. */
export function storeName(apiKey: string): string {
  return `report-presets-${bytesToHex(sha256(new TextEncoder().encode(apiKey))).slice(0, 16)}`;
}

export type StoreOptions = {
  apiKey: string;
  /** Tells two profiles of one key apart in one browser; tests use it for a second device. */
  device?: string;
  /** "mem" in tests that have no IndexedDB. */
  kvStore?: "idb" | "mem";
  /** Absolute URLs in tests; the same-origin paths by default. */
  pushURL?: string;
  pullURL?: string;
  pullInterval?: number | null;
};

export function createPresetStore(options: StoreOptions): PresetStore {
  return new Replicache({
    name: storeName(options.apiKey) + (options.device ? `-${options.device}` : ""),
    schemaVersion: SCHEMA_VERSION,
    auth: `Bearer ${options.apiKey}`,
    pushURL: options.pushURL ?? "/api/replicache/push",
    pullURL: options.pullURL ?? "/api/replicache/pull",
    mutators,
    kvStore: options.kvStore ?? "idb",
    ...(options.pullInterval !== undefined ? { pullInterval: options.pullInterval } : {}),
  });
}

/** Every preset in the local database, oldest name first. A stored value that is not a preset is skipped. */
export async function listPresets(tx: ReadTransaction): Promise<Preset[]> {
  const out: Preset[] = [];
  for await (const value of tx.scan({ prefix: PRESET_PREFIX }).values()) {
    const parsed = presetSchema.safeParse(value);
    if (parsed.success) out.push(parsed.data);
  }
  return out.sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
}
