/** The Reports reducer on its own: no React, no DOM. */
import { describe, expect, it } from "vitest";
import { initialReportsState, isFormat, lastMonth, reportsReducer, type ReportsState } from "@/lib/report/reports-state";

const start = (): ReportsState => initialReportsState(new Date("2026-11-09T12:00:00Z"));

describe("lastMonth and the initial state", () => {
  it("is the month before now, across a year boundary too", () => {
    expect(lastMonth(new Date("2026-11-09T12:00:00Z"))).toBe("2026-10");
    expect(lastMonth(new Date("2026-01-01T00:00:00Z"))).toBe("2025-12");
    expect(lastMonth(new Date("2026-03-31T23:59:59Z"))).toBe("2026-02");
  });

  it("starts on last month, CSV, no summary, no key, nothing exported", () => {
    expect(start()).toEqual({ tenantKey: null, month: "2026-10", format: "csv", includeSummary: false, exportStatus: "idle" });
  });
});

describe("format", () => {
  it("accepts csv, xlsx and pdf", () => {
    for (const f of ["csv", "xlsx", "pdf"]) {
      expect(reportsReducer(start(), { type: "format", format: f }).format).toBe(f);
      expect(isFormat(f)).toBe(true);
    }
  });

  it("refuses anything else and returns the very same state object", () => {
    const before = start();
    for (const bad of ["", "CSV", "docx", "csv ", "__proto__"]) {
      expect(isFormat(bad)).toBe(false);
      expect(reportsReducer(before, { type: "format", format: bad })).toBe(before);
    }
  });
});

describe("export outcomes", () => {
  it("started clears the old error and the old result, and turns the status to exporting", () => {
    const before: ReportsState = { ...start(), exportStatus: "failed", error: "old", lastExport: { filename: "old.csv" } };
    const after = reportsReducer(before, { type: "exportStarted" });
    expect(after.exportStatus).toBe("exporting");
    expect(after.error).toBeUndefined();
    expect(after.lastExport).toBeUndefined();
  });

  it("saved keeps the file name and the summary note", () => {
    const after = reportsReducer(start(), { type: "exportSaved", saved: { filename: "report-2026-10.pdf", summary: { source: "template", fallbackReason: "why" } } });
    expect(after.exportStatus).toBe("saved");
    expect(after.lastExport).toEqual({ filename: "report-2026-10.pdf", summary: { source: "template", fallbackReason: "why" } });
  });

  it("failed keeps the message and drops a previous result", () => {
    const saved = reportsReducer(start(), { type: "exportSaved", saved: { filename: "a.csv" } });
    const after = reportsReducer(saved, { type: "exportFailed", message: "No stored data for 2025-01" });
    expect(after.exportStatus).toBe("failed");
    expect(after.error).toBe("No stored data for 2025-01");
    expect(after.lastExport).toBeUndefined();
  });

  it("an upload error does not touch the status or the saved result", () => {
    const saved = reportsReducer(start(), { type: "exportSaved", saved: { filename: "a.csv" } });
    const after = reportsReducer(saved, { type: "error", message: "That file is over 10 MB." });
    expect(after.exportStatus).toBe("saved");
    expect(after.lastExport).toEqual({ filename: "a.csv" });
    expect(after.error).toBe("That file is over 10 MB.");
    expect(reportsReducer(after, { type: "error", message: undefined }).error).toBeUndefined();
  });
});

describe("the other fields", () => {
  it("sets and clears the attachment", () => {
    const att = { objectKey: "tenants/x/reports/2026-10/y-z.csv", filename: "z.csv" };
    const withIt = reportsReducer(start(), { type: "attachment", attachment: att });
    expect(withIt.attachment).toEqual(att);
    expect("attachment" in reportsReducer(withIt, { type: "attachment", attachment: undefined })).toBe(false);
  });

  it("keeps a half typed month, because the schema judges it at submit", () => {
    expect(reportsReducer(start(), { type: "month", month: "" }).month).toBe("");
  });

  it("holds the key and the summary switch", () => {
    expect(reportsReducer(start(), { type: "tenantKey", key: "aah_x" }).tenantKey).toBe("aah_x");
    expect(reportsReducer(start(), { type: "includeSummary", value: true }).includeSummary).toBe(true);
  });
});
