"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, type ReactNode } from "react";
import type { ExportActionResult } from "@/lib/report/action-types";
import { saveFile } from "@/lib/report/download";
import type { Format } from "@/lib/report/input";
import { initialReportsState, isFormat, reportsReducer, type Attachment, type ReportsState } from "@/lib/report/reports-state";

export type ExportActions = Record<Format, (raw: unknown) => Promise<ExportActionResult>>;

export type ReportsContextValue = {
  state: ReportsState;
  /** True while an export request is in flight. */
  busy: boolean;
  setTenantKey: (key: string | null) => void;
  setMonth: (month: string) => void;
  /** Returns false, and changes nothing, for a format that is not csv, xlsx or pdf. */
  setFormat: (format: string) => boolean;
  setIncludeSummary: (value: boolean) => void;
  setAttachment: (attachment: Attachment | undefined) => void;
  setError: (message: string | undefined) => void;
  /** Runs the export for the state as it is now, saves the file, and records the outcome in `state`. */
  exportReport: () => Promise<void>;
};

const ReportsContext = createContext<ReportsContextValue | null>(null);

function storedKey(): string | null {
  try {
    return window.localStorage.getItem("apiKey") || null;
  } catch {
    return null;
  }
}

type ProviderProps = {
  children: ReactNode;
  /** One Server Action per format. Passed in, so a test can hand in a fake. */
  actions: ExportActions;
  /** What happens to the file the action returned; the browser download by default. */
  save?: (base64: string, filename: string, mimeType: string) => void;
  /** The clock that picks the default month (last month). */
  now?: Date;
};

/** Holds what the Reports page components share: the key, the month, the format, the summary switch, the attachment and the last export. */
export function ReportsProvider({ children, actions, save = saveFile, now }: ProviderProps) {
  const [state, dispatch] = useReducer(reportsReducer, now, initialReportsState);
  // The newest state for exportReport, which runs from an event handler after the last render.
  const latest = useRef(state);
  latest.current = state;

  // localStorage does not exist on the server, so the key is read after mount, and again when another tab changes it.
  useEffect(() => {
    dispatch({ type: "tenantKey", key: storedKey() });
    const onStorage = (e: StorageEvent) => {
      if (e.key === null || e.key === "apiKey") dispatch({ type: "tenantKey", key: storedKey() });
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const setFormat = useCallback((format: string) => {
    dispatch({ type: "format", format });
    return isFormat(format);
  }, []);

  const exportReport = useCallback(async () => {
    const s = latest.current;
    if (s.exportStatus === "exporting") return;
    if (!s.tenantKey) {
      dispatch({ type: "exportFailed", message: "Add your key above first." });
      return;
    }
    dispatch({ type: "exportStarted" });
    try {
      const result = await actions[s.format]({
        format: s.format,
        month: s.month,
        includeSummary: s.includeSummary,
        ...(s.attachment ? { attachment: s.attachment } : {}),
        apiKey: s.tenantKey,
      });
      if (!result.ok) {
        dispatch({ type: "exportFailed", message: result.message });
        return;
      }
      save(result.base64, result.filename, result.mimeType);
      dispatch({ type: "exportSaved", saved: { filename: result.filename, ...(result.summary ? { summary: result.summary } : {}) } });
    } catch {
      dispatch({ type: "exportFailed", message: "Could not reach the server. Try again." });
    }
  }, [actions, save]);

  const value = useMemo<ReportsContextValue>(
    () => ({
      state,
      busy: state.exportStatus === "exporting",
      setTenantKey: (key) => dispatch({ type: "tenantKey", key }),
      setMonth: (month) => dispatch({ type: "month", month }),
      setFormat,
      setIncludeSummary: (v) => dispatch({ type: "includeSummary", value: v }),
      setAttachment: (attachment) => dispatch({ type: "attachment", attachment }),
      setError: (message) => dispatch({ type: "error", message }),
      exportReport,
    }),
    [state, setFormat, exportReport],
  );

  return <ReportsContext.Provider value={value}>{children}</ReportsContext.Provider>;
}

/** The Reports page state and its setters. Throws outside a ReportsProvider, so a misplaced component fails loudly. */
export function useReports(): ReportsContextValue {
  const ctx = useContext(ReportsContext);
  if (ctx === null) throw new Error("useReports must be used inside a <ReportsProvider>.");
  return ctx;
}
