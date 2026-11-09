/** What a Server Action hands back to the form. A value, never a throw: a thrown message is hidden in production. */
import type { ExportCode } from "@/lib/report/export";

export type ExportActionResult =
  | {
      ok: true;
      filename: string;
      mimeType: string;
      /** The file, base64. A Server Action can return only serialisable values. */
      base64: string;
      summary?: { source: "ai" | "template"; fallbackReason?: string };
    }
  | { ok: false; code: ExportCode; message: string; fieldErrors?: Record<string, string[]> };

export type UploadActionResult =
  | { ok: true; url: string; objectKey: string; headers: Record<string, string>; expiresInSeconds: number }
  | {
      ok: false;
      code: "invalid_input" | "unauthorized" | "forbidden" | "not_configured" | "failed";
      message: string;
      fieldErrors?: Record<string, string[]>;
    };
