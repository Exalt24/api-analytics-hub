/**
 * The upload request, start to finish, with no Next.js in it: validate with Zod, turn
 * the API key into a tenant, sign a URL under that tenant's prefix. app/reports/actions.ts
 * wraps it as a Server Action.
 */
import type { S3Client } from "@aws-sdk/client-s3";
import { AuthError, resolvePrincipal } from "@/lib/server/auth";
import type { Db } from "@/lib/server/db";
import { monthNotInFuture, uploadRequestSchema } from "@/lib/report/input";
import { presignUpload, type PresignedUpload, type S3Config } from "@/lib/s3/presign";

export type UploadFailure = {
  ok: false;
  code: "invalid_input" | "unauthorized" | "forbidden" | "not_configured" | "failed";
  message: string;
  fieldErrors?: Record<string, string[]>;
};

export type UploadDeps = { db: Db; s3: { client: S3Client; config: S3Config } | null; now?: () => Date };

export async function createUpload(deps: UploadDeps, raw: unknown): Promise<({ ok: true } & PresignedUpload) | UploadFailure> {
  const parsed = uploadRequestSchema.safeParse(raw);
  if (!parsed.success) {
    const fieldErrors: Record<string, string[]> = {};
    for (const issue of parsed.error.issues) (fieldErrors[issue.path.join(".") || "request"] ??= []).push(issue.message);
    return { ok: false, code: "invalid_input", message: "The upload request is not valid.", fieldErrors };
  }
  const req = parsed.data;
  if (!monthNotInFuture(req.month, (deps.now ?? (() => new Date()))())) {
    return { ok: false, code: "invalid_input", message: "That month has not started.", fieldErrors: { month: ["has not started"] } };
  }
  if (!deps.s3) return { ok: false, code: "not_configured", message: "File upload is not set up." };
  try {
    const principal = await resolvePrincipal(deps.db, req.apiKey);
    const signed = await presignUpload(deps.s3.client, deps.s3.config, {
      tenantId: principal.tenantId,
      month: req.month,
      filename: req.filename,
      contentType: req.contentType,
      size: req.size,
    });
    return { ok: true, ...signed };
  } catch (err) {
    if (err instanceof AuthError) return { ok: false, code: err.status === 403 ? "forbidden" : "unauthorized", message: err.message };
    return { ok: false, code: "failed", message: "Could not prepare the upload. Try again." };
  }
}
