"""The order state machine as a table, with no database: the table against the rows of the migration, and what `decide` does with every pair of statuses, written out independently."""
from __future__ import annotations

import itertools
import random
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.fulfillment import states  # noqa: E402


def migration_moves() -> set[tuple[str, str]]:
    sql = (ROOT / "migrations" / "024_fulfillment.sql").read_text(encoding="utf-8")
    block = sql[sql.index("insert into fulfillment_transition"):sql.index("on conflict do nothing")]
    return set(re.findall(r"\('(\w+)', '(\w+)'\)", block))


def test_the_code_table_and_the_migration_table_are_the_same_set_of_moves():
    code = {(a, b) for a, to in states.TRANSITIONS.items() for b in to}
    assert code == migration_moves() and len(code) == 9


def test_every_status_is_reachable_from_created_and_the_three_ends_have_no_way_out():
    reach, frontier = {"created"}, ["created"]
    while frontier:
        for nxt in states.TRANSITIONS.get(frontier.pop(), ()):
            if nxt not in reach:
                reach.add(nxt)
                frontier.append(nxt)
    assert reach == set(states.STATES)
    assert all(not states.TRANSITIONS.get(end) for end in states.TERMINAL)


# what decide must answer for each pair, written here by hand from the rules in states.py and not derived from its code
APPLY = {("created", "submitted"), ("created", "cancelled"), ("submitted", "accepted"), ("submitted", "rejected"), ("submitted", "cancelled"), ("accepted", "packed"),
         ("accepted", "cancelled"), ("packed", "shipped"), ("shipped", "delivered")}
LATE = {("accepted", "submitted"), ("packed", "submitted"), ("packed", "accepted"), ("shipped", "submitted"), ("shipped", "accepted"), ("shipped", "packed"),
        ("delivered", "submitted"), ("delivered", "accepted"), ("delivered", "packed"), ("delivered", "shipped"), ("rejected", "submitted"), ("rejected", "accepted"),
        ("cancelled", "submitted"), ("cancelled", "accepted")}


@pytest.mark.parametrize("current,target", list(itertools.product(states.STATES, states.PARTNER_STATUSES)))
def test_decide_for_every_pair_of_statuses(current, target):
    got = states.decide(current, target)
    if current == target:
        want = "ignore"                                  # a repeat of where the order already is
    elif (current, target) in APPLY:
        want = "apply"
    elif (current, target) in LATE:
        want = "ignore"                                  # older than where the order is, or a late early-stage event for an order that ended
    else:
        want = "refuse"                                  # a skip ahead, or a move the table does not have
    assert got == want, (current, target)


def test_a_status_the_partner_may_not_report_or_an_unknown_one_is_refused_never_applied():
    assert states.decide("created", "created") == "refuse"
    assert states.decide("submitted", "teleported") == "refuse"
    assert states.decide("teleported", "accepted") == "refuse"


def test_following_decide_over_random_event_streams_never_moves_an_order_backwards_or_off_the_table():
    """The property under the whole machine: whatever order, however many times, whatever the stream, an applied move is in the table, the road rank never falls, and an order that
    ended never leaves its end."""
    rank = {"created": 0, "submitted": 1, "accepted": 2, "packed": 3, "shipped": 4, "delivered": 5}
    for seed in range(300):
        rng = random.Random(seed)
        current, ended = "created", None
        for _ in range(rng.randrange(1, 40)):
            target = rng.choice(states.PARTNER_STATUSES)
            verdict = states.decide(current, target)
            if verdict == "apply":
                assert states.can_move(current, target)
                if current in rank and target in rank:
                    assert rank[target] > rank[current]
                current = target
            if current in states.TERMINAL:
                ended = ended or current
                assert current == ended
