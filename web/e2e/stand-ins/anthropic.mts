/**
 * A stand-in for the Anthropic Messages API, for the browser test. Written from the
 * Messages API reference and from the request the Vercel AI SDK's Anthropic provider sends
 * (captured once: POST /v1/messages with `output_config.format` holding a JSON schema), not
 * from a real model. It is a script, not a model.
 *
 * It answers the summary request by reading the totals out of the prompt:
 *   - for most months it writes a summary that quotes only the totals it was given;
 *   - for the month 2026-09 it always invents a figure, so the figure guard has something to refuse
 *     and the browser test can watch the retry and the fallback happen.
 * GET /__log lists every request it has seen, so a test can say how many calls were made and
 * what the prompt held. It checks the x-api-key and anthropic-version headers like the real service.
 *
 * Run: node e2e/stand-ins/anthropic.mts   (PORT, default 4010). Needs Node 22.18 or newer, which
 * strips the type annotations itself.
 */
import http from "node:http";

type Block = { type: string; text?: string };
type Message = { role: string; content: string | Block[] };
type Body = {
  model?: string;
  messages?: Message[];
  output_config?: { format?: { schema?: { properties?: { highlights?: { items?: { properties?: { metricKey?: { enum?: string[] } } } } } } } };
};
type Seen = { model: string; month: string; prompt: string };

const KEY = "stand-in-key";
const seen: Seen[] = [];

function promptText(body: Body): string {
  const last = [...(body.messages ?? [])].reverse().find((m) => m.role === "user");
  if (!last) return "";
  if (typeof last.content === "string") return last.content;
  return last.content.map((b) => b.text ?? "").join("\n");
}

function reply(body: Body): string {
  const prompt = promptText(body);
  const month = /Report month: .*\((\d{4}-\d{2})\)/.exec(prompt)?.[1] ?? "";
  const monthLabel = /Report month: (.+?) \(/.exec(prompt)?.[1] ?? "This month";
  const keys = body.output_config?.format?.schema?.properties?.highlights?.items?.properties?.metricKey?.enum ?? [];
  const totals = [...prompt.matchAll(/^- metricKey (\S+) \| (.+?) \| total (\S+)(?: [A-Z]{3})? \| days with data (\d+)$/gm)].map((m) => ({
    key: m[1] ?? "",
    label: m[2] ?? "",
    total: m[3] ?? "",
    days: m[4] ?? "",
  }));
  seen.push({ model: body.model ?? "", month, prompt });
  if (month === "2026-09") {
    return JSON.stringify({
      headline: `${monthLabel} was a strong month`,
      highlights: [{ metricKey: keys[0] ?? "orders", text: "Orders grew 40% to 61, the best month yet." }],
    });
  }
  return JSON.stringify({
    headline: `${monthLabel} totals`,
    highlights: totals.slice(0, 6).map((t) => ({ metricKey: t.key, text: `${t.label} totalled ${t.total} over ${t.days} days.` })),
  });
}

const server = http.createServer((req, res) => {
  const json = (status: number, payload: unknown): void => {
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify(payload));
  };
  if (req.method === "GET" && req.url === "/__health") return json(200, { ok: true });
  if (req.method === "GET" && req.url === "/__log") return json(200, seen);
  if (req.method === "DELETE" && req.url === "/__log") {
    seen.length = 0;
    return json(200, { cleared: true });
  }
  if (req.method !== "POST" || req.url !== "/v1/messages") return json(404, { type: "error", error: { type: "not_found_error", message: "no such route" } });
  if (req.headers["x-api-key"] !== KEY) return json(401, { type: "error", error: { type: "authentication_error", message: "invalid x-api-key" } });
  if (!req.headers["anthropic-version"]) return json(400, { type: "error", error: { type: "invalid_request_error", message: "anthropic-version header is required" } });
  let raw = "";
  req.on("data", (chunk: Buffer) => {
    raw += chunk.toString("utf8");
  });
  req.on("end", () => {
    let body: Body;
    try {
      body = JSON.parse(raw) as Body;
    } catch {
      return json(400, { type: "error", error: { type: "invalid_request_error", message: "body is not JSON" } });
    }
    json(200, {
      id: `msg_stand_in_${seen.length + 1}`,
      type: "message",
      role: "assistant",
      model: body.model ?? "stand-in",
      content: [{ type: "text", text: reply(body) }],
      stop_reason: "end_turn",
      stop_sequence: null,
      usage: { input_tokens: 100, output_tokens: 50 },
    });
  });
});

const port = Number(process.env.PORT ?? 4010);
server.listen(port, "127.0.0.1", () => {
  process.stdout.write(`anthropic stand-in on http://127.0.0.1:${port}\n`);
});
