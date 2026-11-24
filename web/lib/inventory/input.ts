/**
 * What an Inventory request may contain. Every Server Action takes `unknown` (it can be POSTed to directly) and
 * these schemas decide what it is. The API key in the request names the tenant; no action accepts a tenant id.
 */
import { z } from "zod";

const KEY = z.string().min(8, "Enter your API key").max(200); // COPY-OK: the dashboard already calls it an API key

export const DAY = /^20\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$/;

export const planRequestSchema = z.object({ apiKey: KEY });
export type PlanRequest = z.infer<typeof planRequestSchema>;

export const PO_FORMATS = ["csv", "xlsx", "sheets"] as const;
export type PoFormat = (typeof PO_FORMATS)[number];

export const poRequestSchema = z.object({
  apiKey: KEY,
  format: z.enum(PO_FORMATS),
  supplier: z.string().min(1).max(120).optional(),
});
export type PoRequest = z.infer<typeof poRequestSchema>;

export const PO_EXTENSION: Record<PoFormat, string> = { csv: "csv", xlsx: "xlsx", sheets: "tsv" };
export const PO_MIME: Record<PoFormat, string> = {
  csv: "text/csv; charset=utf-8",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  sheets: "text/tab-separated-values; charset=utf-8",
};

/** A dated promotion factor entered by a person. 1.5 is half as much again as a normal day; the database holds the same bounds. */
export const promoFormSchema = z
  .object({
    sku: z.string().trim().max(80).optional(),
    startsOn: z.string().regex(DAY, "Use a date like 2026-12-01"),
    endsOn: z.string().regex(DAY, "Use a date like 2026-12-07"),
    factor: z.number().gt(0, "The factor must be above 0").max(20, "The factor can be at most 20"),
    note: z.string().trim().max(200).optional(),
  })
  .refine((v) => v.endsOn >= v.startsOn, { message: "The promotion cannot end before it starts", path: ["endsOn"] });
export type PromoForm = z.infer<typeof promoFormSchema>;

export const promoRequestSchema = promoFormSchema.safeExtend({ apiKey: KEY });
export type PromoRequest = z.infer<typeof promoRequestSchema>;
