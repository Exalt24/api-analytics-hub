"use client";

import { useEffect, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { createUploadUrlAction } from "@/app/reports/actions";
import { ExportResult } from "@/components/ExportResult";
import { useReports } from "@/components/ReportsProvider";
import { FORMATS, MAX_UPLOAD_BYTES, UPLOAD_TYPES, exportFormSchema, type ExportForm, type Format } from "@/lib/report/input";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

const FORMAT_LABELS: Record<Format, string> = { csv: "CSV", xlsx: "Excel (.xlsx)", pdf: "PDF" };

/**
 * The export form. What the visitor picks (month, format, summary switch, attachment) lives in the
 * Reports context, so the key bar and the result alerts read the same state. React Hook Form stays
 * as the validator: it holds a mirror of those four values, kept in step both ways, and the Zod
 * schema runs over it at submit.
 */
export function ReportForm() {
  const { state, busy, setMonth, setFormat, setIncludeSummary, setAttachment, setError, exportReport } = useReports();
  const form = useForm<ExportForm>({
    resolver: zodResolver(exportFormSchema),
    defaultValues: { format: state.format, month: state.month, includeSummary: state.includeSummary },
  });
  const { control, handleSubmit, setValue, getValues, formState } = form;
  const [uploading, setUploading] = useState(false);
  // The button stays off until the page is interactive, so an early click cannot submit the form as a plain page load.
  const [ready, setReady] = useState(false);
  useEffect(() => setReady(true), []);
  const attachment = state.attachment;

  // A change made through the context (not through these controls) reaches the validator too.
  useEffect(() => {
    const revalidate = { shouldValidate: formState.isSubmitted };
    if (getValues("format") !== state.format) setValue("format", state.format, revalidate);
    if (getValues("month") !== state.month) setValue("month", state.month, revalidate);
    if (getValues("includeSummary") !== state.includeSummary) setValue("includeSummary", state.includeSummary, revalidate);
    if (getValues("attachment")?.objectKey !== state.attachment?.objectKey) setValue("attachment", state.attachment, revalidate);
  }, [state.format, state.month, state.includeSummary, state.attachment, getValues, setValue, formState.isSubmitted]);

  async function onSubmit(): Promise<void> {
    await exportReport();
  }

  async function onFile(file: File | undefined): Promise<void> {
    setError(undefined);
    setAttachment(undefined);
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
    if (!state.tenantKey) {
      setError("Add your key above first.");
      return;
    }
    setUploading(true);
    try {
      const signed = await createUploadUrlAction({ apiKey: state.tenantKey, month: state.month, filename: file.name, contentType: type, size: file.size });
      if (!signed.ok) {
        setError(signed.message);
        return;
      }
      const put = await fetch(signed.url, { method: "PUT", headers: { "Content-Type": type }, body: file });
      if (!put.ok) {
        setError("The upload failed. Try again.");
        return;
      }
      setAttachment({ objectKey: signed.objectKey, filename: file.name });
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
                <Select
                  value={state.format}
                  onValueChange={(v) => {
                    if (setFormat(v)) field.onChange(v);
                  }}
                  disabled={busy}
                >
                  <SelectTrigger id="format" aria-label="Format">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {FORMATS.map((f) => (
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
            <Controller
              control={control}
              name="month"
              render={({ field }) => (
                <Input
                  id="month"
                  type="month"
                  disabled={busy}
                  aria-invalid={Boolean(formState.errors.month)}
                  name={field.name}
                  ref={field.ref}
                  onBlur={field.onBlur}
                  value={state.month}
                  onChange={(e) => {
                    field.onChange(e);
                    setMonth(e.target.value);
                  }}
                />
              )}
            />
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
                <Switch
                  id="includeSummary"
                  checked={state.includeSummary}
                  onCheckedChange={(v) => {
                    field.onChange(v);
                    setIncludeSummary(v);
                  }}
                  disabled={busy}
                />
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

          <ExportResult />

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
