"use client";

import { KeyGate } from "@/components/KeyGate";
import { ReportForm } from "@/components/ReportForm";

/** The key bar and the export form. The form reads the key at submit, so the bar needs no callback. */
export function ReportsPanel() {
  return (
    <>
      <KeyGate onSet={() => undefined} />
      <ReportForm />
    </>
  );
}
