"use client";

import { useEffect, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { exportCsvAction, exportPdfAction, exportXlsxAction, createUploadUrlAction } from "@/app/reports/actions";
import type { ExportActionResult } from "@/lib/report/action-types";
import { MAX_UPLOAD_BYTES, UPLOAD_TYPES, exportFormSchema, type ExportForm, type Format } from "@/lib/report/input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

const ACTIONS: Record<Format, (raw: unknown) => Promise<ExportActionResult>> = {
  csv: exportCsvAction,
  xlsx: exportXlsxAction,
  pdf: exportPdfAction,
};

const FORMAT_LABELS: Record<Format, string> = { csv: "CSV", xlsx: "Excel (.xlsx)", pdf: "PDF" };

/** The month before this one, as YYYY-MM, in UTC (the server reads months the same way). */
function lastMonth(now: Date = new Date()): string {
  const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

function storedKey(): string {
  try {
    return window.localStorage.getItem("apiKey") ?? "";
  } catch {
    return "";
  }
}

function saveFile(base64: string, filename: string, mimeType: string): void {
  const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([bytes], { type: mimeType }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

type Done = { filename: string; summary?: { source: "ai" | "template"; fallbackReason?: string } };

export function ReportForm() {
  const form = useForm<ExportForm>({
    resolver: zodResolver(exportFormSchema),
    defaultValues: { format: "csv", month: lastMonth(), includeSummary: false },
  });
  const { control, register, handleSubmit, setValue, watch, formState } = form;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>(undefined);
  const [done, setDone] = useState<Done | undefined>(undefined);
  const [uploading, setUploading] = useState(false);
  // The button stays off until the page is interactive, so an early click cannot submit the form as a plain page load.
  const [ready, setReady] = useState(false);
  useEffect(() => setReady(true), []);
  const attachment = watch("attachment");

  async function onSubmit(values: ExportForm): Promise<void> {
    setError(undefined);
    setDone(undefined);
    const apiKey = storedKey();
    if (!apiKey) {
      setError("Add your key above first.");
      return;
    }
    setBusy(true);
    try {
      const result = await ACTIONS[values.format]({ ...values, apiKey });
      if (!result.ok) {
        setError(result.message);
        return;
      }
      saveFile(result.base64, result.filename, result.mimeType);
      setDone({ filename: result.filename, ...(result.summary ? { summary: result.summary } : {}) });
    } catch {
      setError("Could not reach the server. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function onFile(file: File | undefined): Promise<void> {
    setError(undefined);
    setValue("attachment", undefined);
    if (!file) return;
    const type = UPLOAD_TYPES.find((t) => t === file.type);
    if (!type) {
      setError("That file type is not allowed.");
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setError("That file is over 10 MB.");
      return;
    }
    const apiKey = storedKey();
    if (!apiKey) {
      setError("Add your key above first.");
      return;
    }
    setUploading(true);
    try {
      const signed = await createUploadUrlAction({ apiKey, month: watch("month"), filename: file.name, contentType: type, size: file.size });
      if (!signed.ok) {
        setError(signed.message);
        return;
      }
      const put = await fetch(signed.url, { method: "PUT", headers: { "Content-Type": type }, body: file });
      if (!put.ok) {
        setError("The upload failed. Try again.");
        return;
      }
      setValue("attachment", { objectKey: signed.objectKey, filename: file.name });
    } catch {
      setError("The upload failed. Try again.");
    } finally {
      setUploading(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Export a report</CardTitle>
        <CardDescription>Pick a format and a month. The file downloads when it is ready.</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit(onSubmit)} className="flex flex-col gap-5" noValidate aria-busy={busy}>
          <div className="flex flex-col gap-2">
            <Label htmlFor="format">Format</Label>
            <Controller
              control={control}
              name="format"
              render={({ field }) => (
                <Select value={field.value} onValueChange={field.onChange} disabled={busy}>
                  <SelectTrigger id="format" aria-label="Format">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {(Object.keys(FORMAT_LABELS) as Format[]).map((f) => (
                      <SelectItem key={f} value={f}>
                        {FORMAT_LABELS[f]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            />
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="month">Month</Label>
            <Input id="month" type="month" disabled={busy} aria-invalid={Boolean(formState.errors.month)} {...register("month")} />
            {formState.errors.month ? (
              <p className="text-destructive text-sm" role="alert">
                {formState.errors.month.message}
              </p>
            ) : null}
          </div>

          <div className="flex items-start gap-3">
            <Controller
              control={control}
              name="includeSummary"
              render={({ field }) => (
                <Switch id="includeSummary" checked={field.value} onCheckedChange={field.onChange} disabled={busy} />
              )}
            />
            <div className="flex flex-col gap-1">
              {/* COPY-OK: the control is named for the AI summary */}
              <Label htmlFor="includeSummary">Include AI summary</Label>
              <p className="text-muted-foreground text-sm">A model writes it. Every figure is checked against the data.</p>
            </div>
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="source">Source document</Label>
            <Input
              id="source"
              type="file"
              accept={UPLOAD_TYPES.join(",")}
              disabled={busy || uploading}
              onChange={(e) => void onFile(e.target.files?.[0])}
            />
            <p className="text-muted-foreground text-sm" aria-live="polite">
              {uploading ? "Uploading." : attachment ? `Attached ${attachment.filename}.` : "Optional. Up to 10 MB."}
            </p>
          </div>

          {error ? (
            <Alert variant="destructive">
              <AlertTitle>Could not export</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : null}

          {done ? (
            <Alert>
              <AlertTitle>Saved {done.filename}</AlertTitle>
              {done.summary ? (
                <AlertDescription>
                  {done.summary.source === "ai"
                    ? "Summary written by the model. Every figure matched the data."
                    : `Summary from a plain template. ${done.summary.fallbackReason ?? ""}`}
                </AlertDescription>
              ) : undefined}
            </Alert>
          ) : null}

          <div>
            <Button type="submit" disabled={!ready || busy || uploading}>
              {busy ? "Exporting" : "Export report"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
