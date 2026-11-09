// Mutation check for the web code: break each guard on purpose and prove a test goes red.
//
// A green suite is not evidence that a guard is tested. Each mutant below deletes or weakens ONE guard
// (a tenant setting, a figure check, a retry, a signature header, a compiler flag) in the real source, runs
// the tests that are meant to cover it, and expects a failure. The original bytes are written back after
// every mutant, on a normal exit, on Ctrl-C and on SIGTERM.
//
// THIS EDITS SOURCE FILES IN PLACE. Run it in a clean checkout or a copy, never in a tree you are editing;
// a killed process (power loss, kill -9) can leave a weakened guard behind, and `git diff` shows it.
//
//   node scripts/mutation_check.mjs --check-targets    only confirm every mutant finds its text exactly once
//   node scripts/mutation_check.mjs --list             print the mutants
//   node scripts/mutation_check.mjs --only db,guard    only mutants whose name starts with one of these
//   node scripts/mutation_check.mjs                    every mutant (needs Postgres on 5433 and the S3 stand-in)
//
// Exit status: 0 when every mutant was caught, 1 when one survived, 2 on a bad argument or a broken mutant.
import { spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const vitestBin = path.join(web, "node_modules", "vitest", "vitest.mjs");

const T = {
  iso: "tests/tenant-isolation.test.ts",
  exp: "tests/export.test.ts",
  fmt: "tests/format.test.ts",
  guard: "tests/guard.test.ts",
  sum: "tests/summary.test.ts",
  s3: "tests/s3.test.ts",
  search: "tests/search.test.ts",
  strict: "tests/typescript-strict.test.ts",
};

/** One edit: the text must occur exactly once in the file. */
const e = (file, find, replace) => ({ file, find, replace });

const MUTANTS = [
  // ---- the tenant boundary (lib/server/db.ts, auth.ts, data.ts)
  { name: "db.no_set_config", tests: [T.iso], edits: [e("lib/server/db.ts", "    await sql`select set_config('app.tenant_id', ${tenantId}, true)`.execute(trx);\n    return fn(trx);", "    return fn(trx);")] },
  { name: "db.session_scoped_setting", tests: [T.iso], edits: [e("lib/server/db.ts", "set_config('app.tenant_id', ${tenantId}, true)", "set_config('app.tenant_id', ${tenantId}, false)")] },
  { name: "db.no_role_switch", tests: [T.iso], edits: [e("lib/server/db.ts", "    await sql`set local role ${sql.id(role)}`.execute(trx);\n    await sql`select set_config", "    await sql`select set_config")] },
  { name: "db.no_superuser_refusal", tests: [T.iso], edits: [e("lib/server/db.ts", "if (row.rolsuper || row.rolbypassrls) {", "if (false) {")] },
  { name: "db.no_uuid_check", tests: [T.iso], edits: [e("lib/server/db.ts", "if (!tenantId || !UUID.test(tenantId)) {", "if (false) {")] },
  { name: "auth.revoked_key_accepted", tests: [T.iso], edits: [e("lib/server/auth.ts", 'if (row.revoked_at !== null) throw new AuthError(401, "API key revoked");', "")] },
  { name: "auth.wrong_hash", tests: [T.iso], edits: [e("lib/server/auth.ts", 'createHash("sha256")', 'createHash("sha1")')] },
  { name: "data.gauges_included", tests: [T.iso], edits: [e("lib/report/data.ts", '    .where("m.kind", "=", "delta")\n', "")] },
  { name: "data.month_end_inclusive", tests: [T.iso], edits: [e("lib/report/data.ts", '.where("s.observed_on", "<", end)', '.where("s.observed_on", "<=", end)')] },
  { name: "data.fold_overwrites", tests: [T.iso], edits: [e("lib/report/data.ts", "if (seen) seen.value += r.value;", "if (seen) seen.value = r.value;")] },
  { name: "data.empty_month_exported", tests: [T.iso, T.exp], edits: [e("lib/report/data.ts", "if (daily.length === 0) throw new NoDataError(month);", "")] },

  // ---- the export and its input (lib/report/export.ts, input.ts)
  { name: "export.foreign_attachment_allowed", tests: [T.exp], edits: [e("lib/report/export.ts", "if (req.attachment && !req.attachment.objectKey.startsWith(`tenants/${principal.tenantId}/`)) {", "if (false) {")] },
  { name: "export.future_month_allowed", tests: [T.exp], edits: [e("lib/report/export.ts", "if (!monthNotInFuture(req.month, now)) {", "if (false) {")] },
  { name: "export.no_data_not_mapped", tests: [T.exp], edits: [e("lib/report/export.ts", "if (err instanceof NoDataError) {", "if (false) {")] },
  { name: "export.format_not_pinned", tests: [T.exp], edits: [e("lib/report/export.ts", "if (onlyFormat !== undefined && req.format !== onlyFormat) {", "if (false) {")] },
  { name: "input.month_any_two_digits", tests: [T.exp], edits: [e("lib/report/input.ts", "export const MONTH = /^(20\\d{2})-(0[1-9]|1[0-2])$/;", "export const MONTH = /^(20\\d{2})-\\d{1,2}.*$/;")] },
  { name: "input.apikey_unchecked", tests: [T.exp], edits: [e("lib/report/input.ts", '.min(8, "Enter your API key")', "")] },

  // ---- file formats (csv.ts, format.ts, pdf.ts)
  { name: "csv.formula_not_defused", tests: [T.fmt], edits: [e("lib/report/csv.ts", `if (!numeric && /^[=+\\-@\\t\\r]/.test(cell)) cell = "'" + cell;`, "")] },
  { name: "csv.quote_not_doubled", tests: [T.fmt], edits: [e("lib/report/csv.ts", "cell.replace(/\\x22/g, Q + Q)", "cell")] },
  { name: "format.jpy_has_cents", tests: [T.fmt], edits: [e("lib/report/format.ts", '"JPY", "KRW"', '"KRW"')] },
  { name: "format.cents_not_padded", tests: [T.fmt, T.exp], edits: [e("lib/report/format.ts", '.toString().padStart(exponent, "0")', ".toString()")] },
  { name: "pdf.no_page_break", tests: [T.fmt], edits: [e("lib/report/pdf.ts", "if (y - needed < MARGIN) {", "if (false) {")] },

  // ---- the figure guard (lib/report/guard.ts)
  { name: "guard.digits_not_read", tests: [T.guard], edits: [e("lib/report/guard.ts", "return text.match(DIGITS) ?? [];", "return [];")] },
  { name: "guard.words_not_read", tests: [T.guard, T.sum], edits: [e("lib/report/guard.ts", "for (const [written, value] of wordTokens(text)) {", "for (const [written, value] of [] as Array<[string, string]>) {")] },
  { name: "guard.totals_not_allowed", tests: [T.guard, T.sum], edits: [e("lib/report/guard.ts", "    add(plainAmount(t.total, t.unit, t.currency));\n", "")] },
  { name: "guard.daily_not_allowed", tests: [T.guard], edits: [e("lib/report/guard.ts", "  for (const r of data.daily) add(plainAmount(r.value, r.unit, r.currency));\n", "")] },
  { name: "guard.any_date_accepted", tests: [T.guard], edits: [e("lib/report/guard.ts", '(days.has(d) ? " " : m)', '" "')] },
  { name: "guard.commas_not_stripped", tests: [T.guard], edits: [e("lib/report/guard.ts", 'let s = raw.replace(/,/g, "").replace(/^[-+]/, "");', 'let s = raw.replace(/^[-+]/, "");')] },
  { name: "guard.other_script_digits_ignored", tests: [T.guard], edits: [e("lib/report/guard.ts", 'flag("non-ASCII digit");', "")] },
  { name: "guard.no_unicode_normalisation", tests: [T.guard], edits: [e("lib/report/guard.ts", 'original.normalize("NFKC")', "original")] },
  { name: "guard.one_always_a_figure", tests: [T.guard], edits: [e("lib/report/guard.ts", '(words[i + 1] === "of" || words[i + 1] === "another" || words[i - 1] === "no")', "false")] },

  // ---- the summary (lib/report/summary.ts)
  { name: "summary.no_retry", tests: [T.sum, T.exp], edits: [e("lib/report/summary.ts", "export const MAX_ATTEMPTS = 2;", "export const MAX_ATTEMPTS = 1;")] },
  { name: "summary.guard_verdict_ignored", tests: [T.sum, T.exp], edits: [e("lib/report/summary.ts", "if (verdict.ok) {", "if (true) {")] },
  { name: "summary.fallback_is_not_a_template", tests: [T.sum, T.exp], edits: [e("lib/report/summary.ts", 'summary: templateSummary(data, `${reason}; tried ${plural(attempts, "time")}`),', 'summary: { source: "ai", headline: "x", highlights: [] },')] },
  { name: "summary.template_claims_ai", tests: [T.sum], edits: [e("lib/report/summary.ts", 'source: "template",\n    headline: `Stored figures', 'source: "ai",\n    headline: `Stored figures')] },
  { name: "summary.retry_not_told_why", tests: [T.sum], edits: [e("lib/report/summary.ts", "prompt: `${dataForPrompt(data)}${feedback}`,", "prompt: dataForPrompt(data),")] },
  { name: "summary.any_metric_key_accepted", tests: [T.sum], edits: [e("lib/report/summary.ts", "metricKey: z.enum([first, ...rest])", "metricKey: z.string()")] },
  { name: "summary.no_key_still_calls_model", tests: [T.sum], edits: [e("lib/report/summary.ts", "if (!model) {", "if (false) {")] },

  // ---- the presigned upload (lib/s3)
  { name: "s3.type_and_length_unsigned", tests: [T.s3], env: { WEB_REQUIRE_S3: "1" }, edits: [e("lib/s3/presign.ts", 'signableHeaders: new Set(["content-type", "content-length"]),', "signableHeaders: new Set<string>(),")] },
  { name: "s3.expiry_ignored", tests: [T.s3], env: { WEB_REQUIRE_S3: "1" }, edits: [e("lib/s3/presign.ts", "expiresIn: expiresInSeconds,", "expiresIn: 3600,")] },
  { name: "s3.foreign_read_link_signed", tests: [T.s3], env: { WEB_REQUIRE_S3: "1" }, edits: [e("lib/s3/presign.ts", "if (!objectKey.startsWith(`tenants/${tenantId}/`)) throw", "if (false) throw")] },
  { name: "s3.hidden_file_names_kept", tests: [T.s3], env: { WEB_REQUIRE_S3: "1" }, edits: [e("lib/s3/presign.ts", '.replace(/^\\.+/, "")', "")] },
  { name: "s3.key_outside_tenant_prefix", tests: [T.s3], env: { WEB_REQUIRE_S3: "1" }, edits: [e("lib/s3/presign.ts", "return `tenants/${tenantId}/reports/", "return `shared/${tenantId}/reports/")] },
  { name: "s3.checksum_of_empty_body_signed", tests: [T.s3], env: { WEB_REQUIRE_S3: "1" }, edits: [e("lib/s3/presign.ts", '    requestChecksumCalculation: "WHEN_REQUIRED",\n', "")] },
  { name: "s3.size_limit_dropped", tests: [T.s3], env: { WEB_REQUIRE_S3: "1" }, edits: [e("lib/report/input.ts", ".max(MAX_UPLOAD_BYTES)", "")] },
  { name: "s3.any_content_type", tests: [T.s3], env: { WEB_REQUIRE_S3: "1" }, edits: [e("lib/report/input.ts", "contentType: z.enum(UPLOAD_TYPES),", "contentType: z.string(),")] },

  // ---- AI search (lib/server/search.ts)
  { name: "search.no_iterative_scan", tests: [T.search], edits: [e("lib/server/search.ts", "  await sql`select set_config('hnsw.iterative_scan', 'strict_order', true)`.execute(trx);\n", "")] },
  { name: "search.embed_wrong_bit", tests: [T.search], edits: [e("lib/server/search.ts", "((n >> 32n) & 1n) === 1n", "((n >> 31n) & 1n) === 1n")] },
  { name: "search.embed_utf16_trigrams", tests: [T.search], edits: [e("lib/server/search.ts", "Array.from(`  ${t} `)", "`  ${t} `.split(\"\")")] },
  { name: "search.limit_unchecked", tests: [T.search], edits: [e("lib/server/search.ts", "if (!Number.isInteger(limit) || limit < 1 || limit > 200) throw", "if (false) throw")] },

  // ---- the compiler and the linter
  { name: "tsc.strict_off", tests: [T.strict], edits: [e("tsconfig.json", '"strict": true,\n    "noImplicitAny": true,', '"strict": false,\n    "noImplicitAny": false,')] },
  { name: "lint.any_is_a_warning", lint: true, edits: [e("lint/eslint.config.mjs", '"@typescript-eslint/no-explicit-any": "error"', '"@typescript-eslint/no-explicit-any": "warn"')] },
  { name: "lint.any_is_off", lint: true, edits: [e("lint/eslint.config.mjs", '"@typescript-eslint/no-explicit-any": "error"', '"@typescript-eslint/no-explicit-any": "off"')] },
];

// ---------------------------------------------------------------------------------------------

const args = process.argv.slice(2);
const known = new Set(["--check-targets", "--list", "--only"]);
const flags = new Set();
let only = null;
for (let i = 0; i < args.length; i += 1) {
  const a = args[i];
  if (!known.has(a)) {
    console.error(`unknown argument: ${a}`);
    process.exit(2);
  }
  if (a === "--only") {
    only = (args[i + 1] ?? "").split(",").filter(Boolean);
    if (only.length === 0) {
      console.error("--only needs a comma separated list");
      process.exit(2);
    }
    i += 1;
  } else flags.add(a);
}

const names = new Set();
for (const m of MUTANTS) {
  if (names.has(m.name)) {
    console.error(`duplicate mutant name ${m.name}`);
    process.exit(2);
  }
  names.add(m.name);
}

const selected = only ? MUTANTS.filter((m) => only.some((p) => m.name.startsWith(p))) : MUTANTS;
if (only && selected.length === 0) {
  console.error("no mutant matches --only");
  process.exit(2);
}

if (flags.has("--list")) {
  for (const m of selected) console.log(m.name);
  process.exit(0);
}

// Matching is done on LF text, so a file with CRLF (or mixed) line endings is found the same way; the original bytes are what is restored.
const readRaw = (file) => readFileSync(path.join(web, file), "utf8");
const read = (file) => readRaw(file).replace(/\r\n/g, "\n");
const write = (file, text) => writeFileSync(path.join(web, file), text);

function occurrences(text, find) {
  let n = 0;
  for (let at = text.indexOf(find); at !== -1; at = text.indexOf(find, at + find.length)) n += 1;
  return n;
}

let bad = 0;
for (const m of selected) {
  for (const ed of m.edits) {
    const n = occurrences(read(ed.file), ed.find);
    if (n !== 1) {
      console.error(`BROKEN TARGET ${m.name}: ${ed.file} holds the text ${n} times, not once`);
      bad += 1;
    }
  }
}
if (bad > 0) process.exit(2);
if (flags.has("--check-targets")) {
  console.log(`${selected.length} mutants, every target found exactly once`);
  process.exit(0);
}

// ---- run

const originals = new Map();
function restoreAll() {
  for (const [file, text] of originals) write(file, text);
  originals.clear();
}
process.on("exit", restoreAll);
for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    restoreAll();
    process.exit(130);
  });
}

function run(m) {
  const env = { ...process.env, WEB_REQUIRE_DB: "1", ...(m.env ?? {}) };
  if (m.lint) return spawnSync(process.execPath, ["--test", "lint/lint.test.mjs"], { cwd: web, env, encoding: "utf8" });
  return spawnSync(process.execPath, [vitestBin, "run", ...m.tests, "--reporter=dot"], { cwd: web, env, encoding: "utf8" });
}

function failed(m, res) {
  const out = `${res.stdout}\n${res.stderr}`;
  if (res.status === 0) return false;
  if (/SyntaxError|Transform failed|Failed to parse|Cannot find module|Unexpected token/.test(out)) return "broken";
  if (m.lint) return /(# fail|ℹ fail) [1-9]/.test(out);
  return /(Tests|Test Files)\s+[^\n]*\d+ failed/.test(out);
}

// the unmutated code must pass first, or a "caught" means nothing
const baseGroups = new Map();
for (const m of selected) baseGroups.set(JSON.stringify([m.lint ?? false, m.tests ?? [], m.env ?? {}]), m);
for (const m of baseGroups.values()) {
  const res = run(m);
  if (res.status !== 0) {
    console.error(`BASELINE FAILS for ${m.lint ? "lint" : m.tests.join(" ")}; fix the tests before mutating`);
    console.error(`${res.stdout}\n${res.stderr}`.slice(-3000));
    process.exit(2);
  }
}

const results = [];
for (const m of selected) {
  for (const ed of m.edits) {
    if (!originals.has(ed.file)) originals.set(ed.file, readRaw(ed.file));
    write(ed.file, read(ed.file).replace(ed.find, () => ed.replace));
  }
  let verdict;
  try {
    const res = run(m);
    const f = failed(m, res);
    verdict = f === "broken" ? "BROKEN" : f ? "caught" : "SURVIVED";
  } finally {
    restoreAll();
  }
  results.push([m.name, verdict]);
  console.log(`${verdict.padEnd(9)} ${m.name}`);
}

const caught = results.filter(([, v]) => v === "caught").length;
const survived = results.filter(([, v]) => v === "SURVIVED").map(([n]) => n);
const broken = results.filter(([, v]) => v === "BROKEN").map(([n]) => n);
console.log(`\n${caught} of ${results.length} mutants caught`);
if (survived.length) console.log(`survived: ${survived.join(", ")}`);
if (broken.length) console.log(`broken (did not compile): ${broken.join(", ")}`);
process.exit(broken.length ? 2 : survived.length ? 1 : 0);
