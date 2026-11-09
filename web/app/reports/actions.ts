"use server";

/**
 * The Server Actions behind the Reports page. Each one takes `unknown`, because a Server
 * Action can be POSTed to directly and its argument is whatever the caller sent; Zod
 * (lib/report/input.ts) decides what it is. The key in the request names the tenant, the
 * action never accepts a tenant id.
 */
import { runExport } from "@/lib/report/export";
import type { ExportActionResult, UploadActionResult } from "@/lib/report/action-types";
import type { Format } from "@/lib/report/input";
import { modelFromEnv } from "@/lib/report/summary";
import { makeS3Client, s3ConfigFromEnv } from "@/lib/s3/presign";
import { createUpload } from "@/lib/s3/upload";
import { getDb } from "@/lib/server/db";

async function exportAs(raw: unknown, format: Format): Promise<ExportActionResult> {
  let db;
  try {
    db = await getDb();
  } catch {
    return { ok: false, code: "failed", message: "Reports are not set up on this server." };
  }
  const result = await runExport({ db, model: modelFromEnv() }, raw, format);
  if (!result.ok) return result;
  return {
    ok: true,
    filename: result.filename,
    mimeType: result.mimeType,
    base64: result.bytes.toString("base64"),
    ...(result.summary
      ? { summary: { source: result.summary.source, ...(result.summary.fallbackReason ? { fallbackReason: result.summary.fallbackReason } : {}) } }
      : {}),
  };
}

export async function exportCsvAction(raw: unknown): Promise<ExportActionResult> {
  return exportAs(raw, "csv");
}

export async function exportXlsxAction(raw: unknown): Promise<ExportActionResult> {
  return exportAs(raw, "xlsx");
}

export async function exportPdfAction(raw: unknown): Promise<ExportActionResult> {
  return exportAs(raw, "pdf");
}

export async function createUploadUrlAction(raw: unknown): Promise<UploadActionResult> {
  let db;
  try {
    db = await getDb();
  } catch {
    return { ok: false, code: "failed", message: "Reports are not set up on this server." };
  }
  const config = s3ConfigFromEnv();
  const result = await createUpload({ db, s3: config ? { client: makeS3Client(config), config } : null }, raw);
  return result;
}
