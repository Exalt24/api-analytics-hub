// The lint gate tested: the config must refuse `any` at error level, accept clean code, and the
// project's own files must pass it. Run with `npm run lint:test` from web/.
import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { ESLint } from "eslint";

const here = path.dirname(fileURLToPath(import.meta.url));
const web = path.resolve(here, "..");
const configFile = path.join(here, "eslint.config.mjs");

function linter() {
  return new ESLint({ cwd: web, overrideConfigFile: configFile });
}

test("an explicit any is an error, not a warning", async () => {
  const [res] = await linter().lintText("export const x: any = 1;\n", { filePath: path.join(web, "lib", "probe.ts") });
  const hit = res.messages.find((m) => m.ruleId === "@typescript-eslint/no-explicit-any");
  assert.ok(hit, "the rule did not fire");
  assert.equal(hit.severity, 2);
});

test("any in a generic, a cast and a parameter is each caught", async () => {
  const code = [
    "export function a(v: any) { return v; }",
    "export const b = JSON.parse('1') as any;",
    "export const c: Array<any> = [];",
    "",
  ].join("\n");
  const [res] = await linter().lintText(code, { filePath: path.join(web, "lib", "probe.ts") });
  const n = res.messages.filter((m) => m.ruleId === "@typescript-eslint/no-explicit-any").length;
  assert.equal(n, 3);
});

test("typed code passes", async () => {
  const [res] = await linter().lintText("export const x: unknown = 1;\nexport function f(v: unknown): string { return String(v); }\n", {
    filePath: path.join(web, "lib", "probe.ts"),
  });
  assert.deepEqual(res.messages, []);
});

test("every source file in web/ passes with no warnings", async () => {
  const results = await linter().lintFiles(["app", "components", "lib", "tests", "e2e"].map((d) => `${d}/**/*.{ts,tsx,mts}`));
  const bad = results.filter((r) => r.errorCount + r.warningCount > 0).map((r) => `${path.relative(web, r.filePath)}: ${r.messages.map((m) => m.ruleId).join(", ")}`);
  assert.deepEqual(bad, []);
  assert.ok(results.length > 30, "linted suspiciously few files: " + results.length);
});
