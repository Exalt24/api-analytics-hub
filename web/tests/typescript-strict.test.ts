/**
 * The compiler settings are tested by what they refuse, not by reading the file: a probe
 * with an implicit any and an implicit-any callback parameter must fail to compile under the
 * project's tsconfig, and a typed probe must compile.
 */
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";

const web = path.resolve(__dirname, "..");
const tscBin = path.join(web, "node_modules", "typescript", "bin", "tsc");

function compile(source: string): { status: number | null; output: string } {
  const dir = mkdtempSync(path.join(os.tmpdir(), "strict-probe-"));
  writeFileSync(path.join(dir, "probe.ts"), source);
  writeFileSync(
    path.join(dir, "tsconfig.json"),
    JSON.stringify({ extends: path.join(web, "tsconfig.json"), compilerOptions: { incremental: false, plugins: [] }, include: ["probe.ts"] }),
  );
  const run = spawnSync(process.execPath, [tscBin, "--noEmit", "-p", path.join(dir, "tsconfig.json")], { encoding: "utf8" });
  return { status: run.status, output: `${run.stdout}${run.stderr}` };
}

describe("TypeScript strictness of the web project", () => {
  it("refuses an implicit any parameter (TS7006)", () => {
    const r = compile("export function f(x) { return x; }\n");
    expect(r.status).not.toBe(0);
    expect(r.output).toContain("TS7006");
  });

  it("refuses a possibly null value being used (strictNullChecks, TS18047 or TS2531)", () => {
    const r = compile("export function f(x: string | null): number { return x.length; }\n");
    expect(r.status).not.toBe(0);
    expect(r.output).toMatch(/TS18047|TS2531/);
  });

  it("accepts typed code", () => {
    const r = compile("export function f(x: unknown): string { return String(x); }\n");
    expect(r.output).toBe("");
    expect(r.status).toBe(0);
  });

  it("names strict and noImplicitAny in the config, so a reader sees them", () => {
    const text = readFileSync(path.join(web, "tsconfig.json"), "utf8");
    expect(text).toMatch(/"strict": true/);
    expect(text).toMatch(/"noImplicitAny": true/);
  });
});
