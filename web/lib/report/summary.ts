/**
 * The written summary on an export.
 *
 * A model drafts it through the Vercel AI SDK (`generateText` with `Output.object`,
 * so the reply is a Zod-validated object, not free text). The figure guard
 * (guard.ts) then reads every sentence. A summary that cites a number the data does
 * not hold is sent back once with the offending figures named. If the second try is
 * also refused, or the model is unreachable, or no key is set, the export carries a
 * plain template built from the totals, and says in every format that it did.
 *
 * The template is built from the same data and passes the same guard; a test holds that
 * over generated data.
 */
import { generateText, Output, type LanguageModel } from "ai";
import { createAnthropic } from "@ai-sdk/anthropic";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import { z } from "zod";
import type { ReportData } from "@/lib/report/data";
import { checkFigures } from "@/lib/report/guard";
import { displayAmount, monthName, plainAmount } from "@/lib/report/format";
import type { ReportSummary } from "@/lib/report/summary-types";

export const MAX_ATTEMPTS = 2;

export const DEFAULT_MODEL = "claude-haiku-4-5";

/** The model for this process, or null when no key is configured (then the template is used). */
export function modelFromEnv(env: Record<string, string | undefined> = process.env): LanguageModel | null {
  const apiKey = env.ANTHROPIC_API_KEY;
  if (!apiKey) return openAICompatibleFromEnv(env);
  const provider = createAnthropic({ apiKey, ...(env.ANTHROPIC_BASE_URL ? { baseURL: env.ANTHROPIC_BASE_URL } : {}) });
  return provider(env.ANTHROPIC_MODEL ?? DEFAULT_MODEL);
}

/**
 * A second way in for any provider that speaks the OpenAI chat format (Gemini, Groq, OpenRouter, a local server):
 * OPENAI_COMPATIBLE_API_KEY, OPENAI_COMPATIBLE_BASE_URL and OPENAI_COMPATIBLE_MODEL, all three required, used only when no
 * Anthropic key is set. The same guard, retry and template fallback apply to whatever it returns.
 */
function openAICompatibleFromEnv(env: Record<string, string | undefined>): LanguageModel | null {
  const apiKey = env.OPENAI_COMPATIBLE_API_KEY;
  const baseURL = env.OPENAI_COMPATIBLE_BASE_URL;
  const model = env.OPENAI_COMPATIBLE_MODEL;
  if (!apiKey || !baseURL || !model) return null;
  return createOpenAICompatible({ name: "openai-compatible", apiKey, baseURL, supportsStructuredOutputs: true })(model);
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** The fallback: one sentence per total, with nothing but the data's own figures. */
export function templateSummary(data: ReportData, fallbackReason?: string): ReportSummary {
  const highlights = data.totals.map(
    (t) => `${t.label} totalled ${displayAmount(t.total, t.unit, t.currency)} over ${plural(t.daysWithData, "day")} with data.`,
  );
  return {
    source: "template",
    headline: `Stored figures for ${monthName(data.month)}`,
    highlights,
    ...(fallbackReason ? { fallbackReason } : {}),
  };
}

/** The structure the model must return. `metricKey` can only be a metric that is in the data. */
export function summarySchema(data: ReportData) {
  const keys = [...new Set(data.totals.map((t) => t.metricKey))];
  const [first, ...rest] = keys;
  if (first === undefined) throw new Error("no metrics to summarise");
  return z.object({
    headline: z.string().min(1).max(200),
    highlights: z
      .array(z.object({ metricKey: z.enum([first, ...rest]), text: z.string().min(1).max(300) }))
      .min(1)
      .max(6),
  });
}

/** The data as the model sees it: the same plain figures the guard will accept. */
export function dataForPrompt(data: ReportData): string {
  const lines: string[] = [`Report month: ${monthName(data.month)} (${data.month})`, "", "Month totals:"];
  for (const t of data.totals) {
    const cur = t.currency ? ` ${t.currency}` : "";
    lines.push(`- metricKey ${t.metricKey} | ${t.label} | total ${plainAmount(t.total, t.unit, t.currency)}${cur} | days with data ${t.daysWithData}`);
  }
  lines.push("", "Daily values (date | metricKey | value):");
  for (const r of data.daily) lines.push(`${r.date} | ${r.metricKey} | ${plainAmount(r.value, r.unit, r.currency)}`);
  return lines.join("\n");
}

// COPY-OK: model instructions, not screen text
const SYSTEM = [
  "You write the short summary at the top of a monthly analytics report.",
  "Use only figures that appear in the data below. Copy each one as written, in digits.",
  "Do not round, add, subtract, average, or give a percentage. Do not write a number as a word.",
  "Do not name a cause the data does not show. Write plain, specific sentences.",
].join(" ");

export type SummaryOutcome = {
  summary: ReportSummary;
  /** How many model calls were made. */
  attempts: number;
  /** The figures the guard refused, one list per refused attempt. */
  refused: string[][];
  /** The last failure in full, for a log. The report shows only the short reason. */
  detail: string;
};

function describe(err: unknown): string {
  return err instanceof Error ? err.message.slice(0, 160) : "unknown error";
}

export async function generateSummary(
  data: ReportData,
  model: LanguageModel | null,
  maxAttempts: number = MAX_ATTEMPTS,
): Promise<SummaryOutcome> {
  if (!model) {
    return { summary: templateSummary(data, "no model key is configured"), attempts: 0, refused: [], detail: "no model key is configured" };
  }
  const schema = summarySchema(data);
  const refused: string[][] = [];
  let feedback = "";
  let lastFailure = "";
  let reason = "";
  let attempts = 0;
  for (let n = 0; n < maxAttempts; n += 1) {
    attempts += 1;
    try {
      const result = await generateText({
        model,
        system: SYSTEM,
        prompt: `${dataForPrompt(data)}${feedback}`,
        output: Output.object({ schema }),
        maxRetries: 0,
      });
      const out = result.output;
      const verdict = checkFigures([out.headline, ...out.highlights.map((h) => h.text)], data);
      if (verdict.ok) {
        return {
          summary: { source: "ai", headline: out.headline, highlights: out.highlights.map((h) => h.text) },
          attempts,
          refused,
          detail: "",
        };
      }
      refused.push(verdict.violations);
      lastFailure = `the summary cited figures not in the data (${verdict.violations.join(", ")})`;
      reason = "the model's summary cited figures that are not in the data";
      // COPY-OK: model instructions, not screen text
      feedback = `\n\nRefused. These figures are not in the data: ${verdict.violations.join(", ")}. Write it again with figures from the data only.`;
    } catch (err) {
      lastFailure = `the model call failed (${describe(err)})`;
      reason = "the model call failed";
      // COPY-OK: model instructions, not screen text
      feedback = `\n\nThe last reply was not a valid summary object (${describe(err)}). Return the object in the format given.`;
    }
  }
  return {
    summary: templateSummary(data, `${reason}; tried ${plural(attempts, "time")}`),
    attempts,
    refused,
    detail: lastFailure,
  };
}
