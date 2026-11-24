/**
 * A stand-in for the backend's plan workbook route (GET /api/inventory/workbook, app/api/inventory.py), for the browser test of the download button.
 * It is a script, not the backend: it checks that a bearer key arrived, records the request, and answers with a small real .xlsx whose Plan sheet holds one formula.
 * The real workbook, its formulas and their recalculation are tested in Python (tests/test_inventory_workbook.py); this proves the page asks for it, passes the promotion
 * settings on, and hands the file to the browser.
 *
 * GET /__log lists the requests seen. Run: node e2e/stand-ins/workbook-backend.mts (PORT, default 4011). Needs Node 22.18 or newer.
 */
import http from "node:http";
import ExcelJS from "exceljs";

type Seen = { url: string; authorization: string | null };
const seen: Seen[] = [];

async function workbook(): Promise<Buffer> {
  const wb = new ExcelJS.Workbook();
  const plan = wb.addWorksheet("Plan");
  plan.addRow(["SKU", "Daily velocity", "Doubled"]);
  plan.addRow(["TEE-1", 2, { formula: "B2*2", result: 4 }]);
  return Buffer.from(await wb.xlsx.writeBuffer());
}

const server = http.createServer(async (req, res) => {
  const url = req.url ?? "";
  if (url === "/__health") {
    res.writeHead(200).end("ok");
    return;
  }
  if (url === "/__log") {
    res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify(seen));
    return;
  }
  if (url.startsWith("/api/inventory/workbook")) {
    const authorization = req.headers.authorization ?? null;
    seen.push({ url, authorization });
    if (!authorization?.startsWith("Bearer aah_test_")) {
      res.writeHead(401).end("no key");
      return;
    }
    const bytes = await workbook();
    res.writeHead(200, {
      "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "content-disposition": 'attachment; filename="inventory-plan-2026-11-24.xlsx"',
    });
    res.end(bytes);
    return;
  }
  res.writeHead(404).end("not found");
});

server.listen(Number(process.env.PORT ?? 4011), "127.0.0.1");
