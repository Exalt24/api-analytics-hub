/**
 * The state the Reports page shares, as a plain reducer with no React in it, so the rules can be
 * tested on their own. `components/ReportsProvider.tsx` puts this reducer in a React Context.
 *
 * "Tenant" here is the API key: the dashboard has no tenant picker, the key resolves to the tenant
 * on the server (lib/server/auth.ts), so the key is what the visitor selects.
 */
import { FORMATS, type ExportForm, type Format } from "@/lib/report/input";

export type ExportStatus = "idle" | "exporting" | "saved" | "failed";
export type SavedExport = { filename: string; summary?: { source: "ai" | "template"; fallbackReason?: string } };
export type Attachment = NonNullable<ExportForm["attachment"]>;

export type ReportsState = {
  /** The API key that picks the tenant; null when none is stored. */
  tenantKey: string | null;
  /** YYYY-MM as typed; the Zod schema judges it at submit, so a half typed month is allowed here. */
  month: string;
  format: Format;
  includeSummary: boolean;
  attachment?: Attachment;
  exportStatus: ExportStatus;
  /** The last export that was saved; cleared when a new export starts. */
  lastExport?: SavedExport;
  error?: string;
};

export type ReportsAction =
  | { type: "tenantKey"; key: string | null }
  | { type: "month"; month: string }
  | { type: "format"; format: string }
  | { type: "includeSummary"; value: boolean }
  | { type: "attachment"; attachment: Attachment | undefined }
  | { type: "error"; message: string | undefined }
  | { type: "exportStarted" }
  | { type: "exportSaved"; saved: SavedExport }
  | { type: "exportFailed"; message: string };

export function isFormat(value: string): value is Format {
  return (FORMATS as readonly string[]).includes(value);
}

/** The month before this one, as YYYY-MM, in UTC (the server reads months the same way). */
export function lastMonth(now: Date = new Date()): string {
  const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

export function initialReportsState(now: Date = new Date()): ReportsState {
  return { tenantKey: null, month: lastMonth(now), format: "csv", includeSummary: false, exportStatus: "idle" };
}

export function reportsReducer(state: ReportsState, action: ReportsAction): ReportsState {
  switch (action.type) {
    case "tenantKey":
      return { ...state, tenantKey: action.key };
    case "month":
      return { ...state, month: action.month };
    case "format":
      // A format outside csv, xlsx and pdf is refused: the state stays as it was, same object.
      return isFormat(action.format) ? { ...state, format: action.format } : state;
    case "includeSummary":
      return { ...state, includeSummary: action.value };
    case "attachment": {
      const { attachment: _old, ...rest } = state;
      return action.attachment ? { ...rest, attachment: action.attachment } : rest;
    }
    case "error": {
      const { error: _old, ...rest } = state;
      return action.message === undefined ? rest : { ...rest, error: action.message };
    }
    case "exportStarted": {
      const { error: _e, lastExport: _l, ...rest } = state;
      return { ...rest, exportStatus: "exporting" };
    }
    case "exportSaved": {
      const { error: _e, ...rest } = state;
      return { ...rest, exportStatus: "saved", lastExport: action.saved };
    }
    case "exportFailed": {
      const { lastExport: _l, ...rest } = state;
      return { ...rest, exportStatus: "failed", error: action.message };
    }
  }
}
