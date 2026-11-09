import { describe, expect, it } from "vitest";
import { MockLanguageModelV4 } from "ai/test";
import { MAX_ATTEMPTS, DEFAULT_MODEL, dataForPrompt, generateSummary, modelFromEnv, templateSummary } from "@/lib/report/summary";
import { sampleData } from "./report-data";

const data = sampleData();

/** A model that answers from a script, one entry per call: a JSON object, a raw string, or an Error to throw. */
function scripted(replies: Array<object | string | Error>) {
  let call = 0;
  const model = new MockLanguageModelV4({
    doGenerate: async () => {
      const reply = replies[Math.min(call, replies.length - 1)];
      call += 1;
      if (reply instanceof Error) throw reply;
      const text = typeof reply === "string" ? reply : JSON.stringify(reply);
      return {
        content: [{ type: "text" as const, text }],
        finishReason: { unified: "stop" as const, raw: undefined },
        usage: {
          inputTokens: { total: 10, noCache: 10, cacheRead: undefined, cacheWrite: undefined },
          outputTokens: { total: 20, text: 20, reasoning: undefined },
        },
        warnings: [],
      };
    },
  });
  return model;
}

const good = {
  headline: "October 2026 orders and sales",
  highlights: [
    { metricKey: "orders", text: "Orders totalled 38 over 3 days." },
    { metricKey: "gross_sales", text: "Gross sales were $3,722.21." },
  ],
};
const invented = {
  headline: "October 2026 was strong",
  highlights: [{ metricKey: "orders", text: "Orders grew 27% to 41." }],
};

describe("generateSummary with a scripted model", () => {
  it("uses the model's summary when every figure is in the data, after one call", async () => {
    const model = scripted([good]);
    const out = await generateSummary(data, model);
    expect(model.doGenerateCalls.length).toBe(1);
    expect(out.attempts).toBe(1);
    expect(out.summary).toEqual({ source: "ai", headline: good.headline, highlights: good.highlights.map((h) => h.text) });
    expect(out.refused).toEqual([]);
  });

  it("retries once when a figure is invented, names it to the model, and uses the second try if it is clean", async () => {
    const model = scripted([invented, good]);
    const out = await generateSummary(data, model);
    expect(model.doGenerateCalls.length).toBe(2);
    expect(out.summary.source).toBe("ai");
    expect(out.refused).toEqual([["27", "41"]]);
    const secondPrompt = JSON.stringify(model.doGenerateCalls[1]?.prompt);
    expect(secondPrompt).toContain("27, 41");
  });

  it("falls back to the template after the second refusal, and says so in the summary", async () => {
    const model = scripted([invented, invented, good]); // a third reply exists and must not be asked for
    const out = await generateSummary(data, model);
    expect(model.doGenerateCalls.length).toBe(MAX_ATTEMPTS);
    expect(out.attempts).toBe(2);
    expect(out.summary.source).toBe("template");
    expect(out.summary.fallbackReason).toBe("the model's summary cited figures that are not in the data; tried 2 times");
    expect(out.detail).toContain("27, 41"); // the figures are for the log, not for the client's report
    expect(JSON.stringify(out.summary)).not.toContain("27");
    expect(out.summary.highlights).toEqual(templateSummary(data).highlights);
    expect(out.summary.highlights.join(" ")).not.toContain("27");
  });

  it("refuses an invented number written as a word, then falls back", async () => {
    const words = { headline: "A good month", highlights: [{ metricKey: "orders", text: "There were forty-two orders." }] };
    const out = await generateSummary(data, scripted([words, words]));
    expect(out.summary.source).toBe("template");
    expect(out.refused).toEqual([["forty-two"], ["forty-two"]]);
  });

  it("treats a reply that is not the schema as a failed try: bad JSON, an unknown metric, a missing field", async () => {
    for (const bad of ["not json at all", { headline: "x", highlights: [{ metricKey: "made_up_metric", text: "Fine." }] }, { headline: "x" }]) {
      const model = scripted([bad, good]);
      const out = await generateSummary(data, model);
      expect(model.doGenerateCalls.length, JSON.stringify(bad)).toBe(2);
      expect(out.summary.source).toBe("ai");
    }
    const twice = await generateSummary(data, scripted(["not json at all"]));
    expect(twice.summary.source).toBe("template");
    expect(twice.summary.fallbackReason).toBe("the model call failed; tried 2 times");
  });

  it("falls back, with the reason, when the model call itself throws", async () => {
    const model = scripted([new Error("503 overloaded")]);
    const out = await generateSummary(data, model);
    expect(model.doGenerateCalls.length).toBe(2);
    expect(out.summary).toMatchObject({ source: "template" });
    expect(out.summary.fallbackReason).toBe("the model call failed; tried 2 times");
    expect(out.detail).toContain("503 overloaded");
  });

  it("uses the template with no model call at all when no key is configured", async () => {
    const out = await generateSummary(data, null);
    expect(out.attempts).toBe(0);
    expect(out.summary).toEqual(templateSummary(data, "no model key is configured"));
  });

  it("makes exactly as many calls as it is allowed", async () => {
    const model = scripted([invented]);
    await generateSummary(data, model, 1);
    expect(model.doGenerateCalls.length).toBe(1);
  });

  it("gives the model only the data's own figures in the prompt", () => {
    const prompt = dataForPrompt(data);
    expect(prompt).toContain("metricKey orders | Orders | total 38 | days with data 3");
    expect(prompt).toContain("metricKey gross_sales | Gross sales | total 3722.21 USD | days with data 3");
    expect(prompt).toContain("2026-10-02 | orders | 8");
    expect(prompt).not.toContain("Alpha"); // the tenant name stays out of the prompt
  });
});

describe("the template", () => {
  it("states each total with its day count, in the reader's format, and names no tenant", () => {
    const t = templateSummary(data);
    expect(t.source).toBe("template");
    expect(t.headline).toBe("Stored figures for October 2026");
    expect(t.highlights).toEqual([
      "Gross sales totalled $3,722.21 over 3 days with data.",
      "Orders totalled 38 over 3 days with data.",
      "Refunds totalled $35.50 over 2 days with data.",
    ]);
  });

  it("says 1 day, not 1 days", () => {
    const one = sampleData({ daily: [data.daily[0]!], totals: [{ ...data.totals[0]!, daysWithData: 1 }] });
    expect(templateSummary(one).highlights[0]).toMatch(/over 1 day with data\.$/);
  });
});

describe("modelFromEnv", () => {
  it("is null without a key, and a model for the configured name with one", () => {
    expect(modelFromEnv({})).toBeNull();
    const m = modelFromEnv({ ANTHROPIC_API_KEY: "test-key-not-real" });
    expect(m).not.toBeNull();
    expect(typeof m === "object" && m !== null && "modelId" in m ? m.modelId : "").toBe(DEFAULT_MODEL);
    const named = modelFromEnv({ ANTHROPIC_API_KEY: "test-key-not-real", ANTHROPIC_MODEL: "claude-sonnet-4-5" });
    expect(typeof named === "object" && named !== null && "modelId" in named ? named.modelId : "").toBe("claude-sonnet-4-5");
  });
});
