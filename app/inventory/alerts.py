"""Shortage alerts for the inventory plan, raised once and delivered through the hub's own outbox.

THREE KINDS, at most one for a variant on a run, in this order. `out_of_stock`: nothing is available and the variant sells. `cover_below_lead_time`: the days of cover at the normal
velocity are fewer than the supplier's lead time, so an order placed today would arrive after the shelf is empty. `stockout_projected`: the cover is enough at the normal velocity but
a promotion a person entered brings the projected stockout inside the lead time. A variant that does not sell, is not tracked or has no stock reading raises nothing; a variant with no
supplier can still be out of stock but has no lead time to compare cover with.

RAISED ONCE. The unit of an alert is a stock cycle: `event_ref` is the date of the last restock the snapshots show (or `start` when none was ever seen), and the table has a UNIQUE key on
(tenant, variant, kind, event_ref) with an `on conflict do nothing` insert, the pattern of change_event in migrations/005. A daily run, a rerun of the same day and a restart cannot raise a
second alert for the same shortage; a new restock begins a new cycle, so the next shortage alerts again. The payload is written once, when the alert is raised.

DELIVERED BY THE EXISTING CODE. The outbox has the columns of change_outbox, and `app.changes.deliver_pending(..., table="inventory_alert_outbox")` claims, pushes and records the outcome
with its lease, backoff and dead letter rules; nothing of that is copied here. The repeat policy for a digest of what is still open is `app.health.decide`, the function the sync health
check and the backup check use.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

import asyncpg
import httpx

from app.changes import SinkAnswer

from . import model

KINDS = ("out_of_stock", "cover_below_lead_time", "stockout_projected")
OUTBOX_TABLE = "inventory_alert_outbox"


def shortage_kind(status: str, velocity: float, available: int, lead_time_days: int | None, cover_days: float | None, forecast_cover_days: float | None) -> str | None:
    """The one shortage a variant shows, or None. The arguments are the fields of a plan row, so the same rule reads a fresh PlanRow and a stored plan line."""
    if status in ("untracked", "no_stock_reading") or velocity <= 0:
        return None
    if available <= 0:
        return "out_of_stock"
    if status != "ok" or lead_time_days is None:
        return None
    if cover_days is not None and cover_days < lead_time_days:
        return "cover_below_lead_time"
    if forecast_cover_days is not None and forecast_cover_days < lead_time_days:
        return "stockout_projected"
    return None


def event_ref(restock: date | None) -> str:
    return restock.isoformat() if restock else "start"


@dataclass(frozen=True)
class Raised:
    new: int
    already_raised: int


async def raise_alerts(conn: asyncpg.Connection, tenant_id: str, as_of: date, lines: list[asyncpg.Record], snapshots: dict[str, dict[date, int]]) -> Raised:
    """Raise the shortages of a stored plan. Each new one gets its outbox row, with the payload and the idempotency key the sink will see. Runs in the caller's tenant transaction."""
    new = seen = 0
    for ln in lines:
        row_kind = _kind_of_line(ln)
        if row_kind is None:
            continue
        variant_id = str(ln["variant_id"])
        ref = event_ref(model.last_restock(snapshots.get(variant_id, {})))
        detail: dict[str, Any] = {
            "variant_id": variant_id, "sku": ln["sku"], "product": ln["product_title"], "variant": ln["variant_title"], "supplier": ln["supplier_name"],
            "available": ln["available"], "velocity": round(ln["velocity"], 4), "cover_days": None if ln["cover_days"] is None else round(ln["cover_days"], 1),
            "forecast_cover_days": None if ln["forecast_cover_days"] is None else round(ln["forecast_cover_days"], 1), "lead_time_days": ln["lead_time_days"],
            "stockout_date": ln["stockout_date"].isoformat() if ln["stockout_date"] else None, "suggested_qty": ln["suggested_qty"], "abc_class": ln["abc_class"], "label": ln["label"]}
        alert_id = await conn.fetchval(
            """insert into inventory_alert (tenant_id, variant_id, kind, event_ref, as_of, detail) values ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb)
               on conflict on constraint inventory_alert_natural_key do nothing returning id""", tenant_id, variant_id, row_kind, ref, as_of, json.dumps(detail))
        if alert_id is None:
            seen += 1
            continue
        new += 1
        key = "%s:%s:%s:%s" % (tenant_id, variant_id, row_kind, ref)
        payload = {"alert_id": alert_id, "tenant_id": tenant_id, "kind": row_kind, "event_ref": ref, "as_of": as_of.isoformat(), "external_key": key, **detail}
        await conn.execute("insert into inventory_alert_outbox (event_id, tenant_id, idempotency_key, payload) values ($1, $2::uuid, $3, $4::jsonb)", alert_id, tenant_id, key, json.dumps(payload))
    return Raised(new, seen)


def _kind_of_line(ln: asyncpg.Record) -> str | None:
    return shortage_kind(ln["status"], ln["velocity"], ln["available"], ln["lead_time_days"], ln["cover_days"], ln["forecast_cover_days"])


# ------------------------------------------------------------------------------------------------------- sinks

def describe_alert(payload: dict[str, Any]) -> str:
    name = payload.get("product") or "a product"
    variant = payload.get("variant")
    title = "%s - %s" % (name, variant) if variant and variant != "Default Title" else name
    sku = " (%s)" % payload["sku"] if payload.get("sku") else ""
    kind = payload["kind"]
    if kind == "out_of_stock":
        head = "%s%s is out of stock and it sells %.2f a day." % (title, sku, payload.get("velocity") or 0)
    elif kind == "cover_below_lead_time":
        head = "%s%s has %.1f days of cover and the supplier needs %s days." % (title, sku, payload.get("cover_days") or 0, payload.get("lead_time_days"))
    else:
        head = "%s%s runs out about %s with the promotion entered, inside the %s day lead time." % (title, sku, payload.get("stockout_date"), payload.get("lead_time_days"))
    tail = " Suggested order: %s units from %s." % (payload["suggested_qty"], payload.get("supplier") or "no supplier set") if payload.get("suggested_qty") else ""
    return head + tail


def webhook_sink(url: str, *, client: httpx.Client | None = None, timeout: float = 10.0) -> Callable[[dict[str, Any], str], SinkAnswer]:
    """A Slack style incoming webhook as a sink. 2xx is ok, 429 is throttled (with Retry-After), another 4xx is rejected for good, a 5xx or a transport failure is ambiguous (retried
    with the same key). The URL is a secret and is never put into an answer or an error."""
    http = client or httpx.Client(timeout=timeout)

    def sink(payload: dict[str, Any], key: str) -> SinkAnswer:
        try:
            resp = http.post(url, json={"text": describe_alert(payload)}, headers={"Idempotency-Key": key})
        except httpx.HTTPError as exc:
            return SinkAnswer("ambiguous", None, type(exc).__name__)
        if resp.status_code < 300:
            return SinkAnswer("ok", key)
        if resp.status_code == 429:
            wait = resp.headers.get("Retry-After")
            return SinkAnswer("throttled", None, "HTTP 429", float(wait) if wait and wait.replace(".", "", 1).isdigit() else None)
        if resp.status_code >= 500:
            return SinkAnswer("ambiguous", None, "HTTP %d" % resp.status_code)
        return SinkAnswer("rejected", None, "HTTP %d" % resp.status_code)

    return sink


# ------------------------------------------------------------------------------------------------------- the digest of what is still open

@dataclass(frozen=True)
class OpenShortage:
    """A shortage still open, shaped for `app.health.decide`, which keys a problem by `.key` and alerts again only when the set changes or the repeat window passes."""

    variant_id: str
    kind: str
    title: str
    detail: str

    @property
    def key(self) -> str:
        return "%s:%s" % (self.variant_id, self.kind)


def open_shortages(lines: list[asyncpg.Record]) -> list[OpenShortage]:
    out: list[OpenShortage] = []
    for ln in lines:
        kind = _kind_of_line(ln)
        if kind is None:
            continue
        payload = {"product": ln["product_title"], "variant": ln["variant_title"], "sku": ln["sku"], "kind": kind, "velocity": ln["velocity"], "cover_days": ln["cover_days"],
                   "lead_time_days": ln["lead_time_days"], "stockout_date": ln["stockout_date"], "suggested_qty": ln["suggested_qty"], "supplier": ln["supplier_name"]}
        out.append(OpenShortage(str(ln["variant_id"]), kind, ln["product_title"], describe_alert(payload)))
    return sorted(out, key=lambda s: (KINDS.index(s.kind), s.title, s.variant_id))


def describe_shortages(shortages: list[OpenShortage]) -> str:
    if not shortages:
        return "No variant is short against its lead time."
    return "\n".join(["%d inventory exception(s):" % len(shortages)] + ["- " + s.detail for s in shortages])
