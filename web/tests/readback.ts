/** Read a produced file back the way a person's tools would: parse the CSV, open the workbook, extract the PDF text. */
import { parse } from "csv-parse/sync";
import ExcelJS from "exceljs";
import { extractText, getDocumentProxy } from "unpdf";

export function readCsv(bytes: Buffer | string): string[][] {
  const text = typeof bytes === "string" ? bytes : bytes.toString("utf8");
  return parse(text, { relax_column_count: false }) as string[][];
}

export type SheetRows = Record<string, Array<Array<string | number | null>>>;

/** Every sheet as rows of plain cell values (a number stays a number). */
export async function readXlsx(bytes: Buffer): Promise<{ sheets: SheetRows; formats: Record<string, string[]> }> {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(bytes as unknown as ArrayBuffer);
  const sheets: SheetRows = {};
  const formats: Record<string, string[]> = {};
  wb.eachSheet((ws) => {
    const rows: Array<Array<string | number | null>> = [];
    const fmts: string[] = [];
    ws.eachRow((row) => {
      const cells: Array<string | number | null> = [];
      row.eachCell({ includeEmpty: true }, (cell) => {
        const v = cell.value;
        cells.push(typeof v === "number" || typeof v === "string" ? v : v === null || v === undefined ? null : String(v));
        fmts.push(cell.numFmt ?? "");
      });
      rows.push(cells);
    });
    sheets[ws.name] = rows;
    formats[ws.name] = fmts;
  });
  return { sheets, formats };
}

export async function readPdf(bytes: Buffer): Promise<{ pages: number; text: string; lines: string[] }> {
  const pdf = await getDocumentProxy(new Uint8Array(bytes));
  const { totalPages, text } = await extractText(pdf, { mergePages: true });
  return { pages: totalPages, text, lines: text.split(/\r?\n/).map((l) => l.replace(/\s+/g, " ").trim()).filter(Boolean) };
}
