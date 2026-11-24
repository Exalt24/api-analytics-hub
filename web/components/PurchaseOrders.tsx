"use client";

import { useCallback, useEffect, useState } from "react";
import { loadOrdersAction, moveOrderAction, receiveOrderAction, setExpectedDateAction } from "@/app/inventory/actions";
import { ORDER_ACTION_TEXT, ORDER_STATUS_TEXT } from "@/lib/inventory/format";
import { DAY } from "@/lib/inventory/input";
import type { OrderView, OrdersResult } from "@/lib/inventory/orders";

type Props = {
  apiKey: string | undefined;
  ready: boolean;
  /** Changes when something outside made an order, so the list is read again. */
  reloadToken: number;
  setError: (message: string | undefined) => void;
  setNotice: (message: string | undefined) => void;
};

/** The purchase orders of the tenant: what was ordered, where each one stands, when it is expected, and what has arrived. The rules are the database's; this draws the next legal buttons. */
export function PurchaseOrders({ apiKey, ready, reloadToken, setError, setNotice }: Props) {
  const [orders, setOrders] = useState<OrderView[] | undefined>(undefined);

  const load = useCallback(async () => {
    if (!apiKey) {
      setOrders(undefined);
      return;
    }
    try {
      const result = await loadOrdersAction({ apiKey });
      if (result.ok) setOrders(result.orders);
      else setError(result.message);
    } catch {
      setError("Could not reach the server. Try again.");
    }
  }, [apiKey, setError]);

  useEffect(() => {
    void load();
  }, [load, reloadToken]);

  /** Run a change, show its refusal or its notice, and take the orders it returns. */
  async function apply(change: () => Promise<OrdersResult>, done: string): Promise<boolean> {
    setError(undefined);
    setNotice(undefined);
    try {
      const result = await change();
      if (!result.ok) {
        setError(result.message);
        return false;
      }
      setOrders(result.orders);
      setNotice(done);
      return true;
    } catch {
      setError("Could not reach the server. Try again.");
      return false;
    }
  }

  if (!orders) return undefined;
  const live = orders.filter((o) => o.status !== "received" && o.status !== "cancelled");
  const closed = orders.filter((o) => o.status === "received" || o.status === "cancelled");

  return (
    <section className="inv-section" aria-labelledby="po-tracking">
      <h2 id="po-tracking">Purchase orders</h2>
      {orders.length === 0 ? (
        <p className="empty">No order was made yet. Create one from a draft above.</p>
      ) : (
        <>
          <div className="inv-orders">
            {live.map((o) => (
              <OrderCard key={o.id} order={o} apiKey={apiKey} ready={ready} apply={apply} />
            ))}
          </div>
          {closed.length > 0 ? (
            <>
              <h3>Finished</h3>
              <div className="inv-orders">
                {closed.map((o) => (
                  <OrderCard key={o.id} order={o} apiKey={apiKey} ready={ready} apply={apply} />
                ))}
              </div>
            </>
          ) : undefined}
        </>
      )}
    </section>
  );
}

type CardProps = {
  order: OrderView;
  apiKey: string | undefined;
  ready: boolean;
  apply: (change: () => Promise<OrdersResult>, done: string) => Promise<boolean>;
};

function OrderCard({ order, apiKey, ready, apply }: CardProps) {
  const [expected, setExpected] = useState(order.expectedOn ?? "");
  const [quantities, setQuantities] = useState<Record<number, string>>({});
  const [allowOver, setAllowOver] = useState(false);
  const [localError, setLocalError] = useState<string | undefined>(undefined);
  const key = apiKey ?? "";
  const label = order.supplier;

  // placing an order gives it a date and a person can change it on the server, so the field follows the order it belongs to
  useEffect(() => {
    setExpected(order.expectedOn ?? "");
  }, [order.expectedOn]);

  async function move(to: "ordered" | "confirmed" | "shipped" | "cancelled") {
    if (!apiKey) return;
    await apply(() => moveOrderAction({ apiKey: key, orderId: order.id, to }), `${label}: ${ORDER_STATUS_TEXT[to].toLowerCase()}.`);
  }

  async function saveDate() {
    setLocalError(undefined);
    if (!DAY.test(expected)) {
      setLocalError("Use a date like 2026-12-01");
      return;
    }
    await apply(() => setExpectedDateAction({ apiKey: key, orderId: order.id, expectedOn: expected }), `${label}: expected ${expected}.`);
  }

  async function receive() {
    setLocalError(undefined);
    const lines = order.lines
      .map((l) => ({ lineId: l.id, quantity: Number(quantities[l.id] ?? "") }))
      .filter((l) => (quantities[l.lineId] ?? "") !== "");
    if (lines.length === 0 || lines.some((l) => !Number.isInteger(l.quantity) || l.quantity <= 0)) {
      setLocalError("Enter a whole quantity above 0 on at least one line.");
      return;
    }
    const ok = await apply(() => receiveOrderAction({ apiKey: key, orderId: order.id, lines, allowOver }), `${label}: receipt recorded.`);
    if (ok) setQuantities({});
  }

  return (
    <div className="inv-order" data-testid={`order-${order.id}`}>
      <h3>
        {order.supplier} <span className={`inv-tag po-${order.status}`}>{ORDER_STATUS_TEXT[order.status]}</span>
        {order.late ? <span className="inv-tag short"> Late</span> : undefined}
      </h3>
      <div className="muted">
        {order.orderedOn ? `Ordered ${order.orderedOn}` : "Not placed yet"}
        {order.expectedOn ? `, expected ${order.expectedOn}` : ""}
        {order.total ? `, ${order.total}` : ""}
      </div>
      <table>
        <thead>
          <tr>
            <th>Line</th>
            <th>Ordered</th>
            <th>Received</th>
            <th>Open</th>
            {order.canReceive ? <th>Receive</th> : undefined}
          </tr>
        </thead>
        <tbody>
          {order.lines.map((l) => (
            <tr key={l.id}>
              <td>
                {l.description}
                {l.sku ? <span className="muted"> {l.sku}</span> : undefined}
              </td>
              <td>{l.ordered}</td>
              <td>
                {l.received}
                {l.overReceived ? <span className="muted"> accepted over</span> : undefined}
              </td>
              <td>{l.open}</td>
              {order.canReceive ? (
                <td>
                  <input
                    className="inv-qty"
                    inputMode="numeric"
                    aria-label={`Received for ${l.sku ?? l.description}`}
                    value={quantities[l.id] ?? ""}
                    onChange={(e) => setQuantities({ ...quantities, [l.id]: e.target.value })}
                  />
                </td>
              ) : undefined}
            </tr>
          ))}
        </tbody>
      </table>
      {order.linesWithoutCost > 0 ? <p className="inv-note">{order.linesWithoutCost} line(s) have no unit cost and are left out of the total.</p> : undefined}
      {localError ? (
        <p className="inv-note" role="alert">
          {localError}
        </p>
      ) : undefined}
      <div className="inv-actions">
        {order.nextStatuses.map((to) =>
          to === "draft" || to === "partly_received" || to === "received" ? undefined : (
            <button key={to} type="button" disabled={!ready} onClick={() => void move(to)}>
              {ORDER_ACTION_TEXT[to]}
            </button>
          ),
        )}
        {order.canReceive ? (
          <>
            <button type="button" disabled={!ready} onClick={() => void receive()}>
              Record receipt
            </button>
            <label className="inv-check">
              <input type="checkbox" checked={allowOver} onChange={(e) => setAllowOver(e.target.checked)} /> Accept more than ordered
            </label>
          </>
        ) : undefined}
      </div>
      {order.canReceive ? (
        <div className="inv-actions">
          <label className="inv-check">
            Expected date
            <input type="date" aria-label={`Expected date for ${label}`} value={expected} onChange={(e) => setExpected(e.target.value)} />
          </label>
          <button type="button" disabled={!ready} onClick={() => void saveDate()}>
            Save date
          </button>
        </div>
      ) : undefined}
    </div>
  );
}
