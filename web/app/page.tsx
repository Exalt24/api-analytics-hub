"use client";

import { useCallback, useEffect, useState } from "react";
import { KeyGate } from "@/components/KeyGate";
import { KpiCard } from "@/components/KpiCard";
import { SyncButton } from "@/components/SyncButton";
import { WindowPicker } from "@/components/WindowPicker";
import {
  api,
  ApiError,
  formatCount,
  formatMoney,
  relativeAge,
  type Connection,
  type KpiResponse,
  type SyncRun,
} from "@/lib/api";
import { byDay, METRICS, totals } from "@/lib/kpis";

/**
 * The dashboard.
 *
 * It reads the API, which reads Postgres. It never touches Shopify, which is the
 * property the whole architecture exists for, and the "synced" line makes that
 * visible instead of implying the figures are live.
 */
export default function Page() {
  const [days, setDays] = useState(7);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [data, setData] = useState<KpiResponse | null>(null);
  const [runs, setRuns] = useState<SyncRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [k, c, r] = await Promise.all([
        api.kpis(days, selected ?? undefined),
        api.connections(),
        api.runs(),
      ]);
      setData(k);
      setConnections(c.connections);
      setRuns(r.runs);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `${err.status}: ${err.message}`
          : "Could not reach the API",
      );
    } finally {
      setLoading(false);
    }
  }, [days, selected]);

  useEffect(() => {
    void load();
  }, [load]);

  const t = data ? totals(data.points) : {};
  const rows = data ? byDay(data.points) : [];

  return (
    <main>
      <header className="top">
        <div>
          <h1>Analytics</h1>
          {/* Staleness stated up front. The numbers are as old as the last sync
              and pretending otherwise is the failure this design prevents. */}
          <p className="sub">
            Reading stored data. Last sync {relativeAge(data?.last_synced_at ?? null)}.
          </p>
        </div>
        <div className="controls">
          <label className="field">
            <span>Connection</span>
            <select
              value={selected ?? ""}
              onChange={(e) => setSelected(e.target.value || null)}
            >
              <option value="">All connections</option>
              {connections.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.display_name ?? c.external_account} ({c.platform})
                </option>
              ))}
            </select>
          </label>
          <WindowPicker
            value={days}
            options={[7, 30, 90]}
            onChange={setDays}
            disabled={loading}
          />
          <SyncButton
            connectionId={selected ?? connections[0]?.id ?? null}
            days={days}
            onAccepted={() => void load()}
          />
        </div>
      </header>

      <KeyGate onSet={() => void load()} />

      {error ? (
        <div className="error" role="alert">
          {error}
        </div>
      ) : null}

      <section className="cards">
        {METRICS.map((spec) => {
          const cell = t[spec.key];
          const value =
            !cell || cell.value === null
              ? null
              : spec.kind === "money"
                ? formatMoney(cell.value, cell.currency)
                : formatCount(cell.value);
          return (
            <KpiCard
              key={spec.key}
              label={spec.label}
              value={loading && !data ? null : value}
              hint={
                spec.key === "avg_order_value"
                  ? "gross over orders"
                  : spec.key === "customers"
                    ? "distinct per day, not summable across days"
                    : undefined
              }
            />
          );
        })}
      </section>

      <section>
        <h2>By day</h2>
        {rows.length === 0 && !loading ? (
          <p className="empty">
            No stored data for this window. Run a sync, or widen the range.
          </p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Day</th>
                  {METRICS.map((m) => (
                    <th key={m.key}>{m.label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.day}>
                    <td>{row.day}</td>
                    {METRICS.map((m) => {
                      const p = row.values[m.key];
                      if (!p) return <td key={m.key} className="muted">-</td>;
                      return (
                        <td key={m.key}>
                          {m.kind === "money"
                            ? formatMoney(p.value_numeric, p.currency)
                            : formatCount(p.value_numeric)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section>
        <h2>Recent syncs</h2>
        {/* Exposed on purpose. "The number looks wrong" is unanswerable without a
            run log, and hiding failures does not make them stop happening. */}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Started</th>
                <th>Trigger</th>
                <th>Status</th>
                <th>Rows</th>
                <th>API calls</th>
                <th>Throttle waits</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {runs.length === 0 ? (
                <tr>
                  <td colSpan={7} className="muted">
                    No syncs recorded yet.
                  </td>
                </tr>
              ) : (
                runs.map((r) => (
                  <tr key={r.id}>
                    <td>{r.started_at?.replace("T", " ").slice(0, 16) ?? "-"}</td>
                    <td>{r.trigger}</td>
                    <td className={r.status === "failed" ? "bad" : ""}>{r.status}</td>
                    <td>{r.rows_written}</td>
                    <td>{r.api_calls}</td>
                    <td>{r.throttle_waits}</td>
                    <td className="muted">{r.error_detail ?? ""}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}
