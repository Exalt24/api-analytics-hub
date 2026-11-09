/**
 * One export, start to finish, with no Next.js in it so the tests can call it directly:
 * key to tenant, tenant-scoped read, optional summary, one of three file formats.
 * The Server Actions in app/reports/actions.ts are thin wrappers over `runExport`.
 */
import type { LanguageModel } from "ai";
import { AuthError, resolvePrincipal } from "@/lib/server/auth";
import { withTenant, type Db } from "@/lib/server/db";
import { NoDataError, readMonthlySnapshot } from "@/lib/report/data";
import { buildCsv } from "@/lib/report/csv";
import { buildPdf } from "@/lib/report/pdf";
import { buildXlsx } from "@/lib/report/xlsx";
import { EXTENSION, MIME, exportRequestSchema, monthNotInFuture, type ExportRequest, type Format } from "@/lib/report/input";
import { generateSummary } from "@/lib/report/summary";
import type { ReportSummary } from "@/lib/report/summary-types";

export type ExportCode = "invalid_input" | "unauthorized" | "forbidden" | "no_data" | "failed";

export type ExportFile = {
  ok: true;
  filename: string;
  mimeType: string;
  bytes: Buffer;
  summary?: ReportSummary;
};

export type ExportFailure = {
  ok: false;
  code: ExportCode;
  message: string;
  fieldErrors?: Record<string, string[]>;
};

export type ExportDeps = { db: Db; model: LanguageModel | null; now?: () => Date };

export function reportFilename(month: string, format: Format): string {
  return `report-${month}.${EXTENSION[format]}`;
}

/** Validate with Zod, then run. Never throws for a bad request or a missing row; those come back as a failure value. */
export async function runExport(deps: ExportDeps, raw: unknown, onlyFormat?: Format): Promise<ExportFile | ExportFailure> {
  const parsed = exportRequestSchema.safeParse(raw);
  if (!parsed.success) {
    const fieldErrors: Record<string, string[]> = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path.join(".") || "request";
      (fieldErrors[key] ??= []).push(issue.message);
    }
    return { ok: false, code: "invalid_input", message: "The request is not valid.", fieldErrors };
  }
  const req: ExportRequest = parsed.data;
  if (onlyFormat !== undefined && req.format !== onlyFormat) {
    return { ok: false, code: "invalid_input", message: `This action exports ${onlyFormat} only.`, fieldErrors: { format: [`expected ${onlyFormat}`] } };
  }
  const now = (deps.now ?? (() => new Date()))();
  if (!monthNotInFuture(req.month, now)) {
    return { ok: false, code: "invalid_input", message: "That month has not started.", fieldErrors: { month: ["has not started"] } };
  }

  try {
    const principal = await resolvePrincipal(deps.db, req.apiKey);
    if (req.attachment && !req.attachment.objectKey.startsWith(`tenants/${principal.tenantId}/`)) {
      return { ok: false, code: "forbidden", message: "That document belongs to another tenant." };
    }
    const data = await withTenant(deps.db, principal.tenantId, (trx) => readMonthlySnapshot(trx, req.month));
    if (req.attachment) data.attachment = req.attachment;

    const summary = req.includeSummary ? (await generateSummary(data, deps.model)).summary : undefined;
    const bytes =
      req.format === "csv"
        ? Buffer.from(buildCsv(data, summary), "utf8")
        : req.format === "xlsx"
          ? await buildXlsx(data, summary)
          : await buildPdf(data, summary);
    return {
      ok: true,
      filename: reportFilename(req.month, req.format),
      mimeType: MIME[req.format],
      bytes,
      ...(summary ? { summary } : {}),
    };
  } catch (err) {
    if (err instanceof AuthError) {
      return { ok: false, code: err.status === 403 ? "forbidden" : "unauthorized", message: err.message };
    }
    if (err instanceof NoDataError) {
      return { ok: false, code: "no_data", message: `No stored data for ${req.month}. Run a sync or pick another month.` };
    }
    return { ok: false, code: "failed", message: "The export failed. Try again." };
  }
}
