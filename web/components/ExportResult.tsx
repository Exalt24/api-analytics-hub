"use client";

import { useReports } from "@/components/ReportsProvider";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

/** The error and the "Saved" alert, read from the Reports context, so any component can cause them. */
export function ExportResult() {
  const { state } = useReports();
  const { error, lastExport } = state;
  return (
    <>
      {error ? (
        <Alert variant="destructive">
          <AlertTitle>Could not export</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      {lastExport ? (
        <Alert>
          <AlertTitle>Saved {lastExport.filename}</AlertTitle>
          {lastExport.summary ? (
            <AlertDescription>
              {lastExport.summary.source === "ai"
                ? "Summary written by the model. Every figure matched the data."
                : `Summary from a plain template. ${lastExport.summary.fallbackReason ?? ""}`}
            </AlertDescription>
          ) : undefined}
        </Alert>
      ) : null}
    </>
  );
}
