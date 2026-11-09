"use client";

import { exportCsvAction, exportPdfAction, exportXlsxAction } from "@/app/reports/actions";
import { KeyGate } from "@/components/KeyGate";
import { PresetsPanel } from "@/components/PresetsPanel";
import { ReportForm } from "@/components/ReportForm";
import { ReportsProvider, useReports, type ExportActions } from "@/components/ReportsProvider";

const ACTIONS: ExportActions = { csv: exportCsvAction, xlsx: exportXlsxAction, pdf: exportPdfAction };

/** The key bar writes the key into the Reports context; the form reads it from there at submit. */
function ReportsKeyGate() {
  const { setTenantKey } = useReports();
  return <KeyGate onSet={setTenantKey} />;
}

/** The key bar, the export form and the saved presets, sharing one ReportsProvider. */
export function ReportsPanel() {
  return (
    <ReportsProvider actions={ACTIONS}>
      <ReportsKeyGate />
      <ReportForm />
      <PresetsPanel />
    </ReportsProvider>
  );
}
