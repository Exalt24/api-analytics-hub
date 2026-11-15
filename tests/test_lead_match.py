"""Lead matching: phone and email keys, the clusters of one person, the source of each person, and the missed call work queue.

The examples pin each rule once. The seeded worlds then plant a known set of people (each with several leads that share an email or a phone through a CHAIN, so the
truth is known), write every phone in a different spelling, add shared office lines and look-alike numbers that are NOT the same person, shuffle the input, and demand
the same answer every time. A guard removed from the code must turn this file red: tests/mutation_check_salesforce.py runs each one out.
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.lead_match import (AMBIGUOUS_NUMBER, CALL_BEFORE_LEAD, CALL_SOURCE_MISSING, EMAIL, EMAIL_AND_PHONE, LEAD_SOURCE, MISSED_CALL_NO_LEAD,  # noqa: E402
                            MISSED_CALL_NUMBER_UNUSABLE, MISSED_CALL_OPEN_LEAD, NO_SOURCE, PHONE, SURVIVOR, TRANSITIVE, UNKNOWN_SOURCE, Call, Lead,
                            attribute, dedupe, missed_call_queue, normalize_email, normalize_phone)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def at(hours=0):
    return T0 + timedelta(hours=hours)


def lead(n, hours=0, email=None, phones=(), source=None, converted=False):
    return Lead("L%03d" % n, at(hours), email, tuple(phones), source, converted)


def call(n, hours, caller, source=None, answered=True):
    return Call("C%03d" % n, at(hours), caller, source, answered)


# ---------------------------------------------------------------- the keys

@pytest.mark.parametrize("raw, country, expect", [
    ("+1 (212) 555-0100", "1", "12125550100"),
    ("212-555-0100", "1", "12125550100"),
    ("1-212-555-0100", "1", "12125550100"),
    ("(212) 555 0100 x42", "1", "12125550100"),
    ("212.555.0100 ext. 7", "1", "12125550100"),
    ("0917 123 4567", "63", "639171234567"),
    ("09171234567", "63", "639171234567"),
    ("+63 917 123 4567", "63", "639171234567"),
    ("0063 917 123 4567", "1", "639171234567"),
    ("+63 917 123 4567", "1", "639171234567"),
    ("917 123 4567", "63", "639171234567"),
])
def test_phone_numbers_become_one_key_however_they_are_written(raw, country, expect):
    assert normalize_phone(raw, country) == expect


@pytest.mark.parametrize("raw", [None, "", "n/a", "555", "123456", "+1 2", "+" + "9" * 16, "0" * 20])
def test_a_number_that_cannot_be_trusted_is_none_never_a_padded_guess(raw):
    assert normalize_phone(raw, "1") is None


@pytest.mark.parametrize("country", ["", "us", "+1", "1 "])
def test_the_default_country_must_be_a_calling_code(country):
    with pytest.raises(ValueError, match="default_country"):
        normalize_phone("2125550100", country)


@pytest.mark.parametrize("raw, expect", [(" A@X.COM ", "a@x.com"), ("a@x.com", "a@x.com"), ("no at sign", None), ("a@b", None), ("", None), (None, None), ("a b@x.com", None)])
def test_an_email_is_lower_cased_and_a_bad_one_is_none(raw, expect):
    assert normalize_email(raw) == expect


# ---------------------------------------------------------------- dedupe

def ids(clusters):
    return sorted(tuple(sorted(c.lead_ids)) for c in clusters)


def test_leads_with_the_same_email_in_any_case_are_one_person_and_the_oldest_survives():
    out = dedupe([lead(2, 5, "Pat@X.com"), lead(1, 9, "pat@x.com"), lead(3, 1, "other@x.com")], "1")
    assert ids(out) == [("L001", "L002"), ("L003",)]
    pat = next(c for c in out if "L001" in c.lead_ids)
    assert pat.survivor == "L002" and [(m.lead_id, m.reason) for m in pat.members] == [("L002", SURVIVOR), ("L001", EMAIL)]
    assert pat.members[1].shared == ("pat@x.com",)


def test_the_same_phone_in_two_spellings_is_one_person_and_a_tie_in_time_goes_to_the_lowest_id():
    out = dedupe([lead(2, 0, phones=["(212) 555-0100"]), lead(1, 0, phones=["+1 212 555 0100"])], "1")
    assert len(out) == 1 and out[0].survivor == "L001" and out[0].members[1] == type(out[0].members[1])("L002", PHONE, ("12125550100",))


def test_email_and_phone_together_and_a_link_through_a_third_lead_are_told_apart():
    out = dedupe([lead(1, 0, "a@x.com", ["212 555 0100"]), lead(2, 1, "a@x.com", ["212 555 0100"]),
                  lead(3, 2, "a@x.com", ["212 555 0199"]), lead(4, 3, None, ["212 555 0199"])], "1")
    assert len(out) == 1
    assert {m.lead_id: m.reason for m in out[0].members} == {"L001": SURVIVOR, "L002": EMAIL_AND_PHONE, "L003": EMAIL, "L004": TRANSITIVE}


def test_the_same_name_is_never_a_match_and_a_lead_with_nothing_to_match_stands_alone():
    out = dedupe([lead(1, 0, "a@x.com"), lead(2, 1, "b@x.com"), lead(3, 2), lead(4, 3, "not an email", ["n/a"])], "1")
    assert len(out) == 4


def test_a_phone_held_by_two_different_emails_is_a_shared_line_and_merges_nobody():
    office = ["+1 212 555 0100"]
    out = dedupe([lead(1, 0, "a@x.com", office), lead(2, 1, "b@x.com", office), lead(3, 2, None, office)], "1")
    assert ids(out) == [("L001",), ("L002",), ("L003",)], "the email-less lead is not assigned to either person either"


def test_a_phone_held_by_one_email_and_an_email_less_lead_is_one_person():
    out = dedupe([lead(1, 0, "a@x.com", ["212 555 0100"]), lead(2, 1, None, ["212 555 0100"])], "1")
    assert len(out) == 1


def test_duplicate_lead_ids_and_a_naive_time_are_refused():
    with pytest.raises(ValueError, match="unique"):
        dedupe([lead(1), lead(1)], "1")
    with pytest.raises(ValueError, match="time zone"):
        Lead("x", datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="time zone"):
        Call("x", datetime(2026, 1, 1), "1")


def test_an_empty_input_is_an_empty_answer():
    assert dedupe([], "1") == [] and attribute([], [], "1") == [] and missed_call_queue([], [], "1") == []


# ---------------------------------------------------------------- attribution

def test_a_call_inside_the_window_before_the_first_lead_is_the_first_touch_and_wins_over_the_leads_own_source():
    out = attribute([lead(1, 24, "a@x.com", ["212 555 0100"], source="Web")], [call(1, 20, "+1 212 555 0100", "Google Ads")], "1")
    assert len(out) == 1 and (out[0].source, out[0].reason, out[0].call_id, out[0].overrode_lead_source) == ("Google Ads", CALL_BEFORE_LEAD, "C001", True)


def test_the_earliest_call_in_the_window_is_the_first_touch_and_one_that_agrees_does_not_override():
    out = attribute([lead(1, 100, phones=["2125550100"], source="Google Ads")],
                    [call(2, 90, "212-555-0100", "Google Ads"), call(1, 50, "212-555-0100", "Facebook")], "1")
    assert (out[0].source, out[0].call_id) == ("Facebook", "C001")
    only = attribute([lead(1, 100, phones=["2125550100"], source="Google Ads")], [call(2, 90, "212-555-0100", "Google Ads")], "1")
    assert only[0].overrode_lead_source is False


def test_a_call_after_the_lead_or_before_the_window_never_attributes_it():
    window = timedelta(days=14)
    base = [lead(1, 24 * 20, phones=["2125550100"], source="Web")]
    after = attribute(base, [call(1, 24 * 20 + 1, "2125550100", "Google Ads")], "1", window=window)
    early = attribute(base, [call(1, 24 * 20 - 24 * 14 - 1, "2125550100", "Google Ads")], "1", window=window)
    edge = attribute(base, [call(1, 24 * 20 - 24 * 14, "2125550100", "Google Ads")], "1", window=window)
    assert after[0].reason == LEAD_SOURCE and early[0].reason == LEAD_SOURCE and after[0].source == "Web"
    assert edge[0].reason == CALL_BEFORE_LEAD, "exactly at the edge of the window counts"


def test_a_call_that_names_no_source_falls_back_to_the_lead_and_says_why():
    out = attribute([lead(1, 24, phones=["2125550100"], source="Web")], [call(1, 20, "2125550100", None)], "1")
    assert (out[0].source, out[0].reason, out[0].call_id) == ("Web", CALL_SOURCE_MISSING, "C001")
    none = attribute([lead(1, 24, phones=["2125550100"])], [call(1, 20, "2125550100", "  ")], "1")
    assert (none[0].source, none[0].reason) == (UNKNOWN_SOURCE, CALL_SOURCE_MISSING)


def test_no_call_and_no_source_is_unknown_with_its_own_reason():
    out = attribute([lead(1, 24, phones=["2125550100"])], [call(1, 20, "9995550100", "Google Ads")], "1")
    assert (out[0].source, out[0].reason) == (UNKNOWN_SOURCE, NO_SOURCE)


def test_a_call_from_a_shared_line_is_listed_but_never_attributes_either_person():
    office = ["+1 212 555 0100"]
    leads = [lead(1, 24, "a@x.com", office, source="Web"), lead(2, 30, "b@x.com", office)]
    out = {a.survivor: a for a in attribute(leads, [call(1, 20, "212 555 0100", "Google Ads")], "1")}
    assert out["L001"].reason == LEAD_SOURCE and out["L001"].source == "Web" and out["L001"].ambiguous_call_ids == ("C001",)
    assert out["L002"].reason == AMBIGUOUS_NUMBER and out["L002"].source == UNKNOWN_SOURCE and out["L002"].ambiguous_call_ids == ("C001",)


def test_every_lead_of_a_person_is_listed_on_their_one_attribution():
    leads = [lead(1, 24, "a@x.com", source="Web"), lead(2, 48, "a@x.com", source="Referral"), lead(3, 50, "b@x.com", source="Referral")]
    out = attribute(leads, [], "1")
    assert [(a.survivor, a.lead_ids, a.source) for a in out] == [("L001", ("L001", "L002"), "Web"), ("L003", ("L003",), "Referral")]


# ---------------------------------------------------------------- the missed call queue

def test_a_missed_call_is_queued_with_the_reason_until_someone_has_spoken_to_the_caller():
    leads = [lead(1, 0, phones=["2125550100"]), lead(2, 0, phones=["2125550111"], converted=True)]
    calls = [call(1, 10, "212 555 0100", answered=False),            # a known open lead
             call(2, 11, "999 555 0100", answered=False),            # nobody known
             call(3, 12, "n/a", answered=False),                     # a number nobody can use
             call(4, 13, "212 555 0111", answered=False),            # a converted lead: nothing to chase
             call(5, 14, "212 555 0100", answered=True),             # an answered call is not a task
             call(6, 15, "888 555 0100", answered=False), call(7, 16, "(888) 555-0100", answered=True)]   # called back later
    got = [(q.call_id, q.reason, q.lead_ids) for q in missed_call_queue(leads, calls, "1")]
    assert got == [("C002", MISSED_CALL_NO_LEAD, ()), ("C003", MISSED_CALL_NUMBER_UNUSABLE, ())]
    still_open = missed_call_queue(leads, [call(1, 10, "212 555 0100", answered=False)], "1")
    assert [(q.call_id, q.reason, q.lead_ids) for q in still_open] == [("C001", MISSED_CALL_OPEN_LEAD, ("L001",))]


def test_the_queue_is_oldest_first_whatever_the_input_order():
    calls = [call(2, 30, "9995550102", answered=False), call(1, 10, "9995550101", answered=False)]
    assert [q.call_id for q in missed_call_queue([], calls, "1")] == ["C001", "C002"]


# ---------------------------------------------------------------- seeded worlds with a planted truth

WORLD_SEEDS = range(150)
SPELLINGS = ["{a}-{b}-{c}", "({a}) {b}-{c}", "+1 {a} {b} {c}", "1-{a}-{b}-{c}", "{a}.{b}.{c}", "{a}{b}{c}", "({a}) {b} {c} x{e}", "+1{a}{b}{c}"]


def spell(rng, number):
    a, b, c = number[:3], number[3:6], number[6:]
    return rng.choice(SPELLINGS).format(a=a, b=b, c=c, e=rng.randrange(1, 99))


def world(seed):
    """People with known leads: each person's leads are chained by a shared email or phone, so the truth is the person. Different people share nothing but,
    sometimes, one office line that sits behind two different emails (never merged). Returns (leads, calls, truth: lead id -> person, shared lines, rng)."""
    rng = random.Random(seed)
    people = rng.randrange(1, 9)
    leads, truth, calls = [], {}, []
    office = "2129990000"
    n = 0
    owner_numbers = {}
    for person in range(people):
        email = "person%d@example.com" % person
        phone = "21255%05d" % (person * 7 + 1)
        extra = "21266%05d" % (person * 7 + 2)
        owner_numbers[person] = (phone, extra)
        k = rng.randrange(1, 5)
        for j in range(k):
            n += 1
            # lead j shares with lead j-1 either the email or a phone: the chain keeps the person connected
            use_email = j == 0 or rng.random() < 0.5
            shown_email = email if use_email else None
            numbers = [phone] if (not use_email or rng.random() < 0.5) else []
            if j and not use_email and not numbers:
                numbers = [phone]
            if rng.random() < 0.3:
                numbers.append(extra)
            if rng.random() < 0.3 and person < 2 and people >= 2:
                numbers.append(office)                                        # a line two different people both give
                shown_email = email
            email_text = shown_email.upper() if shown_email and rng.random() < 0.3 else shown_email
            leads.append(Lead("L%03d" % n, at(rng.randrange(0, 24 * 30)), email_text, tuple(spell(rng, x) for x in numbers),
                              rng.choice([None, "Web", "Referral", "Trade Show"]), rng.random() < 0.3))
            truth["L%03d" % n] = person
    for c in range(rng.randrange(0, 12)):
        who = rng.randrange(people)
        number = rng.choice([owner_numbers[who][0], office, "21200%05d" % rng.randrange(99999)])
        calls.append(Call("C%03d" % c, at(rng.randrange(0, 24 * 30)), spell(rng, number), rng.choice([None, "Google Ads", "Facebook", "Direct"]), rng.random() < 0.6))
    return leads, calls, truth, rng


@pytest.mark.parametrize("seed", WORLD_SEEDS)
def test_in_every_seeded_world_the_clusters_are_the_planted_people_and_the_order_never_matters(seed):
    leads, calls, truth, rng = world(seed)
    out = dedupe(leads, "1")
    seen = [l for c in out for l in c.lead_ids]
    assert sorted(seen) == sorted(l.id for l in leads), "every lead is in exactly one cluster"
    by_id = {l.id: l for l in leads}
    for cluster in out:
        persons = {truth[l] for l in cluster.lead_ids}
        assert len(persons) == 1, (seed, "two people were merged", cluster)
        oldest = min((by_id[l].created_at, l) for l in cluster.lead_ids)
        assert cluster.survivor == oldest[1] and cluster.members[0].reason == SURVIVOR
    shuffled = list(leads)
    rng.shuffle(shuffled)
    assert dedupe(shuffled, "1") == out, (seed, "input order changed the clusters")
    shuffled_calls = list(calls)
    rng.shuffle(shuffled_calls)
    assert attribute(shuffled, shuffled_calls, "1") == attribute(leads, calls, "1")
    assert missed_call_queue(shuffled, shuffled_calls, "1") == missed_call_queue(leads, calls, "1")


@pytest.mark.parametrize("seed", WORLD_SEEDS)
def test_in_every_seeded_world_each_attribution_follows_its_reason_code(seed):
    leads, calls, truth, _ = world(seed)
    out = attribute(leads, calls, "1")
    clusters = {c.survivor: c for c in dedupe(leads, "1")}
    by_id = {l.id: l for l in leads}
    calls_by_id = {c.id: c for c in calls}
    assert sorted(a.survivor for a in out) == sorted(clusters), "one answer per person"
    for a in out:
        first = by_id[a.survivor].created_at
        own = (by_id[a.survivor].source or "").strip() or None
        assert a.lead_ids == clusters[a.survivor].lead_ids
        if a.reason == CALL_BEFORE_LEAD:
            c = calls_by_id[a.call_id]
            assert first - timedelta(days=14) <= c.started_at <= first and a.source == c.source
            assert a.overrode_lead_source == bool(own and own != c.source)
            phones = {normalize_phone(p, "1") for l in a.lead_ids for p in by_id[l].phones}
            assert normalize_phone(c.caller, "1") in phones
            assert a.call_id not in a.ambiguous_call_ids
        elif a.reason == LEAD_SOURCE:
            assert a.source == own and a.call_id is None
        elif a.reason == CALL_SOURCE_MISSING:
            assert not (calls_by_id[a.call_id].source or "").strip() and a.source == (own or UNKNOWN_SOURCE)
        else:
            assert a.reason in (NO_SOURCE, AMBIGUOUS_NUMBER) and a.source == UNKNOWN_SOURCE and own is None
        # the one rule that matters most: a call from a number two different people hold is never the proof for either
        for call_id in a.ambiguous_call_ids:
            assert a.call_id != call_id
            number = normalize_phone(calls_by_id[call_id].caller, "1")
            holders = {c.survivor for c in clusters.values() for l in c.lead_ids for p in by_id[l].phones if normalize_phone(p, "1") == number}
            assert len(holders) >= 2


@pytest.mark.parametrize("seed", range(60))
def test_in_every_seeded_world_the_missed_call_queue_holds_exactly_the_unanswered_calls_nobody_has_followed_up(seed):
    leads, calls, truth, _ = world(seed)
    queue = {q.call_id: q for q in missed_call_queue(leads, calls, "1")}
    by_id = {l.id: l for l in leads}
    clusters = dedupe(leads, "1")                       # a person is open while ANY of their leads is open
    for c in calls:
        number = normalize_phone(c.caller, "1")
        followed_up = any(o.answered and o.started_at > c.started_at and normalize_phone(o.caller, "1") == number for o in calls)
        holders = [l for k in clusters for l in k.lead_ids if number and any(number in {normalize_phone(p, "1") for p in by_id[m].phones} for m in k.lead_ids)]
        if c.answered or followed_up:
            assert c.id not in queue, (seed, c)
        elif number is None:
            assert queue[c.id].reason == MISSED_CALL_NUMBER_UNUSABLE
        elif not holders:
            assert queue[c.id].reason == MISSED_CALL_NO_LEAD
        elif all(by_id[h].converted for h in holders):
            assert c.id not in queue
        else:
            assert queue[c.id].reason == MISSED_CALL_OPEN_LEAD and set(holders) <= set(queue[c.id].lead_ids)
    assert [q.call_id for q in queue.values()] == [c.id for c in sorted(calls, key=lambda c: (c.started_at, c.id)) if c.id in queue]
