"""Read one tenant's stored ad and booking snapshots and produce the combined ad spend view.

The only database code in the ad spend view. It reads `metric_snapshot` inside a tenant-scoped transaction, so row level security filters it;
nothing here calls a vendor API, the property the dashboard and the monthly report have too. The arithmetic and the wording are in `adspend.py`.

Every active connection of an ad platform becomes one line of the view, labelled by its display name, so a tenant with two Meta accounts or a Google
and a TikTok account gets each separately and nothing is summed across accounts in different currencies. At most one active Shopmonkey connection
supplies the booked jobs; two would double them, so two is refused as the monthly report refuses two stores.
"""
from __future__ import annotations

from datetime import date

from app.db import Database
from app.report.adspend import AD_PLATFORMS, BOOKINGS_PLATFORM, AdSpendView, build
from app.report.monthly import ReportError

_ROWS = ("select observed_on, metric_key, value_numeric, currency from metric_snapshot "
         "where connection_id = $1 and observed_on >= $2 and observed_on < $3 order by observed_on, metric_key")


async def load_view(db: Database, tenant_id: str, start: date, end: date) -> AdSpendView:
    if end <= start:
        raise ReportError("the window must hold at least one day")
    async with db.tenant(tenant_id) as conn:
        ads = await conn.fetch(
            "select id, platform, coalesce(display_name, external_account) as label from platform_connection "
            "where status = 'active' and platform = any($1::text[]) order by created_at", list(AD_PLATFORMS))
        shops = await conn.fetch(
            "select id from platform_connection where status = 'active' and platform = $1 order by created_at", BOOKINGS_PLATFORM)
        if len(shops) > 1:
            raise ReportError("this tenant has %d active %s connections, so the booked jobs would be counted twice" % (len(shops), BOOKINGS_PLATFORM))
        if not ads:
            raise ReportError("this tenant has no active ad connection (%s)" % ", ".join(AD_PLATFORMS))
        labels = [(r["platform"], r["label"]) for r in ads]
        if len(set(labels)) != len(labels):
            raise ReportError("two ad connections of one platform share a display name; name them apart")
        ad_rows = {(r["platform"], r["label"]): [dict(x) for x in await conn.fetch(_ROWS, r["id"], start, end)] for r in ads}
        bookings = [dict(x) for x in await conn.fetch(_ROWS, shops[0]["id"], start, end)] if shops else []
    return build(ad_rows, start, end, bookings=bookings)
