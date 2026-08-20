/**
 * The only place that knows how to talk to the backend.
 *
 * ONE MODULE ON PURPOSE. Every fetch in the app goes through here, so the base
 * URL, the bearer token, the error shape and the response types are defined once.
 * When a professional design is dropped in later, the components change and this
 * does not, which is the "well-structured frontend architecture" the brief asks
 * for rather than a folder of pretty components each doing its own fetch.
 *
 * NOTHING IS CACHED. The backend already reads from stored snapshots, so caching
 * here would only add a second staleness layer on top of the sync interval, and
 * then "the number is wrong" has two possible causes instead of one.
 */

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

export type KpiPoint = {
  observed_on: string;
  metric_key: string;
  value_numeric: number;
  currency: string | null;
};

export type KpiResponse = {
  window_days: number;
  metrics: string[];
  points: KpiPoint[];
  last_synced_at: string | null;
};

export type Connection = {
  id: string;
  platform: string;
  external_account: string;
  display_name: string | null;
  status: string;
  token_expires_at: string | null;
};

export type SyncRun = {
  id: string;
  connection_id: string;
  trigger: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  rows_written: number;
  api_calls: number;
  throttle_waits: number;
  error_detail: string | null;
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

function token(): string {
  // Read at call time, not at module load. A token captured at import survives a
  // sign-out, which is the kind of bug that only shows up on someone else's
  // machine.
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem("apiKey") ?? "";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token() ? { Authorization: `Bearer ${token()}` } : {}),
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  });

  if (!res.ok) {
    // Surface the server's own message where there is one. A generic "request
    // failed" turns a 403 that says exactly which permission is missing into a
    // support ticket.
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* a non-JSON error body is still an error, just a less useful one */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export const api = {
  kpis: (days: number, connectionId?: string) =>
    request<KpiResponse>(
      `/api/kpis?days=${days}${connectionId ? `&connection_id=${connectionId}` : ""}`,
    ),
  connections: () => request<{ connections: Connection[] }>("/api/connections"),
  runs: () => request<{ runs: SyncRun[] }>("/api/runs"),
  syncNow: (connectionId: string, days: number) =>
    request<{ accepted: boolean }>("/api/sync", {
      method: "POST",
      body: JSON.stringify({ connection_id: connectionId, days }),
    }),
};

/**
 * Money arrives as an integer in minor units with its currency beside it, and it
 * is formatted HERE and nowhere else.
 *
 * The integer never becomes a float anywhere in the system; this is the single
 * point where it becomes a string for a human, which is why a rounding decision
 * cannot leak back into a stored value.
 */
export function formatMoney(minor: number, currency: string | null): string {
  const code = currency ?? "USD";
  // Zero-decimal currencies exist. Dividing JPY by 100 invents a fractional yen.
  const zeroDecimal = new Set(["JPY", "KRW", "VND", "CLP", "ISK"]);
  const value = zeroDecimal.has(code) ? minor : minor / 100;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: code,
    maximumFractionDigits: zeroDecimal.has(code) ? 0 : 2,
  }).format(value);
}

export function formatCount(n: number): string {
  return new Intl.NumberFormat("en-US").format(n);
}

/** Human-readable staleness. A dashboard that hides its own age implies live data. */
export function relativeAge(iso: string | null): string {
  if (!iso) return "never synced";
  const then = new Date(iso).getTime();
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}
