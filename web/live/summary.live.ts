/**
 * The export summary through a REAL model, once. The normal tests and the browser tests use a scripted stand-in
 * server so they cost nothing and never flake; this file is the opt-in check that the same code path (generateText
 * with Output.object, the Zod schema, the figure guard, the retry, the template fallback) also works against a live
 * Anthropic-style endpoint. It needs ANTHROPIC_API_KEY (and ANTHROPIC_BASE_URL / ANTHROPIC_MODEL for a gateway) and
 * writes what happened to stdout. It makes real calls, so it is never part of `npm test` or CI.
 */
import { writeFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { generateSummary, modelFromEnv } from "@/lib/report/summary";
import { checkFigures } from "@/lib/report/guard";
import { sampleData } from "../tests/report-data";

describe("summary through a live model", () => {
  it("returns a guarded summary or an honest template, and reports which", async () => {
    const model = modelFromEnv();
    expect(model, "set a model key first, see the README").not.toBeNull();
    const data = sampleData();
    const started = Date.now();
    const out = await generateSummary(data, model);
    const seconds = Math.round((Date.now() - started) / 100) / 10;
    const verdict = checkFigures([out.summary.headline, ...out.summary.highlights], data);
    const report = JSON.stringify(
      { source: out.summary.source, attempts: out.attempts, refusedAttempts: out.refused.length, seconds, headline: out.summary.headline, highlights: out.summary.highlights, guardAgrees: verdict.ok, fallbackReason: out.summary.fallbackReason ?? null, detail: out.detail },
      null,
      2,
    );
    console.log(report);
    if (process.env.LIVE_OUT) writeFileSync(process.env.LIVE_OUT, report);
    expect(verdict.ok).toBe(true);
  });
});
