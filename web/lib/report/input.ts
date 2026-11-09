/**
 * What an export request may contain. The same schemas run in the browser (the form,
 * through react-hook-form) and on the server (every Server Action), so a request that
 * skips the form and posts straight to the action is held to the same rules.
 */
import { z } from "zod";

export const FORMATS = ["csv", "xlsx", "pdf"] as const;
export type Format = (typeof FORMATS)[number];

export const MONTH = /^(20\d{2})-(0[1-9]|1[0-2])$/;

/** `tenants/<tenant uuid>/reports/<YYYY-MM>/<uuid>-<file name>`, the key s3/presign.ts hands out. */
export const OBJECT_KEY = /^tenants\/[0-9a-f-]{36}\/reports\/20\d{2}-(0[1-9]|1[0-2])\/[0-9a-f-]{36}-[A-Za-z0-9._-]{1,100}$/;

export const attachmentSchema = z.object({
  objectKey: z.string().regex(OBJECT_KEY, "Not a report document"),
  filename: z.string().min(1).max(120),
});

export const exportFormSchema = z.object({
  format: z.enum(FORMATS),
  month: z.string().regex(MONTH, "Use a month like 2026-10"),
  includeSummary: z.boolean(),
  attachment: attachmentSchema.optional(),
});
export type ExportForm = z.infer<typeof exportFormSchema>;

export const exportRequestSchema = exportFormSchema.extend({
  // COPY-OK: the dashboard already calls it an API key
  apiKey: z.string().min(8, "Enter your API key").max(200),
});
export type ExportRequest = z.infer<typeof exportRequestSchema>;

/** The month a person is allowed to ask for: no month that has not started. */
export function monthNotInFuture(month: string, now: Date): boolean {
  const current = `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, "0")}`;
  return month <= current;
}

export const MIME: Record<Format, string> = {
  csv: "text/csv; charset=utf-8",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  pdf: "application/pdf",
};

export const EXTENSION: Record<Format, string> = { csv: "csv", xlsx: "xlsx", pdf: "pdf" };

export const UPLOAD_TYPES = [
  "application/pdf",
  "text/csv",
  "image/png",
  "image/jpeg",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
] as const;
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

export const uploadRequestSchema = z.object({
  apiKey: z.string().min(8).max(200),
  month: z.string().regex(MONTH),
  filename: z.string().min(1).max(120),
  contentType: z.enum(UPLOAD_TYPES),
  size: z.number().int().min(1).max(MAX_UPLOAD_BYTES),
});
export type UploadRequest = z.infer<typeof uploadRequestSchema>;
