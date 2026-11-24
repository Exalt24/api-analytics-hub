"""The inventory alert rules that need no database: which shortage a variant shows, the webhook sink's answers, and the digest of open shortages run through the hub's own alert policy.

The digest is not a second policy. `app.health.decide` is the function the sync health check and the backup check use; here it is given shortages (anything with a `.key`) and a describer,
and the same rules are checked over seeded worlds: an unchanged set alerts once inside the repeat window, a changed set alerts at once, a cleared set sends one recovery.
"""
from __future__ import annotations

import random
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.changes import SinkAnswer  # noqa: E402
from app.health import decide  # noqa: E402
from app.inventory import alerts  # noqa: E402
from app.inventory.alerts import OpenShortage, describe_alert, describe_shortages, event_ref, open_shortages, shortage_kind, webhook_sink  # noqa: E402

OK = dict(status="ok", velocity=2.0, available=20, lead_time_days=9, cover_days=10.0, forecast_cover_days=10.0)


@pytest.mark.parametrize("change,expected", [
    ({}, None),
    ({"available": 0}, "out_of_stock"),
    ({"available": -2}, "out_of_stock"),
    ({"cover_days": 8.99}, "cover_below_lead_time"),
    ({"cover_days": 9.0}, None),                                                         # exactly the lead time is not short
    ({"forecast_cover_days": 8.0}, "stockout_projected"),
    ({"cover_days": 5.0, "forecast_cover_days": 3.0}, "cover_below_lead_time"),            # one kind, the plainer one
    ({"available": 0, "cover_days": 0.0, "forecast_cover_days": 0.0}, "out_of_stock"),
    ({"velocity": 0.0, "available": 0}, None),                                           # nothing sells: an empty shelf is not a shortage
    ({"status": "untracked", "available": 0}, None),
    ({"status": "no_stock_reading", "available": 0}, None),
    ({"status": "no_supplier", "lead_time_days": None, "cover_days": 1.0}, None),         # no lead time to compare cover with
    ({"status": "no_supplier", "lead_time_days": None, "available": 0}, "out_of_stock"),
    ({"cover_days": None, "forecast_cover_days": None}, None),
])
def test_which_shortage_a_variant_shows(change, expected):
    args = OK | change
    assert shortage_kind(args["status"], args["velocity"], args["available"], args["lead_time_days"], args["cover_days"], args["forecast_cover_days"]) == expected


def test_the_event_ref_names_the_stock_cycle():
    assert event_ref(date(2026, 11, 14)) == "2026-11-14" and event_ref(None) == "start"


def _payload(**kw):
    return {"product": "Tee", "variant": "Default Title", "sku": "TEE-1", "kind": "cover_below_lead_time", "velocity": 2.0, "cover_days": 5.0, "lead_time_days": 9, "incoming": 0,
            "stockout_date": "2026-11-29", "suggested_qty": 60, "supplier": "Acme Goods"} | kw


def test_each_alert_is_worded_from_its_own_numbers():
    assert describe_alert(_payload()) == "Tee (TEE-1) has 5.0 days of cover and the supplier needs 9 days. Suggested order: 60 units from Acme Goods."
    assert describe_alert(_payload(kind="out_of_stock", suggested_qty=0)) == "Tee (TEE-1) is out of stock and it sells 2.00 a day."
    assert "runs out about 2026-11-29 with the promotion entered, inside the 9 day lead time" in describe_alert(_payload(kind="stockout_projected"))
    assert "12 units are inbound." in describe_alert(_payload(incoming=12))
    assert describe_alert(_payload(variant="Large", sku=None, supplier=None)).startswith("Tee - Large has")
    assert "no supplier set" in describe_alert(_payload(supplier=None))


def _sink(handler):
    return webhook_sink("https://hooks.invalid/secret-token", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_the_webhook_sink_maps_each_outcome_and_never_leaks_the_url():
    seen = []

    def make(status, headers=None, raises=None):
        def handler(request):
            seen.append((request.headers["idempotency-key"], request.read()))
            if raises:
                raise raises
            return httpx.Response(status, headers=headers or {})
        return handler

    assert _sink(make(200))(_payload(), "k1") == SinkAnswer("ok", "k1")
    assert _sink(make(429, {"Retry-After": "30"}))(_payload(), "k") == SinkAnswer("throttled", None, "HTTP 429", 30.0)
    assert _sink(make(429))(_payload(), "k") == SinkAnswer("throttled", None, "HTTP 429", None)
    assert _sink(make(503))(_payload(), "k") == SinkAnswer("ambiguous", None, "HTTP 503")
    assert _sink(make(400))(_payload(), "k") == SinkAnswer("rejected", None, "HTTP 400")
    lost = _sink(make(0, raises=httpx.ConnectTimeout("https://hooks.invalid/secret-token timed out")))(_payload(), "k")
    assert lost == SinkAnswer("ambiguous", None, "ConnectTimeout") and "secret-token" not in repr(lost)
    assert seen[0][0] == "k1" and b"Tee (TEE-1)" in seen[0][1]


def _line(vid: str, **kw):
    base = {"variant_id": vid, "product_title": "P" + vid, "variant_title": "Default Title", "sku": None, "status": "ok", "velocity": 2.0, "available": 20, "incoming": 0,
            "lead_time_days": 9, "cover_days": 10.0, "forecast_cover_days": 10.0, "stockout_date": None, "suggested_qty": 0, "supplier_name": None} | kw
    base["shortage"] = shortage_kind(base["status"], base["velocity"], base["available"], base["lead_time_days"], base["cover_days"], base["forecast_cover_days"])
    return base


def test_open_shortages_lists_only_shortages_most_urgent_kind_first():
    lines = [_line("c", cover_days=4.0), _line("a", available=0), _line("b"), _line("d", forecast_cover_days=2.0)]
    got = open_shortages(lines)
    assert [(s.variant_id, s.kind) for s in got] == [("a", "out_of_stock"), ("c", "cover_below_lead_time"), ("d", "stockout_projected")]
    assert describe_shortages([]) == "No variant is short against its lead time."
    text = describe_shortages(got)
    assert text.splitlines()[0] == "3 inventory exception(s):" and len(text.splitlines()) == 4


REPEAT = 6 * 3600


def test_the_digest_follows_the_hub_alert_policy_over_many_seeded_worlds():
    """R1 an unchanged set is quiet inside the window; R2 a changed set speaks at once and names exactly the open shortages; R3 a cleared set sends one recovery; R6 an unresolved set speaks again."""
    for seed in range(300):
        rng = random.Random(seed)
        state: dict = {}
        now = 1_000_000.0
        for _ in range(50):
            now += rng.choice((60, 900, 3600, REPEAT, REPEAT + 1))
            lines = [_line("v%d" % i, **rng.choice(({}, {"available": 0}, {"cover_days": 3.0}, {"forecast_cover_days": 2.0}))) for i in range(rng.randint(0, 5)) if rng.random() < 0.6]
            shortages = open_shortages(lines)
            keys = sorted(s.key for s in shortages)
            before = dict(state)
            message, state = decide(state, shortages, now, REPEAT, describer=describe_shortages)
            old = sorted(before.get("keys", []))
            if keys == old and keys:
                assert (message is not None) == (now - before.get("alerted_at", 0.0) >= REPEAT), seed
            elif keys:
                assert message is not None and all(s.detail in message for s in shortages), seed
            elif old:
                assert message is not None and message.startswith("Recovered"), seed
            else:
                assert message is None, seed


def test_a_shortage_is_keyed_by_variant_and_kind_so_a_change_of_kind_is_a_new_problem():
    assert OpenShortage("v1", "out_of_stock", "t", "d").key != OpenShortage("v1", "cover_below_lead_time", "t", "d").key
    assert alerts.KINDS == ("out_of_stock", "cover_below_lead_time", "stockout_projected")
