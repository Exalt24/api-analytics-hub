/**
 * The mutation script is itself a tool that can be wrong, so it is tested: it must refuse an
 * argument it does not know, find every mutant's text exactly once, catch a deliberately weakened
 * guard, and leave the source file byte for byte as it found it.
 */
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const web = path.resolve(__dirname, "..");
const script = path.join(web, "scripts", "mutation_check.mjs");
const lintConfig = path.join(web, "lint", "eslint.config.mjs");

const run = (...args: string[]) => spawnSync(process.execPath, [script, ...args], { cwd: web, encoding: "utf8" });
const sha = (file: string): string => createHash("sha256").update(readFileSync(file)).digest("hex");

describe("scripts/mutation_check.mjs", () => {
  it("refuses an argument it does not know, and edits nothing", () => {
    const before = sha(lintConfig);
    const r = run("--nonsense");
    expect(r.status).toBe(2);
    expect(r.stderr).toContain("unknown argument");
    expect(sha(lintConfig)).toBe(before);
  });

  it("finds the text of every mutant exactly once", () => {
    const r = run("--check-targets");
    expect(r.stderr).toBe("");
    expect(r.status).toBe(0);
    expect(r.stdout).toMatch(/^\d+ mutants, every target found exactly once/);
  });

  it("lists more than 40 mutants, each with a unique name", () => {
    const names = run("--list").stdout.split("\n").filter(Boolean);
    expect(names.length).toBeGreaterThan(40);
    expect(new Set(names).size).toBe(names.length);
  });

  it("catches a lint rule turned off, and writes the file back exactly", () => {
    const before = sha(lintConfig);
    const r = run("--only", "lint.any_is_off");
    expect(r.stdout).toContain("caught    lint.any_is_off");
    expect(r.stdout).toContain("1 of 1 mutants caught");
    expect(r.status).toBe(0);
    expect(sha(lintConfig)).toBe(before);
  });

  it("refuses --only with a name that matches nothing", () => {
    const r = run("--only", "no_such_mutant");
    expect(r.status).toBe(2);
  });
});
