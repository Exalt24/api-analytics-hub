"use client";

import { useCallback, useEffect, useState } from "react";
import { addPromoAction, exportPurchaseOrdersAction, loadPlanAction } from "@/app/inventory/actions";
import { KeyGate } from "@/components/KeyGate";
import { KpiCard } from "@/components/KpiCard";
import { FILTERS, LABEL_TEXT, SHORTAGE_TEXT, STATUS_TEXT, basisText, coverText, matchesFilter, trendText, velocityText, type LabelFilter } from "@/lib/inventory/format";
import { promoFormSchema, type PoFormat } from "@/lib/inventory/input";
import type { PlanView } from "@/lib/inventory/view";
import { saveFile } from "@/lib/report/download";

function storedKey(): string | undefined {
  try {
    return window.localStorage.getItem("apiKey") || undefined;
  } catch {
    return undefined;
  }
}

/** The key bar, the exceptions, the planning table, the purchase order drafts and the promotion form, all over the stored plan. */
export function InventoryPanel() {
  const [key, setKey] = useState<string | undefined>(undefined);
  const [plan, setPlan] = useState<PlanView | undefined>(undefined);
  const [error, setError] = useState<string | undefined>(undefined);
  const [notice, setNotice] = useState<string | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<LabelFilter>("all");
  const [ready, setReady] = useState(false);

  const load = useCallback(async (apiKey: string | undefined) => {
    setError(undefined);
    if (!apiKey) {
      setPlan(undefined);
      return;
    }
    setLoading(true);
    try {
      const result = await loadPlanAction({ apiKey });
      if (result.ok) setPlan(result.plan);
      else {
        setPlan(undefined);
        setError(result.message);
      }
    } catch {
      setError("Could not reach the server. Try again.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const k = storedKey();
    setKey(k);
    setReady(true);
    void load(k);
  }, [load]);

  function onKey(next: string | undefined) {
    setKey(next);
    void load(next);
  }

  async function download(format: PoFormat, supplier?: string) {
    if (!key) return;
    setError(undefined);
    setNotice(undefined);
    try {
      const result = await exportPurchaseOrdersAction({ apiKey: key, format, ...(supplier ? { supplier } : {}) });
      if (!result.ok) {
        setError(result.message);
        return;
      }
      saveFile(result.base64, result.filename, result.mimeType);
      setNotice(`Saved ${result.filename}`);
    } catch {
      setError("Could not reach the server. Try again.");
    }
  }

  const lines = plan ? plan.lines.filter((l) => matchesFilter(l, filter)) : [];
  const shortages = plan ? plan.lines.filter((l) => l.shortage) : [];

  return (
    <>
      <KeyGate onSet={(k) => onKey(k ?? undefined)} />

      {error ? (
        <div className="error" role="alert">
          {error}
        </div>
      ) : undefined}
      {notice ? (
        <div className="ok-note" role="status">
          {notice}
        </div>
      ) : undefined}
      {loading && !plan ? <p className="empty">Loading the stored plan.</p> : undefined}

      {plan ? (
        <>
          <p className="sub" data-testid="plan-date">
            Plan for {plan.asOf}, {plan.tenantName}. {plan.alerts.length} alert{plan.alerts.length === 1 ? "" : "s"} raised so far.
          </p>
          <section className="cards" aria-label="Summary">
            <KpiCard label="Variants" value={String(plan.summary.variants)} />
            <KpiCard label="Shortages" value={String(plan.summary.shortages)} hint="short on cover or out of stock" />
            <KpiCard label="To reorder" value={String(plan.summary.toReorder)} />
            <KpiCard label="Best sellers" value={String(plan.summary.bestSellers)} />
            <KpiCard label="Rising" value={String(plan.summary.rising)} />
            <KpiCard label="Slow movers" value={String(plan.summary.slowMovers)} />
          </section>

          <section className="inv-section" aria-labelledby="exceptions">
            <h2 id="exceptions">Exceptions</h2>
            {shortages.length === 0 ? (
              <p className="empty">Nothing is short against its lead time.</p>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Variant</th>
                      <th>Problem</th>
                      <th>Cover (days)</th>
                      <th>Lead time</th>
                      <th>Runs out</th>
                      <th>Suggested order</th>
                      <th>Supplier</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shortages.map((l) => (
                      <tr key={l.variantId}>
                        <td>{l.name}</td>
                        <td>
                          <span className="inv-tag short">{l.shortage ? SHORTAGE_TEXT[l.shortage] : ""}</span>
                        </td>
                        <td>{coverText(l.coverDays)}</td>
                        <td>{l.leadTimeDays === null ? "-" : `${l.leadTimeDays} days`}</td>
                        <td>{l.stockoutDate ?? "-"}</td>
                        <td>{l.suggestedQty > 0 ? l.suggestedQty : "-"}</td>
                        <td>{l.supplier ?? "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="inv-section" aria-labelledby="orders">
            <h2 id="orders">Purchase order drafts</h2>
            {plan.orders.length === 0 ? (
              <p className="empty">Nothing needs ordering from a supplier.</p>
            ) : (
              <>
                <div className="inv-orders">
                  {plan.orders.map((o) => (
                    <div className="inv-order" key={o.supplier} data-testid={`po-${o.supplier}`}>
                      <h3>{o.supplier}</h3>
                      <div className="muted">
                        Lead time {o.leadTimeDays} days, expected {o.expectedOn}
                        {o.total ? `, ${o.total}` : ""}
                      </div>
                      <ul>
                        {o.lines.map((l) => (
                          <li key={l.sku + l.description}>
                            {l.quantity} x {l.description}
                            {l.sku ? ` (${l.sku})` : ""}
                          </li>
                        ))}
                      </ul>
                      {o.linesWithoutCost > 0 ? <p className="inv-note">{o.linesWithoutCost} line(s) have no unit cost and are left out of the total.</p> : undefined}
                      <div className="inv-actions">
                        <button type="button" disabled={!ready} onClick={() => void download("csv", o.supplier)}>
                          CSV for {o.supplier}
                        </button>
                        <button type="button" disabled={!ready} onClick={() => void download("xlsx", o.supplier)}>
                          Excel for {o.supplier}
                        </button>
                        <button type="button" disabled={!ready} onClick={() => void download("sheets", o.supplier)}>
                          Sheets paste for {o.supplier}
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
                <div className="inv-actions">
                  <button type="button" disabled={!ready} onClick={() => void download("csv")}>
                    All suppliers, CSV
                  </button>
                  <button type="button" disabled={!ready} onClick={() => void download("xlsx")}>
                    All suppliers, Excel
                  </button>
                </div>
                <p className="inv-note">A draft is for you to approve and send.</p>
              </>
            )}
          </section>

          <section className="inv-section" aria-labelledby="planning">
            <h2 id="planning">Planning table</h2>
            <div className="inv-toolbar">
              <label className="field">
                <span>Show</span>
                <select aria-label="Show" value={filter} onChange={(e) => setFilter(e.target.value as LabelFilter)}>
                  {FILTERS.map((f) => (
                    <option key={f.value} value={f.value}>
                      {f.text}
                    </option>
                  ))}
                </select>
              </label>
              <span className="muted">
                {lines.length} of {plan.lines.length} variants
              </span>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Variant</th>
                    <th>Class</th>
                    <th>Label</th>
                    <th>On hand</th>
                    <th>Sold per day</th>
                    <th>Trend</th>
                    <th>Cover (days)</th>
                    <th>Lead time</th>
                    <th>Safety stock</th>
                    <th>Reorder point</th>
                    <th>Order</th>
                    <th>Revenue</th>
                  </tr>
                </thead>
                <tbody>
                  {lines.map((l) => (
                    <tr key={l.variantId}>
                      <td>
                        {l.name}
                        {l.sku ? <span className="muted"> {l.sku}</span> : undefined}
                        {STATUS_TEXT[l.status] ? <div className="muted">{STATUS_TEXT[l.status]}</div> : undefined}
                      </td>
                      <td>{l.abcClass}</td>
                      <td>
                        <span className={`inv-tag ${l.label}`}>{LABEL_TEXT[l.label]}</span>
                      </td>
                      <td>
                        {l.available}
                        {l.incoming > 0 ? <span className="muted"> +{l.incoming} inbound</span> : undefined}
                      </td>
                      <td title={basisText(l.countedDays, l.windowDays, l.outDays, l.unknownDays)}>{velocityText(l.velocity)}</td>
                      <td>{trendText(l.trendRatio, l.label)}</td>
                      <td className={l.shortage ? "bad" : undefined}>{coverText(l.coverDays)}</td>
                      <td>{l.leadTimeDays === null ? "-" : `${l.leadTimeDays}`}</td>
                      <td>{l.safetyStock ?? "-"}</td>
                      <td>{l.reorderPoint ?? "-"}</td>
                      <td>{l.suggestedQty > 0 ? l.suggestedQty : "-"}</td>
                      <td>{l.revenue}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="inv-note">Sold per day counts only days on sale. Hover it for the days used.</p>
          </section>

          <PromoForm apiKey={key} ready={ready} onSaved={() => load(key)} setError={setError} setNotice={setNotice} />
        </>
      ) : undefined}
    </>
  );
}

type PromoProps = {
  apiKey: string | undefined;
  ready: boolean;
  onSaved: () => Promise<void>;
  setError: (message: string | undefined) => void;
  setNotice: (message: string | undefined) => void;
};

/** A dated promotion factor, entered by a person; the next plan run multiplies the forecast by it. */
function PromoForm({ apiKey, ready, onSaved, setError, setNotice }: PromoProps) {
  const [sku, setSku] = useState("");
  const [startsOn, setStartsOn] = useState("");
  const [endsOn, setEndsOn] = useState("");
  const [factor, setFactor] = useState("1.5");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(undefined);
    setNotice(undefined);
    const draft = { sku: sku || undefined, startsOn, endsOn, factor: Number(factor), note: note || undefined };
    const checked = promoFormSchema.safeParse(draft);
    if (!checked.success) {
      setError(checked.error.issues[0]?.message ?? "Check the promotion.");
      return;
    }
    if (!apiKey) {
      setError("Add your key above first.");
      return;
    }
    setBusy(true);
    try {
      const result = await addPromoAction({ ...checked.data, apiKey });
      if (!result.ok) {
        setError(result.message);
        return;
      }
      setNotice("Promotion saved. The next plan uses it.");
      await onSaved();
    } catch {
      setError("Could not reach the server. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="inv-section" aria-labelledby="promo">
      <h2 id="promo">Promotion</h2>
      <p className="sub">A factor of 1.5 means half as much again. Leave the SKU empty for all variants.</p>
      <form className="inv-form" onSubmit={(e) => void submit(e)} noValidate>
        <label>
          SKU
          <input value={sku} onChange={(e) => setSku(e.target.value)} placeholder="all variants" />
        </label>
        <label>
          From
          <input type="date" value={startsOn} onChange={(e) => setStartsOn(e.target.value)} />
        </label>
        <label>
          To
          <input type="date" value={endsOn} onChange={(e) => setEndsOn(e.target.value)} />
        </label>
        <label>
          Factor
          <input inputMode="decimal" value={factor} onChange={(e) => setFactor(e.target.value)} />
        </label>
        <label>
          Note
          <input value={note} onChange={(e) => setNote(e.target.value)} />
        </label>
        <button type="submit" className="primary" disabled={!ready || busy}>
          {busy ? "Saving" : "Save promotion"}
        </button>
      </form>
    </section>
  );
}
