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
  state: "tests/reports-state.test.ts",
  prov: "tests/reports-provider.test.tsx",
  sync: "tests/replicache-sync.test.ts",
  portal: "tests/portal.test.ts",
  esign: "tests/esign.test.ts",
  inv: "tests/inventory.test.ts",
  invo: "tests/inventory-orders.test.ts",
};

/** One edit: the text must occur exactly once in the file. */
const e = (file, find, replace) => ({ file, find, replace });

const MUTANTS = [
  // ---- subdomain client portals (lib/portal.ts, proxy.ts)
  { name: "portal.slug_not_compared", tests: [T.portal], edits: [e("lib/portal.ts", "if (!tenant || tenant.slug !== slug) {", "if (!tenant) {")] },
  { name: "portal.reserved_labels_open", tests: [T.portal], edits: [e("lib/portal.ts", "if (!LABEL.test(label) || RESERVED_LABELS.has(label)) return null;", "if (!LABEL.test(label)) return null;")] },
  { name: "portal.label_not_checked", tests: [T.portal], edits: [e("lib/portal.ts", "if (!LABEL.test(label) || RESERVED_LABELS.has(label)) return null;", "if (RESERVED_LABELS.has(label)) return null;")] },
  { name: "portal.suffix_without_dot", tests: [T.portal], edits: [e("lib/portal.ts", "const suffix = `.${base}`;", "const suffix = base;")] },
  { name: "portal.port_not_stripped", tests: [T.portal], edits: [e("lib/portal.ts", 'h = h.replace(/:\\d{1,5}$/, "");', "")] },
  { name: "portal.trailing_dot_kept", tests: [T.portal], edits: [e("lib/portal.ts", 'if (h.endsWith(".")) h = h.slice(0, -1);', "")] },
  { name: "proxy.portal_path_open", tests: [T.portal], edits: [e("proxy.ts", 'if (path === "/portal" || path.startsWith("/portal/")) {', "if (false) {")] },
  { name: "proxy.tenant_from_path", tests: [T.portal], edits: [e("proxy.ts", "url.pathname = `/portal/${slug}${path === \"/\" ? \"\" : path}`;", "url.pathname = path;")] },
  // ---- e-signatures in the portal (lib/esign.ts)
  { name: "esign.id_not_checked_on_sign", tests: [T.esign], edits: [e("lib/esign.ts", "Promise<Result<SignOutcome>> {\n  if (!isRequestId(id)) return Promise.resolve({ ok: false, status: 404, message: refusalMessage(404, undefined) });\n", "Promise<Result<SignOutcome>> {\n")] },
  { name: "esign.id_not_checked_on_open", tests: [T.esign], edits: [e("lib/esign.ts", "Promise<Result<SigningRequest>> {\n  if (!isRequestId(id)) return Promise.resolve({ ok: false, status: 404, message: refusalMessage(404, undefined) });\n", "Promise<Result<SigningRequest>> {\n")] },
  { name: "esign.id_pattern_unanchored", tests: [T.esign], edits: [e("lib/esign.ts", "const REQUEST_ID = /^[0-9a-f]{8}-", "const REQUEST_ID = /[0-9a-f]{8}-")] },
  { name: "esign.no_bearer_key", tests: [T.esign], edits: [e("lib/esign.ts", "const out: Record<string, string> = { Authorization: `Bearer ${key}` };", "const out: Record<string, string> = {};")] },
  { name: "esign.agent_not_forwarded", tests: [T.esign], edits: [e("lib/esign.ts", 'if (caller?.userAgent) out["User-Agent"] = caller.userAgent;', "")] },
  { name: "esign.address_not_forwarded", tests: [T.esign], edits: [e("lib/esign.ts", 'if (caller?.forwardedFor) out["X-Forwarded-For"] = caller.forwardedFor;', "")] },
  { name: "esign.consent_not_checked", tests: [T.esign], edits: [e("lib/esign.ts", 'if (data.get("consent") !== "on") return', "if (false) return")] },
  { name: "esign.empty_name_allowed", tests: [T.esign], edits: [e("lib/esign.ts", 'if (!typedName) return { ok: false, message: "Type your full name to sign." };', "")] },
  { name: "esign.key_in_the_url", tests: [T.esign], edits: [e("lib/esign.ts", "`${esignBase()}/api/esign/requests/${id}/sign`,", "`${esignBase()}/api/esign/requests/${id}/sign?key=${key}`,")] },
  { name: "esign.tenant_in_the_body", tests: [T.esign], edits: [e("lib/esign.ts", "    consent: form.consent,\n", "    consent: form.consent,\n    tenant_id: \"x\",\n")] },
  { name: "esign.refusal_status_ignored", tests: [T.esign], edits: [e("lib/esign.ts", "  if (!res.ok) return { ok: false, status: res.status, message: refusalMessage(res.status, await detailOf(res)) };", "")] },
  { name: "esign.network_error_thrown", tests: [T.esign], edits: [e("lib/esign.ts", "    return { ok: false, status: 0, message: refusalMessage(0, undefined) };\n  }\n  if (!res.ok)", "    throw new Error(\"down\");\n  }\n  if (!res.ok)")] },
  { name: "esign.expired_message_lost", tests: [T.esign], edits: [e("lib/esign.ts", 'return "This request has expired.";', 'return "Something went wrong.";')] },
  { name: "esign.redirect_to_another_site", tests: [T.esign], edits: [e("lib/esign.ts", 'path.startsWith("/") && !path.startsWith("//") ? path : "/"', "path")] },
  { name: "esign.post_from_any_origin", tests: [T.esign], edits: [e("lib/esign.ts", "return new URL(origin).host.toLowerCase() === host.toLowerCase();", "return true;")] },
  { name: "esign.redirect_is_not_303", tests: [T.esign], edits: [e("lib/esign.ts", "status: 303,", "status: 302,")] },
  { name: "esign.document_returned_on_error", tests: [T.esign], edits: [e("lib/esign.ts", "return res.ok ? res : null;", "return res;")] },
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

  // ---- the Reports context (lib/report/reports-state.ts, components/ReportsProvider.tsx, ReportForm.tsx, ReportsPanel.tsx)
  { name: "context.format_not_validated", tests: [T.state, T.prov], edits: [e("lib/report/reports-state.ts", "return isFormat(action.format) ? { ...state, format: action.format } : state;", "return { ...state, format: action.format as Format };")] },
  { name: "context.old_result_kept_on_new_export", tests: [T.state], edits: [e("lib/report/reports-state.ts", 'const { error: _e, lastExport: _l, ...rest } = state;\n      return { ...rest, exportStatus: "exporting" };', 'const { error: _e, ...rest } = state;\n      return { ...rest, exportStatus: "exporting" };')] },
  { name: "context.failure_keeps_old_result", tests: [T.state], edits: [e("lib/report/reports-state.ts", 'const { lastExport: _l, ...rest } = state;\n      return { ...rest, exportStatus: "failed", error: action.message };', 'const { ...rest } = state;\n      return { ...rest, exportStatus: "failed", error: action.message };')] },
  { name: "context.hook_works_outside_provider", tests: [T.prov], edits: [e("components/ReportsProvider.tsx", "if (ctx === null) throw new Error(", "if (false) throw new Error(")] },
  { name: "context.export_without_key", tests: [T.prov], edits: [e("components/ReportsProvider.tsx", "if (!s.tenantKey) {", "if (false) {")] },
  { name: "context.always_csv_action", tests: [T.prov], edits: [e("components/ReportsProvider.tsx", "await actions[s.format]({", "await actions.csv({")] },
  { name: "context.file_not_saved", tests: [T.prov], edits: [e("components/ReportsProvider.tsx", "      save(result.base64, result.filename, result.mimeType);\n", "")] },
  { name: "context.double_export_allowed", tests: [T.prov], edits: [e("components/ReportsProvider.tsx", 'if (s.exportStatus === "exporting") return;', "")] },
  { name: "context.other_tab_key_ignored", tests: [T.prov], edits: [e("components/ReportsProvider.tsx", 'window.addEventListener("storage", onStorage);', "")] },
  { name: "context.month_not_shared", tests: [T.prov], edits: [e("components/ReportForm.tsx", "                    setMonth(e.target.value);\n", "")] },
  { name: "context.summary_switch_not_shared", tests: [T.prov], edits: [e("components/ReportForm.tsx", "                    setIncludeSummary(v);\n", "")] },
  { name: "context.key_bar_not_wired", tests: [T.prov], edits: [e("components/ReportsPanel.tsx", "<KeyGate onSet={setTenantKey} />", "<KeyGate onSet={() => undefined} />")] },

  // ---- local-first sync of saved presets (lib/replicache/server.ts, client.ts)
  { name: "sync.replay_applied_twice", tests: [T.sync], edits: [e("lib/replicache/server.ts", "    if (m.id < expected) continue; // already applied: a retried push must not apply it again\n", "")] },
  { name: "sync.skipped_ahead_accepted", tests: [T.sync], edits: [e("lib/replicache/server.ts", "    if (m.id > expected) throw new BadRequest(`mutation ${m.id} arrived before ${expected}`);\n", "")] },
  { name: "sync.invalid_save_stored", tests: [T.sync], edits: [e("lib/replicache/server.ts", "    // An invalid mutation is acknowledged and has no effect, so one bad one cannot block the queue behind it.\n    if (!parsed.success) return;\n", "")] },
  { name: "sync.other_group_client_accepted", tests: [T.sync], edits: [e("lib/replicache/server.ts", '    if (client.client_group_id !== req.clientGroupID) throw new BadRequest("client belongs to another client group");\n', "")] },
  { name: "sync.counter_row_not_locked", tests: [T.sync], edits: [e("lib/replicache/server.ts", '.select("version").forUpdate().executeTakeFirstOrThrow()', '.select("version").executeTakeFirstOrThrow()')] },
  { name: "sync.future_cookie_trusted", tests: [T.sync], edits: [e("lib/replicache/server.ts", "req.cookie === null || req.cookie > current ? 0 : req.cookie", "req.cookie === null ? 0 : req.cookie")] },
  { name: "sync.delete_not_sent", tests: [T.sync], edits: [e("lib/replicache/server.ts", '        if (!full) patch.push({ op: "del", key });\n', "")] },
  { name: "sync.first_pull_without_clear", tests: [T.sync], edits: [e("lib/replicache/server.ts", '= full ? [{ op: "clear" }] : [];', "= [];")] },
  { name: "sync.pull_not_tenant_scoped", tests: [T.sync], edits: [e("lib/replicache/server.ts", "const body = await withTenant(db, tenant, async (trx) => {", "const body = await db.transaction().execute(async (trx) => {")] },
  { name: "sync.push_not_tenant_scoped", tests: [T.sync], edits: [e("lib/replicache/server.ts", "await withTenant(db, tenant, (trx) => push(trx, tenant, parsed.data));", "await db.transaction().execute((trx) => push(trx, tenant, parsed.data));")] },
  { name: "sync.client_accepts_any_preset", tests: [T.sync], edits: [e("lib/replicache/client.ts", "    const p = presetSchema.parse(args);\n", "    const p = args;\n")] },

  // ---- the compiler and the linter
  { name: "tsc.strict_off", tests: [T.strict], edits: [e("tsconfig.json", '"strict": true,\n    "noImplicitAny": true,', '"strict": false,\n    "noImplicitAny": false,')] },
  { name: "lint.any_is_a_warning", lint: true, edits: [e("lint/eslint.config.mjs", '"@typescript-eslint/no-explicit-any": "error"', '"@typescript-eslint/no-explicit-any": "warn"')] },
  { name: "lint.any_is_off", lint: true, edits: [e("lint/eslint.config.mjs", '"@typescript-eslint/no-explicit-any": "error"', '"@typescript-eslint/no-explicit-any": "off"')] },

  // ---- inventory planning page (lib/inventory)
  { name: "inventory.po_supplier_not_filtered", tests: [T.inv], edits: [e("lib/inventory/po.ts", "if (onlySupplier !== undefined && name !== onlySupplier) continue;", "")] },
  { name: "inventory.po_untracked_ordered", tests: [T.inv], edits: [e("lib/inventory/po.ts", 'l.status === "ok" && l.needsReorder &&', "l.needsReorder &&")] },
  { name: "inventory.po_not_needing_reorder_ordered", tests: [T.inv], edits: [e("lib/inventory/po.ts", 'l.status === "ok" && l.needsReorder && l.suggestedQty > 0', 'l.status === "ok" && l.suggestedQty > 0')] },
  { name: "inventory.po_zero_quantity_ordered", tests: [T.inv], edits: [e("lib/inventory/po.ts", "l.needsReorder && l.suggestedQty > 0 && l.supplier", "l.needsReorder && l.supplier")] },
  { name: "inventory.po_lead_time_ignored", tests: [T.inv], edits: [e("lib/inventory/po.ts", "expectedOn: addDays(asOf, lead),", "expectedOn: addDays(asOf, 0),")] },
  { name: "inventory.po_missing_cost_becomes_zero", tests: [T.inv], edits: [e("lib/inventory/po.ts", "lineTotalMinor: l.unitCostMinor === null ? null : l.unitCostMinor * BigInt(l.suggestedQty),", "lineTotalMinor: l.unitCostMinor === null ? 0n : l.unitCostMinor * BigInt(l.suggestedQty),")] },
  { name: "inventory.po_cost_not_multiplied", tests: [T.inv], edits: [e("lib/inventory/po.ts", "l.unitCostMinor * BigInt(l.suggestedQty)", "l.unitCostMinor")] },
  { name: "inventory.po_least_cover_not_first", tests: [T.inv], edits: [e("lib/inventory/po.ts", "(a.coverDays ?? Infinity) - (b.coverDays ?? Infinity) || ", "")] },
  { name: "inventory.po_blank_cost_written_as_zero", tests: [T.inv], edits: [e("lib/inventory/po.ts", 'l.unitCostMinor === null ? "" : plainAmount(l.unitCostMinor', 'l.unitCostMinor === null ? "0" : plainAmount(l.unitCostMinor')] },
  { name: "inventory.export_viewer_may_enter_promo", tests: [T.inv], edits: [e("lib/inventory/export.ts", 'if (principal.role === "viewer") return', "if (false) return")] },
  { name: "inventory.export_tab_kept_in_paste", tests: [T.inv], edits: [e("lib/inventory/export.ts", "const TSV_BAD = /[\\t\\r\\n]/g;", "const TSV_BAD = /[\\r\\n]/g;")] },
  { name: "inventory.export_paste_formula_not_defused", tests: [T.inv], edits: [e("lib/inventory/export.ts", "!numeric && /^[=+\\-@]/.test(clean) ?", "false ?")] },
  { name: "inventory.export_empty_draft_written", tests: [T.inv], edits: [e("lib/inventory/export.ts", "if (orders.length === 0) {", "if (false) {")] },
  { name: "inventory.export_supplier_not_passed", tests: [T.inv], edits: [e("lib/inventory/export.ts", "buildPurchaseOrders(plan.lines, plan.asOf, req.supplier)", "buildPurchaseOrders(plan.lines, plan.asOf)")] },
  { name: "inventory.export_numbers_as_text", tests: [T.inv], edits: [e("lib/inventory/export.ts", "cellRef.value = Number(cellRef.value);", "cellRef.value = cellRef.value;")] },
  { name: "inventory.data_oldest_run_read", tests: [T.inv], edits: [e("lib/inventory/data.ts", '.orderBy("as_of", "desc")', '.orderBy("as_of", "asc")')] },
  { name: "inventory.data_every_run_mixed", tests: [T.inv], edits: [e("lib/inventory/data.ts", '.where("l.run_id", "=", run.id)', "")] },
  { name: "inventory.data_shortages_not_first", tests: [T.inv], edits: [e("lib/inventory/data.ts", "if (sa !== sb) return sa - sb;", "")] },
  { name: "inventory.data_reorder_not_next", tests: [T.inv], edits: [e("lib/inventory/data.ts", "if (a.needsReorder !== b.needsReorder) return a.needsReorder ? -1 : 1;", "")] },
  { name: "inventory.data_no_sales_sorts_first", tests: [T.inv], edits: [e("lib/inventory/data.ts", "const ca = a.coverDays ?? Number.POSITIVE_INFINITY;", "const ca = a.coverDays ?? 0;")] },
  { name: "inventory.data_reorder_count_ignores_quantity", tests: [T.inv], edits: [e("lib/inventory/data.ts", "l.needsReorder && l.suggestedQty > 0).length", "l.needsReorder).length")] },
  { name: "inventory.input_zero_factor_allowed", tests: [T.inv], edits: [e("lib/inventory/input.ts", '.gt(0, "The factor must be above 0")', '.gte(0, "The factor must be above 0")')] },
  { name: "inventory.input_factor_cap_dropped", tests: [T.inv], edits: [e("lib/inventory/input.ts", '.max(20, "The factor can be at most 20")', "")] },
  { name: "inventory.input_reversed_range_allowed", tests: [T.inv], edits: [e("lib/inventory/input.ts", "(v) => v.endsOn >= v.startsOn,", "() => true,")] },
  { name: "inventory.input_loose_day", tests: [T.inv], edits: [e("lib/inventory/input.ts", "(0[1-9]|[12]\\d|3[01])$/;", "(\\d{1,2})$/;")] },
  { name: "inventory.input_any_format", tests: [T.inv], edits: [e("lib/inventory/input.ts", "format: z.enum(PO_FORMATS),", "format: z.string(),")] },
  { name: "inventory.input_short_key_allowed", tests: [T.inv], edits: [e("lib/inventory/input.ts", '.min(8, "Enter your API key")', "")] },
  { name: "inventory.format_new_for_every_trend", tests: [T.inv], edits: [e("lib/inventory/format.ts", 'return label === "rising" ? "new" : "-";', 'return "new";')] },
  { name: "inventory.format_cover_rounded", tests: [T.inv], edits: [e("lib/inventory/format.ts", 'days === null ? "no sales" : days.toFixed(1)', 'days === null ? "no sales" : days.toFixed(0)')] },
  { name: "inventory.format_shortage_filter_open", tests: [T.inv], edits: [e("lib/inventory/format.ts", 'if (filter === "short") return line.shortage !== null;', 'if (filter === "short") return true;')] },
  { name: "inventory.format_reorder_filter_ignores_quantity", tests: [T.inv], edits: [e("lib/inventory/format.ts", "return line.needsReorder && line.suggestedQty > 0;", "return line.needsReorder;")] },
  { name: "inventory.view_revenue_in_minor_units", tests: [T.inv], edits: [e("lib/inventory/view.ts", 'revenue: displayAmount(l.revenueMinor, "minor_currency", l.currency),', "revenue: String(l.revenueMinor),")] },
  // ---- purchase orders on the page (lib/inventory/orders.ts, input.ts, format.ts)
  { name: "inventory_orders.viewer_may_change", tests: [T.invo], edits: [e("lib/inventory/orders.ts", 'if (principal.role === "viewer") return {', "if (false) return {")] },
  { name: "inventory_orders.shipped_may_be_cancelled", tests: [T.invo], edits: [e("lib/inventory/orders.ts", "  shipped: [],", '  shipped: ["cancelled"],')] },
  { name: "inventory_orders.draft_may_skip_to_confirmed", tests: [T.invo], edits: [e("lib/inventory/orders.ts", '  draft: ["ordered", "cancelled"],', '  draft: ["ordered", "confirmed", "cancelled"],')] },
  { name: "inventory_orders.partly_received_has_no_cancel", tests: [T.invo], edits: [e("lib/inventory/orders.ts", '  partly_received: ["cancelled"],', "  partly_received: [],")] },
  { name: "inventory_orders.late_on_the_day_itself", tests: [T.invo], edits: [e("lib/inventory/orders.ts", "o.expectedOn < today", "o.expectedOn <= today")] },
  { name: "inventory_orders.open_list_holds_drafts", tests: [T.invo], edits: [e("lib/inventory/orders.ts", 'const OPEN_FILTER: readonly PurchaseOrderStatus[] = ["ordered",', 'const OPEN_FILTER: readonly PurchaseOrderStatus[] = ["draft", "ordered",')] },
  { name: "inventory_orders.total_not_multiplied_by_quantity", tests: [T.invo], edits: [e("lib/inventory/orders.ts", 'BigInt(l.unit_cost_minor ?? "0") * BigInt(l.quantity_ordered)', 'BigInt(l.unit_cost_minor ?? "0")')] },
  { name: "inventory_orders.open_units_can_go_negative", tests: [T.invo], edits: [e("lib/inventory/orders.ts", "open: Math.max(l.quantity_ordered - l.quantity_received, 0),", "open: l.quantity_ordered - l.quantity_received,")] },
  { name: "inventory_orders.missing_order_is_a_conflict", tests: [T.invo], edits: [e("lib/inventory/orders.ts", 'if (code === "PO404") return { ok: false, code: "not_found", message };', 'if (code === "PO404") return { ok: false, code: "conflict", message };')] },
  { name: "inventory_orders.bad_quantity_is_a_conflict", tests: [T.invo], edits: [e("lib/inventory/orders.ts", 'if (code === "PO006") return { ok: false, code: "invalid_input", message };', 'if (code === "PO006") return { ok: false, code: "conflict", message };')] },
  { name: "inventory_orders.driver_message_passed_on", tests: [T.invo], edits: [e("lib/inventory/orders.ts", 'return { ok: false, code: "failed", message: "The request failed. Try again." };\n}\n\nconst DATE', 'return { ok: false, code: "failed", message };\n}\n\nconst DATE')] },
  { name: "inventory_orders.workbook_key_not_sent", tests: [T.invo], edits: [e("lib/inventory/orders.ts", "headers: { Authorization: `Bearer ${req.apiKey}` }", "headers: {}")] },
  { name: "inventory_orders.workbook_forbidden_is_failed", tests: [T.invo], edits: [e("lib/inventory/orders.ts", 'if (response.status === 403) return { ok: false, code: "forbidden",', 'if (response.status === 403) return { ok: false, code: "failed",')] },
  { name: "inventory_orders.workbook_no_plan_is_failed", tests: [T.invo], edits: [e("lib/inventory/orders.ts", 'if (response.status === 404) return { ok: false, code: "no_data",', 'if (response.status === 404) return { ok: false, code: "failed",')] },
  { name: "inventory_orders.workbook_double_slash", tests: [T.invo], edits: [e("lib/inventory/orders.ts", '.replace(/\\/+$/, "")', "")] },
  { name: "inventory_orders.workbook_promotion_dropped", tests: [T.invo], edits: [e("lib/inventory/orders.ts", 'if (req.promoFactor !== undefined) query.set("promo_factor", String(req.promoFactor));', "")] },
  { name: "inventory_orders.workbook_invalid_request_sent", tests: [T.invo], edits: [e("lib/inventory/orders.ts", 'if (!parsed.success) return { ok: false, code: "invalid_input", message: parsed.error.issues[0]?.message ?? "The request is not valid.", fieldErrors: fieldErrorsOf(parsed.error.issues) };\n  const req = parsed.data;\n  const query', 'const req = parsed.success ? parsed.data : { apiKey: "" };\n  const query')] },
  { name: "inventory_orders.input_a_person_may_move_to_received", tests: [T.invo], edits: [e("lib/inventory/input.ts", 'export const MANUAL_STATUSES = ["ordered", "confirmed", "shipped", "cancelled"] as const;', 'export const MANUAL_STATUSES = ["ordered", "confirmed", "shipped", "received", "cancelled"] as const;')] },
  { name: "inventory_orders.input_zero_receipt_allowed", tests: [T.invo], edits: [e("lib/inventory/input.ts", '.positive("Receive at least 1")', "")] },
  { name: "inventory_orders.input_fractional_receipt_allowed", tests: [T.invo], edits: [e("lib/inventory/input.ts", '.int("Use a whole number")', "")] },
  { name: "inventory_orders.input_over_receipt_accepted_by_default", tests: [T.invo], edits: [e("lib/inventory/input.ts", "allowOver: z.boolean().default(false),", "allowOver: z.boolean().default(true),")] },
  { name: "inventory_orders.input_empty_receipt_allowed", tests: [T.invo], edits: [e("lib/inventory/input.ts", '.min(1, "Name a line and a quantity")', "")] },
  { name: "inventory_orders.input_service_level_of_one_allowed", tests: [T.invo], edits: [e("lib/inventory/input.ts", '.lt(1, "Under 1")', "")] },
  { name: "inventory_orders.input_zero_workbook_factor_allowed", tests: [T.invo], edits: [e("lib/inventory/input.ts", '.gt(0, "The promotion factor must be above 0")', '.gte(0, "The promotion factor must be above 0")')] },
  { name: "inventory_orders.inbound_late_not_said", tests: [T.invo], edits: [e("lib/inventory/format.ts", "line.inboundLate ? `, was due ${line.inboundExpectedOn}`", "line.inboundLate ? `, due ${line.inboundExpectedOn}`")] },
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
