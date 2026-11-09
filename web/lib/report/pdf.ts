import { PDFDocument, StandardFonts, rgb, type PDFFont, type PDFPage } from "pdf-lib";
import type { ReportData } from "@/lib/report/data";
import { displayAmount, monthName } from "@/lib/report/format";
import type { ReportSummary } from "@/lib/report/summary-types";

const PAGE = { width: 595, height: 842 };
const MARGIN = 48;
const BODY = 10;
const LEADING = 14;

/** The standard PDF fonts encode Latin-1 only. A tenant name with other characters would throw, so they become `?`. */
export function pdfSafe(text: string): string {
  return text.replace(/[^\x20-\x7E -ÿ]/g, "?");
}

function wrap(text: string, font: PDFFont, size: number, maxWidth: number): string[] {
  const out: string[] = [];
  let current = "";
  for (const word of text.split(/\s+/).filter(Boolean)) {
    const next = current ? `${current} ${word}` : word;
    if (font.widthOfTextAtSize(next, size) > maxWidth && current) {
      out.push(current);
      current = word;
    } else {
      current = next;
    }
  }
  if (current) out.push(current);
  return out;
}

export async function buildPdf(data: ReportData, summary?: ReportSummary): Promise<Buffer> {
  const doc = await PDFDocument.create();
  doc.setTitle(pdfSafe(`${data.tenantName} report ${data.month}`));
  doc.setProducer("api-analytics-hub");
  const text = await doc.embedFont(StandardFonts.Helvetica);
  const bold = await doc.embedFont(StandardFonts.HelveticaBold);
  const mono = await doc.embedFont(StandardFonts.Courier);

  let page: PDFPage = doc.addPage([PAGE.width, PAGE.height]);
  let y = PAGE.height - MARGIN;

  const ensure = (needed: number): void => {
    if (y - needed < MARGIN) {
      page = doc.addPage([PAGE.width, PAGE.height]);
      y = PAGE.height - MARGIN;
    }
  };
  const draw = (s: string, font: PDFFont, size = BODY): void => {
    ensure(LEADING);
    page.drawText(pdfSafe(s), { x: MARGIN, y, size, font, color: rgb(0.08, 0.09, 0.1) });
    y -= LEADING;
  };
  const paragraph = (s: string, font: PDFFont = text): void => {
    for (const l of wrap(pdfSafe(s), font, BODY, PAGE.width - 2 * MARGIN)) draw(l, font);
  };
  const heading = (s: string): void => {
    y -= 8;
    ensure(LEADING + 8);
    draw(s, bold, 12);
  };

  ensure(30);
  page.drawText(pdfSafe(`${data.tenantName}`), { x: MARGIN, y, size: 18, font: bold });
  y -= 24;
  draw(`Monthly report for ${monthName(data.month)} (${data.month})`, text, 11);

  heading("Summary");
  if (summary) {
    draw(summary.source === "ai" ? "Source: AI-written, every figure checked against the data" : "Source: plain template", text, 9);
    if (summary.source === "template" && summary.fallbackReason) {
      paragraph(`AI summary not used: ${summary.fallbackReason}`);
    }
    draw(summary.headline, bold);
    for (const h of summary.highlights) paragraph(`- ${h}`);
  } else {
    draw("No summary requested.", text);
  }

  heading("Totals");
  const totalLines = data.totals.map((t) => {
    const value = displayAmount(t.total, t.unit, t.currency);
    return `${t.label.padEnd(26)} ${value.padStart(16)}  ${t.daysWithData} days`;
  });
  for (const l of totalLines) draw(l, mono);

  heading("Daily rows");
  for (const r of data.daily) {
    const value = displayAmount(r.value, r.unit, r.currency);
    draw(`${r.date}  ${r.label.padEnd(26)} ${value.padStart(16)}`, mono);
  }

  if (data.attachment) {
    heading("Source document");
    draw(data.attachment.filename, text);
  }

  const bytes = await doc.save();
  return Buffer.from(bytes);
}
