/**
 * What a saved report preset is, and the shapes the browser and the server both check. No React and no
 * database in this file, so the Replicache mutators in the browser and the push handler on the server
 * judge a preset with the same Zod schema.
 */
import { z } from "zod";
import { FORMATS } from "@/lib/report/input";

/** A client-generated id: letters, digits, underscore and hyphen (a uuid fits). Same rule as migrations/015. */
export const ID = /^[A-Za-z0-9_-]{8,64}$/;

export const presetSchema = z.object({
  id: z.string().regex(ID),
  name: z.string().trim().min(1).max(60),
  format: z.enum(FORMATS),
  includeSummary: z.boolean(),
});
export type Preset = z.infer<typeof presetSchema>;

export const deleteArgsSchema = z.object({ id: z.string().regex(ID) });

export const PRESET_PREFIX = "preset/";
export const presetKey = (id: string): string => `${PRESET_PREFIX}${id}`;

/** The mutator names the browser sends and the server runs. Anything else is acknowledged and ignored. */
export const MUTATIONS = { save: "savePreset", remove: "deletePreset" } as const;

export const SCHEMA_VERSION = "1";

export const pushRequestSchema = z.object({
  pushVersion: z.literal(1),
  schemaVersion: z.string(),
  profileID: z.string(),
  clientGroupID: z.string().regex(ID),
  mutations: z
    .array(
      z.object({
        id: z.number().int().min(1),
        name: z.string(),
        args: z.unknown(),
        timestamp: z.number(),
        clientID: z.string().regex(ID),
      }),
    )
    .max(200),
});
export type PushRequest = z.infer<typeof pushRequestSchema>;

export const pullRequestSchema = z.object({
  pullVersion: z.literal(1),
  schemaVersion: z.string(),
  profileID: z.string(),
  clientGroupID: z.string().regex(ID),
  cookie: z.union([z.null(), z.number().int().min(0)]),
});
export type PullRequest = z.infer<typeof pullRequestSchema>;
