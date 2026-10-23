"""The alert policy as a simulation: many seeded worlds, many steps, delivery that sometimes fails.

The rules, stated as invariants over a whole run, not as examples:
  R1 an unchanged set of problems never alerts twice inside the repeat window
  R2 a set that gains or loses a member alerts at once (nothing real is merged with something else or silenced)
  R3 a set that clears sends exactly one recovery message, and a healthy run after that sends none
  R4 an alert whose delivery failed comes back on the next run (the caller keeps the OLD state when delivery fails)
  R6 an unchanged set that is still unresolved after the repeat window alerts again (a problem is never silenced for good)
  R5 a message names only problems that exist at that step, and every current problem appears in an alert message
The world varies all of these together: connections entering and leaving, a connection flipping between two problem kinds (same connection, different key), two connections
sharing a platform, the clock jumping by seconds or by hours, and delivery failing.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.health import Problem, decide  # noqa: E402

REPEAT = 6 * 3600
KINDS = ("expired_lease", "stale", "never_succeeded")


def _world(rng: random.Random, steps: int):
    conns = ["c%02d" % i for i in range(rng.randint(1, 6))]
    problems: dict[str, str] = {}
    now = 1_000_000.0
    for _ in range(steps):
        for c in conns:
            roll = rng.random()
            if roll < 0.15:
                problems[c] = rng.choice(KINDS)           # enters, or flips kind when already present
            elif roll < 0.30:
                problems.pop(c, None)                     # recovers
        now += rng.choice((1, 30, 900, 900, 3600, REPEAT, REPEAT + 1))
        yield now, [Problem(c, "shopify" if int(c[1:]) % 2 else "ga4", k, rng.randint(60, 90000)) for c, k in sorted(problems.items())], rng.random() < 0.25


def _simulate(seed: int, steps: int = 60, decide_fn=decide, ignore_failure=False):
    rng = random.Random(seed)
    state: dict = {}
    sent_at = None
    last_alert_keys: list[str] | None = None
    for now, problems, delivery_fails in _world(rng, steps):
        keys = sorted(p.key for p in problems)
        before = dict(state)
        message, new_state = decide_fn(state, problems, now, REPEAT)
        pending_keys = sorted(before.get("keys", []))
        if message is not None:
            if problems:
                for p in problems:
                    assert p.connection_id[:8] in message, "R5: current problem %s missing from the alert" % p.key
                shown = {line.split()[2] for line in message.splitlines()[1:]}
                assert shown == {p.connection_id[:8] for p in problems}, "R5: the message names a connection that has no problem now"
            else:
                assert message.startswith("Recovered") and pending_keys, "R3: a recovery with nothing to recover from"
        else:
            if keys and keys == pending_keys and now - before.get("alerted_at", 0.0) >= REPEAT:
                raise AssertionError("R6: an unchanged problem set stayed silent past the repeat window")
            if keys and keys != pending_keys:
                raise AssertionError("R2: the set changed (%s -> %s) and nothing alerted" % (pending_keys, keys))
            if not keys and pending_keys:
                raise AssertionError("R3: the set cleared and no recovery was sent")
        if message is not None and problems and keys == pending_keys:
            assert now - before.get("alerted_at", 0.0) >= REPEAT, "R1: the same set alerted again inside the window"
        if delivery_fails and message is not None and not ignore_failure:
            continue                                      # the runner keeps the OLD state: R4 then requires the next step to alert again for the same set
        state = new_state
    return True


def test_the_invariants_hold_over_many_seeded_worlds():
    for seed in range(400):
        _simulate(seed)


def test_a_failed_delivery_alerts_again_on_the_next_run():
    ps = [Problem("c01", "shopify", "stale", 9000)]
    msg, new_state = decide({}, ps, 100.0, REPEAT)
    assert msg and "c01" in msg
    again, _ = decide({}, ps, 160.0, REPEAT)                # old state kept because the send failed
    assert again == msg
    quiet, _ = decide(new_state, ps, 160.0, REPEAT)         # state saved because the send worked
    assert quiet is None


def test_the_same_connection_changing_kind_is_a_new_problem_not_a_repeat():
    a, st = decide({}, [Problem("c01", "shopify", "stale", 9000)], 100.0, REPEAT)
    b, _ = decide(st, [Problem("c01", "shopify", "expired_lease", 400)], 160.0, REPEAT)
    assert a and b and "expired_lease" in b


def test_two_connections_on_one_platform_are_not_merged():
    one = [Problem("c01", "shopify", "stale", 9000)]
    two = one + [Problem("c02", "shopify", "stale", 9100)]
    _, st = decide({}, one, 100.0, REPEAT)
    msg, _ = decide(st, two, 160.0, REPEAT)
    assert msg and "c01" in msg and "c02" in msg


def test_recovery_is_sent_once():
    _, st = decide({}, [Problem("c01", "shopify", "stale", 9000)], 100.0, REPEAT)
    rec, st2 = decide(st, [], 200.0, REPEAT)
    assert rec and rec.startswith("Recovered")
    none, _ = decide(st2, [], 300.0, REPEAT)
    assert none is None


def test_the_simulation_can_fail_a_broken_policy():
    """Mutation controls: each broken policy must turn the simulation red, so a passing run means something."""
    def never_repeats(state, problems, now, repeat):
        msg, st = decide(state, problems, now, 10 ** 12)
        return msg, st

    def always_repeats(state, problems, now, repeat):
        msg, st = decide(state, problems, now, 0)
        return msg, st

    def silent_on_recovery(state, problems, now, repeat):
        msg, st = decide(state, problems, now, repeat)
        return (None if not problems else msg), st

    def ignores_kind(state, problems, now, repeat):
        flat = [Problem(p.connection_id, p.platform, "stale", p.age_seconds) for p in problems]
        msg, st = decide(state, flat, now, repeat)
        return msg, st

    failures = 0
    for name, broken in (("never repeats", never_repeats), ("always repeats", always_repeats), ("silent on recovery", silent_on_recovery), ("ignores kind", ignores_kind)):
        red = False
        for seed in range(200):
            try:
                _simulate(seed, decide_fn=broken)
            except AssertionError:
                red = True
                break
        assert red, "the simulation did not catch the mutant: %s" % name
        failures += 1
    assert failures == 4
