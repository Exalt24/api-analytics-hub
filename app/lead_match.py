"""Lead matching: which leads are one person, which call and source brought them in, and which missed calls need a person.

Salesforce keeps a Lead per form fill or call, so one person usually has several, and the leads never say which advertisement or call
produced them. This module answers three questions from plain records, with a REASON CODE on every answer so a staff member (or a test) can see why:

    dedupe       leads that share a normalised email, or a phone number held by one email at most, are one person. Never name alone. A phone held by
                 leads of two different emails is a shared line and merges nobody. Nothing is written back: the clusters are a report for the
                 Salesforce administrator, who owns the merge.
    attribute    each person's source. A call from the person's own number inside the window BEFORE their first lead is the first touch and
                 wins; otherwise the lead's own source; otherwise "unknown". A number that belongs to more than one person (a shared office
                 line) is never used to attribute anyone.
    work queue   a missed call becomes a staff task unless the caller has since been spoken to, with the reason it is on the list.

PHONE NUMBERS. No library and no guessing: `+` or `00` mean the number already carries its country code; anything else is national and takes the
`default_country` the caller names (a leading trunk `0` is dropped except for country 1). A result of fewer than 8 or more than 15 digits is unusable
(E.164's own ceiling) and is None, never a padded guess, because a wrong match merges two strangers. Extensions ("x123") are cut off. A bare 10 digit
number under country 1 and the same number written with its `+1` are therefore one key.

WHAT THIS DOES NOT DO. It does not fuzzy match names or addresses (parcelkey is the repository for address matching), it does not read Salesforce's own
duplicate rules, and it cannot tell two people on one shared mailbox from one person: a shared email merges, which is the stated limit. A shared
PHONE LINE is told apart by the emails behind it, and a call from it is AMBIGUOUS_NUMBER and never attributes anyone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Sequence

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_DIGITS, MAX_DIGITS = 8, 15
_EXT_RE = re.compile(r"(?:ext\.?|x|#)\s*\d+\s*$", re.IGNORECASE)

# reason codes, one per answer
SURVIVOR = "SURVIVOR"
EMAIL = "EMAIL"
PHONE = "PHONE"
EMAIL_AND_PHONE = "EMAIL_AND_PHONE"
TRANSITIVE = "TRANSITIVE"
CALL_BEFORE_LEAD = "CALL_BEFORE_LEAD"
LEAD_SOURCE = "LEAD_SOURCE"
CALL_SOURCE_MISSING = "CALL_SOURCE_MISSING"
AMBIGUOUS_NUMBER = "AMBIGUOUS_NUMBER"
NO_SOURCE = "NO_SOURCE"
MISSED_CALL_NO_LEAD = "MISSED_CALL_NO_LEAD"
MISSED_CALL_OPEN_LEAD = "MISSED_CALL_OPEN_LEAD"
MISSED_CALL_NUMBER_UNUSABLE = "MISSED_CALL_NUMBER_UNUSABLE"
UNKNOWN_SOURCE = "unknown"


@dataclass(frozen=True)
class Lead:
    id: str
    created_at: datetime                      # timezone aware
    email: str | None = None
    phones: tuple[str, ...] = ()
    source: str | None = None                 # Salesforce LeadSource
    converted: bool = False
    url: str | None = None                    # the link to the Salesforce record itself, so an answer can be drilled into

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None:
            raise ValueError("lead %s: created_at must carry a time zone" % self.id)


@dataclass(frozen=True)
class Call:
    id: str
    started_at: datetime                      # timezone aware
    caller: str | None
    source: str | None = None                 # the call tracking source label
    answered: bool = True

    def __post_init__(self) -> None:
        if self.started_at.tzinfo is None:
            raise ValueError("call %s: started_at must carry a time zone" % self.id)


@dataclass(frozen=True)
class Member:
    lead_id: str
    reason: str                               # SURVIVOR, EMAIL, PHONE, EMAIL_AND_PHONE or TRANSITIVE
    shared: tuple[str, ...] = ()              # the normalised keys it shares with the survivor


@dataclass(frozen=True)
class Cluster:
    survivor: str
    members: tuple[Member, ...]               # the survivor first, then by created_at and id

    @property
    def lead_ids(self) -> tuple[str, ...]:
        return tuple(m.lead_id for m in self.members)


@dataclass(frozen=True)
class Attribution:
    survivor: str
    lead_ids: tuple[str, ...]
    source: str
    reason: str
    call_id: str | None = None
    #: True when a call's source overrode a different source the lead itself carried.
    overrode_lead_source: bool = False
    #: Calls from a number shared with another person: seen, but never used to attribute anyone.
    ambiguous_call_ids: tuple[str, ...] = ()
    #: The record links of the person's leads, in lead_ids order, for the leads that carry one.
    links: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueueItem:
    call_id: str
    reason: str
    lead_ids: tuple[str, ...] = ()
    links: tuple[str, ...] = ()


def normalize_email(raw: str | None) -> str | None:
    text = (raw or "").strip().lower()
    return text if EMAIL_RE.match(text) else None


def normalize_phone(raw: str | None, default_country: str) -> str | None:
    """Digits with the country code first, or None when the number cannot be trusted. See the module note."""
    if not default_country or not default_country.isdigit():
        raise ValueError("default_country must be a country calling code such as 1 or 63")
    text = _EXT_RE.sub("", (raw or "").strip())
    international = text.startswith("+") or text.startswith("00")
    digits = re.sub(r"\D", "", text)
    if text.startswith("00"):
        digits = digits[2:]
    if not international:
        if default_country != "1" and digits.startswith("0"):
            digits = digits[1:]
        if not (default_country == "1" and len(digits) == 11 and digits.startswith("1")):
            digits = default_country + digits
    return digits if MIN_DIGITS <= len(digits) <= MAX_DIGITS else None


def _keys(lead: Lead, default_country: str) -> tuple[str | None, frozenset[str]]:
    phones = {p for p in (normalize_phone(raw, default_country) for raw in lead.phones) if p}
    return normalize_email(lead.email), frozenset(phones)


class _Sets:
    def __init__(self, items: Iterable[str]):
        self.parent = {i: i for i in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _group(leads: Sequence[Lead], keys: dict[str, tuple[str | None, frozenset[str]]]) -> tuple[_Sets, frozenset[str]]:
    """Union leads by shared email first; then by shared phone, except a phone held by leads of two or more different emails.

    Such a phone is a SHARED LINE (an office, a family phone): it merges nobody and is returned so no answer uses it as proof of one person. The rule reads
    sets only, so the order of the leads cannot change it.
    """
    ids = [lead.id for lead in leads]
    sets = _Sets(ids)
    by_email: dict[str, str] = {}
    for lead_id in sorted(ids):
        email = keys[lead_id][0]
        if email:
            if email in by_email:
                sets.union(by_email[email], lead_id)
            else:
                by_email[email] = lead_id
    holders: dict[str, list[str]] = {}
    for lead_id in sorted(ids):
        for phone in sorted(keys[lead_id][1]):
            holders.setdefault(phone, []).append(lead_id)
    shared = set()
    for phone, held in holders.items():
        people = {sets.find(l) for l in held if keys[l][0]}
        if len(people) >= 2:
            shared.add(phone)
            continue
        for other in held[1:]:
            sets.union(held[0], other)
    return sets, frozenset(shared)


def dedupe(leads: Sequence[Lead], default_country: str) -> list[Cluster]:
    """One Cluster per person, the survivor the earliest created lead (the lowest id on a tie). Input order never changes the answer."""
    ids = [lead.id for lead in leads]
    if len(set(ids)) != len(ids):
        raise ValueError("lead ids must be unique")
    keys = {lead.id: _keys(lead, default_country) for lead in leads}
    sets, shared_lines = _group(leads, keys)
    groups: dict[str, list[Lead]] = {}
    for lead in leads:
        groups.setdefault(sets.find(lead.id), []).append(lead)
    clusters = []
    for group in groups.values():
        group.sort(key=lambda l: (l.created_at, l.id))
        survivor = group[0]
        s_email, s_phones = keys[survivor.id]
        members = [Member(survivor.id, SURVIVOR)]
        for lead in group[1:]:
            email, phones = keys[lead.id]
            same_email = bool(email) and email == s_email
            shared_phones = sorted((phones & s_phones) - shared_lines)
            shared = ([email] if same_email else []) + shared_phones
            reason = EMAIL_AND_PHONE if same_email and shared_phones else EMAIL if same_email else PHONE if shared_phones else TRANSITIVE
            members.append(Member(lead.id, reason, tuple(shared)))
        clusters.append(Cluster(survivor.id, tuple(members)))
    clusters.sort(key=lambda c: c.survivor)
    return clusters


def _phone_owners(clusters: Sequence[Cluster], leads: Sequence[Lead], default_country: str) -> dict[str, set[str]]:
    by_id = {lead.id: lead for lead in leads}
    owners: dict[str, set[str]] = {}
    for cluster in clusters:
        for lead_id in cluster.lead_ids:
            for phone in _keys(by_id[lead_id], default_country)[1]:
                owners.setdefault(phone, set()).add(cluster.survivor)
    return owners


def attribute(leads: Sequence[Lead], calls: Sequence[Call], default_country: str, *, window: timedelta = timedelta(days=14)) -> list[Attribution]:
    """The source of each person, with the reason. See the module note for the rule."""
    clusters = dedupe(leads, default_country)
    by_id = {lead.id: lead for lead in leads}
    owners = _phone_owners(clusters, leads, default_country)
    out = []
    for cluster in clusters:
        survivor = by_id[cluster.survivor]
        first = survivor.created_at
        phones = {p for lead_id in cluster.lead_ids for p in _keys(by_id[lead_id], default_country)[1]}
        usable, ambiguous = [], []
        for call in sorted(calls, key=lambda c: (c.started_at, c.id)):
            number = normalize_phone(call.caller, default_country)
            if number is None or number not in phones:
                continue
            if len(owners.get(number, ())) > 1:
                ambiguous.append(call.id)
            elif first - window <= call.started_at <= first:
                usable.append(call)
        own = (survivor.source or "").strip() or None
        links = tuple(by_id[l].url for l in cluster.lead_ids if by_id[l].url)
        if usable:
            call = usable[0]                                   # the earliest call in the window is the first touch
            label = (call.source or "").strip() or None
            if label:
                out.append(Attribution(cluster.survivor, cluster.lead_ids, label, CALL_BEFORE_LEAD, call.id,
                                       overrode_lead_source=bool(own and own != label), ambiguous_call_ids=tuple(ambiguous), links=links))
                continue
            out.append(Attribution(cluster.survivor, cluster.lead_ids, own or UNKNOWN_SOURCE, CALL_SOURCE_MISSING, call.id,
                                   ambiguous_call_ids=tuple(ambiguous), links=links))
            continue
        if own:
            out.append(Attribution(cluster.survivor, cluster.lead_ids, own, LEAD_SOURCE, None, ambiguous_call_ids=tuple(ambiguous), links=links))
        else:
            reason = AMBIGUOUS_NUMBER if ambiguous else NO_SOURCE
            out.append(Attribution(cluster.survivor, cluster.lead_ids, UNKNOWN_SOURCE, reason, None, ambiguous_call_ids=tuple(ambiguous), links=links))
    return out


def missed_call_queue(leads: Sequence[Lead], calls: Sequence[Call], default_country: str) -> list[QueueItem]:
    """Missed calls nobody has answered since, oldest first. An answered call from the same number after a missed one closes it."""
    clusters = dedupe(leads, default_country)
    by_id = {lead.id: lead for lead in leads}
    owners = _phone_owners(clusters, leads, default_country)
    members = {c.survivor: c.lead_ids for c in clusters}
    queue = []
    for call in sorted(calls, key=lambda c: (c.started_at, c.id)):
        if call.answered:
            continue
        number = normalize_phone(call.caller, default_country)
        if number is None:
            queue.append(QueueItem(call.id, MISSED_CALL_NUMBER_UNUSABLE))
            continue
        if any(other.answered and other.started_at > call.started_at and normalize_phone(other.caller, default_country) == number for other in calls):
            continue
        people = sorted(owners.get(number, ()))
        lead_ids = tuple(l for person in people for l in members[person])
        if not lead_ids:
            queue.append(QueueItem(call.id, MISSED_CALL_NO_LEAD))
        elif not all(by_id[l].converted for l in lead_ids):
            queue.append(QueueItem(call.id, MISSED_CALL_OPEN_LEAD, lead_ids, tuple(by_id[l].url for l in lead_ids if by_id[l].url)))
    return queue
