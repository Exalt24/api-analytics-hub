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

/** migrations/021: what a purchase order request may contain. The key names the tenant; no schema accepts a tenant id. */
export const ORDER_STATUSES = ["draft", "ordered", "confirmed", "shipped", "partly_received", "received", "cancelled"] as const;
export type OrderStatus = (typeof ORDER_STATUSES)[number];
/** The moves a person makes by hand. A move to partly received or received is made only by recording a receipt. */
export const MANUAL_STATUSES = ["ordered", "confirmed", "shipped", "cancelled"] as const;

const ORDER_ID = z.uuid("That is not an order id");

export const orderListSchema = z.object({ apiKey: KEY, status: z.enum(["open", ...ORDER_STATUSES]).optional() });

export const createOrderSchema = z.object({ apiKey: KEY, supplier: z.string().trim().min(1, "Name a supplier").max(120) });

export const moveOrderSchema = z.object({
  apiKey: KEY,
  orderId: ORDER_ID,
  to: z.enum(MANUAL_STATUSES),
  on: z.string().regex(DAY, "Use a date like 2026-12-01").optional(),
  expectedOn: z.string().regex(DAY, "Use a date like 2026-12-01").optional(),
});

export const expectedDateSchema = z.object({ apiKey: KEY, orderId: ORDER_ID, expectedOn: z.string().regex(DAY, "Use a date like 2026-12-01") });

export const receiptSchema = z.object({
  apiKey: KEY,
  orderId: ORDER_ID,
  lines: z
    .array(z.object({ lineId: z.number().int().positive(), quantity: z.number().int("Use a whole number").positive("Receive at least 1").max(1_000_000) }))
    .min(1, "Name a line and a quantity")
    .max(200),
  allowOver: z.boolean().default(false),
  on: z.string().regex(DAY, "Use a date like 2026-12-01").optional(),
});

/** The plan workbook: every field optional, because the backend defaults them from the stored plan and the promotions entered. */
export const workbookRequestSchema = z.object({
  apiKey: KEY,
  promoFactor: z.number().gt(0, "The promotion factor must be above 0").max(20, "The promotion factor can be at most 20").optional(),
  promoFrom: z.string().regex(DAY, "Use a date like 2026-12-01").optional(),
  promoTo: z.string().regex(DAY, "Use a date like 2026-12-07").optional(),
  serviceLevel: z.number().min(0.5, "At least 0.5").lt(1, "Under 1").optional(),
  reviewDays: z.number().int().min(0).max(365).optional(),
});
export type WorkbookRequest = z.infer<typeof workbookRequestSchema>;

export const WORKBOOK_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
