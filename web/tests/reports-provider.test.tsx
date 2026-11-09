// @vitest-environment jsdom
/**
 * The ReportsProvider and useReports(): the shared state reaches sibling components, the hook fails
 * outside the provider, a bad format is refused, and the last export updates after an export action
 * (a fake one; no server and no database are involved here).
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ExportResult } from "@/components/ExportResult";
import { ReportForm } from "@/components/ReportForm";
import { ReportsPanel } from "@/components/ReportsPanel";
import { ReportsProvider, useReports, type ExportActions } from "@/components/ReportsProvider";
import type { ExportActionResult } from "@/lib/report/action-types";

const actionMocks = vi.hoisted(() => {
  const saved = { ok: true as const, filename: "report-2026-10.csv", mimeType: "text/csv", base64: "YQ==" };
  return { exportCsvAction: vi.fn(async (_raw: unknown) => saved), exportXlsxAction: vi.fn(), exportPdfAction: vi.fn(), createUploadUrlAction: vi.fn() };
});
vi.mock("@/app/reports/actions", () => actionMocks);

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
// jsdom has no ResizeObserver, which the Radix Select and Switch read on mount.
vi.stubGlobal(
  "ResizeObserver",
  class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  },
);

const NOW = new Date("2026-11-09T12:00:00Z");
const OK: ExportActionResult = { ok: true, filename: "report-2026-10.csv", mimeType: "text/csv", base64: "YQ==" };

let lastSetFormat: boolean | undefined;
let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  window.localStorage.clear();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function fakeActions(result: ExportActionResult | Error = OK) {
  const call = vi.fn(async (_raw: unknown): Promise<ExportActionResult> => {
    if (result instanceof Error) throw result;
    return result;
  });
  const actions: ExportActions = { csv: call, xlsx: call, pdf: call };
  return { call, actions };
}

function mount(node: React.ReactNode): void {
  act(() => root.render(node));
}

/** Two siblings under one provider: one writes, one only reads. */
function Writer() {
  const r = useReports();
  return (
    <div>
      <button data-testid="month-btn" onClick={() => r.setMonth("2026-03")} />
      <button data-testid="pdf-btn" onClick={() => r.setFormat("pdf")} />
      <button data-testid="bad-format" onClick={() => (lastSetFormat = r.setFormat("docx"))} />
      <button data-testid="summary-btn" onClick={() => r.setIncludeSummary(true)} />
      <button data-testid="key-btn" onClick={() => r.setTenantKey("aah_test_key_1")} />
      <button data-testid="export-btn" onClick={() => void r.exportReport()} />
    </div>
  );
}

function Reader() {
  const { state, busy } = useReports();
  return (
    <p data-testid="reader">
      {[state.month, state.format, String(state.includeSummary), state.tenantKey ?? "no-key", state.exportStatus, state.lastExport?.filename ?? "-", state.error ?? "-", String(busy)].join("|")}
    </p>
  );
}

const click = (id: string) => act(() => void (host.querySelector<HTMLElement>(`[data-testid="${id}"]`)?.click()));
const read = () => host.querySelector('[data-testid="reader"]')?.textContent;

describe("ReportsProvider, two siblings", () => {
  it("starts with last month, csv, no summary and no key", () => {
    mount(
      <ReportsProvider actions={fakeActions().actions} now={NOW}>
        <Writer />
        <Reader />
      </ReportsProvider>,
    );
    expect(read()).toBe("2026-10|csv|false|no-key|idle|-|-|false");
  });

  it("a change made by one sibling shows in the other: month, format, summary switch and key", () => {
    mount(
      <ReportsProvider actions={fakeActions().actions} now={NOW}>
        <Writer />
        <Reader />
      </ReportsProvider>,
    );
    click("month-btn");
    expect(read()).toBe("2026-03|csv|false|no-key|idle|-|-|false");
    click("pdf-btn");
    click("summary-btn");
    click("key-btn");
    expect(read()).toBe("2026-03|pdf|true|aah_test_key_1|idle|-|-|false");
  });

  it("reads the stored key after mount, and again when another tab changes it", () => {
    window.localStorage.setItem("apiKey", "aah_from_storage_1");
    mount(
      <ReportsProvider actions={fakeActions().actions} now={NOW}>
        <Reader />
      </ReportsProvider>,
    );
    expect(read()).toContain("|aah_from_storage_1|");
    act(() => {
      window.localStorage.removeItem("apiKey");
      window.dispatchEvent(new StorageEvent("storage", { key: "apiKey" }));
    });
    expect(read()).toContain("|no-key|");
  });
});

describe("useReports", () => {
  it("throws a clear error outside a ReportsProvider", () => {
    expect(() => renderToString(<Reader />)).toThrow("useReports must be used inside a <ReportsProvider>.");
  });

  it("refuses a format that is not csv, xlsx or pdf: the setter says false and the state stays", () => {
    mount(
      <ReportsProvider actions={fakeActions().actions} now={NOW}>
        <Writer />
        <Reader />
      </ReportsProvider>,
    );
    click("pdf-btn");
    click("bad-format");
    expect(lastSetFormat).toBe(false);
    expect(read()).toContain("|pdf|");
  });
});

describe("exportReport with a fake export action", () => {
  function setup(result: ExportActionResult | Error = OK) {
    const fake = fakeActions(result);
    const save = vi.fn();
    mount(
      <ReportsProvider actions={fake.actions} save={save} now={NOW}>
        <Writer />
        <Reader />
        <ExportResult />
      </ReportsProvider>,
    );
    return { ...fake, save };
  }

  async function runExport(): Promise<void> {
    await act(async () => {
      host.querySelector<HTMLElement>('[data-testid="export-btn"]')?.click();
    });
  }

  it("success: the action gets the state and the key, the file is saved, the last export is recorded and shown", async () => {
    const { call, save } = setup({ ...OK, filename: "report-2026-03.pdf", mimeType: "application/pdf", summary: { source: "ai" } });
    click("key-btn");
    click("month-btn");
    click("pdf-btn");
    click("summary-btn");
    await runExport();
    expect(call).toHaveBeenCalledTimes(1);
    expect(call).toHaveBeenCalledWith({ format: "pdf", month: "2026-03", includeSummary: true, apiKey: "aah_test_key_1" });
    expect(save).toHaveBeenCalledWith("YQ==", "report-2026-03.pdf", "application/pdf");
    expect(read()).toBe("2026-03|pdf|true|aah_test_key_1|saved|report-2026-03.pdf|-|false");
    expect(host.textContent).toContain("Saved report-2026-03.pdf");
    expect(host.textContent).toContain("Summary written by the model. Every figure matched the data.");
  });

  it("the export button path picks the action of the chosen format", async () => {
    const csv = vi.fn(async (): Promise<ExportActionResult> => OK);
    const pdf = vi.fn(async (): Promise<ExportActionResult> => ({ ...OK, filename: "r.pdf" }));
    mount(
      <ReportsProvider actions={{ csv, xlsx: csv, pdf }} save={vi.fn()} now={NOW}>
        <Writer />
        <Reader />
      </ReportsProvider>,
    );
    click("key-btn");
    click("pdf-btn");
    await runExport();
    expect(pdf).toHaveBeenCalledTimes(1);
    expect(csv).not.toHaveBeenCalled();
  });

  it("a refused export records the message and no file", async () => {
    const { save } = setup({ ok: false, code: "no_data", message: "No stored data for 2026-10" } as ExportActionResult);
    click("key-btn");
    await runExport();
    expect(save).not.toHaveBeenCalled();
    expect(read()).toBe("2026-10|csv|false|aah_test_key_1|failed|-|No stored data for 2026-10|false");
    expect(host.textContent).toContain("Could not export");
  });

  it("a thrown action says the server could not be reached", async () => {
    setup(new Error("network down"));
    click("key-btn");
    await runExport();
    expect(read()).toContain("|failed|-|Could not reach the server. Try again.|");
  });

  it("with no key it asks for one and never calls the action", async () => {
    const { call } = setup();
    await runExport();
    expect(call).not.toHaveBeenCalled();
    expect(read()).toContain("|failed|-|Add your key above first.|");
  });

  it("a second export clears the first one's result while it runs", async () => {
    let release: (r: ExportActionResult) => void = () => undefined;
    const slow = vi.fn(() => new Promise<ExportActionResult>((resolve) => (release = resolve)));
    mount(
      <ReportsProvider actions={{ csv: slow, xlsx: slow, pdf: slow }} save={vi.fn()} now={NOW}>
        <Writer />
        <Reader />
      </ReportsProvider>,
    );
    click("key-btn");
    await act(async () => {
      host.querySelector<HTMLElement>('[data-testid="export-btn"]')?.click();
    });
    expect(read()).toContain("|exporting|");
    expect(read()?.endsWith("|true")).toBe(true);
    // a click while one is in flight does not start another
    await act(async () => {
      host.querySelector<HTMLElement>('[data-testid="export-btn"]')?.click();
    });
    expect(slow).toHaveBeenCalledTimes(1);
    await act(async () => release(OK));
    expect(read()).toContain("|saved|report-2026-10.csv|");
  });
});

describe("the real ReportForm on the context", () => {
  function setValue(el: HTMLInputElement, value: string): void {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  async function mountForm(fake = fakeActions()) {
    window.localStorage.setItem("apiKey", "aah_stored_key_1");
    mount(
      <ReportsProvider actions={fake.actions} save={vi.fn()} now={NOW}>
        <ReportForm />
        <Reader />
      </ReportsProvider>,
    );
    await act(async () => undefined);
    return fake;
  }

  const submit = async () => {
    await act(async () => {
      host.querySelector<HTMLButtonElement>('button[type="submit"]')?.click();
    });
  };

  it("typing a month writes it to the context, and the submit sends it", async () => {
    const { call } = await mountForm();
    const month = host.querySelector<HTMLInputElement>("#month");
    expect(month?.value).toBe("2026-10");
    await act(async () => setValue(month as HTMLInputElement, "2026-03"));
    expect(read()).toContain("2026-03|csv|");
    await submit();
    expect(call).toHaveBeenCalledWith({ format: "csv", month: "2026-03", includeSummary: false, apiKey: "aah_stored_key_1" });
    expect(host.textContent).toContain("Saved report-2026-10.csv");
  });

  it("an empty month is stopped by the Zod schema: the message shows and no action runs", async () => {
    const { call } = await mountForm();
    await act(async () => setValue(host.querySelector<HTMLInputElement>("#month") as HTMLInputElement, ""));
    await submit();
    expect(call).not.toHaveBeenCalled();
    expect(host.textContent).toContain("Use a month like 2026-10");
  });

  it("the summary switch writes to the context and the submit sends it", async () => {
    const { call } = await mountForm();
    await act(async () => host.querySelector<HTMLElement>("#includeSummary")?.click());
    expect(read()).toContain("|csv|true|");
    await submit();
    expect(call).toHaveBeenCalledWith(expect.objectContaining({ includeSummary: true }));
  });

  it("the form is disabled while the export runs, and the sibling alert shows the result after", async () => {
    let release: (r: ExportActionResult) => void = () => undefined;
    const slow = vi.fn(() => new Promise<ExportActionResult>((resolve) => (release = resolve)));
    await mountForm({ call: slow, actions: { csv: slow, xlsx: slow, pdf: slow } });
    await submit();
    expect(host.querySelector("#month")?.hasAttribute("disabled")).toBe(true);
    expect(host.querySelector("button[type=submit]")?.textContent).toBe("Exporting");
    await act(async () => release(OK));
    expect(host.querySelector("#month")?.hasAttribute("disabled")).toBe(false);
    expect(host.textContent).toContain("Saved report-2026-10.csv");
  });
});

describe("the real ReportsPanel: the key bar and the form share one provider", () => {
  it("a key typed into the key bar is the key the export sends, with nothing in localStorage beforehand", async () => {
    // the browser download needs a real blob URL, which jsdom does not have
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:test", revokeObjectURL: () => undefined }));
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    mount(<ReportsPanel />);
    await act(async () => undefined);
    const keyInput = host.querySelector<HTMLInputElement>('input[aria-label="API key"]');
    expect(keyInput).not.toBeNull();
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      setter?.call(keyInput, "aah_typed_key_123");
      keyInput?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => host.querySelector<HTMLButtonElement>("button.primary")?.click());
    expect(host.textContent).toContain("Signed in with an API key.");
    await act(async () => {
      host.querySelector<HTMLButtonElement>('button[type="submit"]')?.click();
    });
    expect(actionMocks.exportCsvAction).toHaveBeenCalledWith(expect.objectContaining({ apiKey: "aah_typed_key_123", format: "csv" }));
    expect(host.textContent).toContain("Saved report-2026-10.csv");
  });
});
