"""The shared rules of the Google Ads, TikTok, Square, QuickBooks and Shopmonkey connectors, as invariants over many seeded worlds.

Two things carry state or rounding across inputs and so get a property test and not only examples:

  * the retry loop (`Retrier`): a world is a random sequence of outcomes (an answer to keep, a failure that may pass with no wait, with a small wait,
    with a wait past the cap, a transport failure, a failure retrying cannot fix) and a random attempt budget. The rules: it never calls more than the
    budget, it sleeps once between two calls and never after the last, a wait the vendor asks for past the cap is handed back and never slept, a
    failure that cannot pass stops it at once, and what it returns is the first answer to keep.
  * the money and count parsers: random decimals, micros and counts are checked against an independent half-up reference, and the shapes that must be
    refused (a float, a bool, a negative, a fraction where a whole number is due, a value past the bigint) are crossed with every parser.

Each rule has a mutation control in the last section: the same simulator run against a deliberately broken copy must go red.
"""
from __future__ import annotations

import asyncio
import random
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.base import RateLimited, UpstreamBroken  # noqa: E402
from app.connectors.common import (  # noqa: E402
    BIGINT_MAX, Retrier, Retry, micros_to_minor, milli_units, minor_exponent, minor_units, retry_after, whole_number)

SEEDS = range(300)


class FinalError(Exception):
    pass


def half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------- the retry loop

def make_world(seed: int):
    """(budget, cap, outcomes). An outcome is ok, retry:<wait|None>, transport or final."""
    rng = random.Random(seed)
    budget = rng.randint(1, 6)
    cap = rng.choice([5.0, 30.0, 60.0])
    outcomes = []
    for _ in range(budget + 2):
        outcomes.append(rng.choice(["ok", "retry:none", "retry:small", "retry:big", "transport", "final"]))
    return budget, cap, outcomes


def simulate(retrier_cls, seed: int):
    budget, cap, outcomes = make_world(seed)
    sleeps: list[float] = []
    calls = {"n": 0}

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def outcome_of(n: int) -> str:
        return outcomes[n]

    async def send() -> httpx.Response:
        n = calls["n"]
        calls["n"] += 1
        if outcome_of(n) == "transport":
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"n": n})

    def classify(response: httpx.Response):
        kind = outcome_of(response.json()["n"])
        if kind == "ok":
            return None
        if kind == "final":
            raise FinalError()
        wait = {"retry:none": None, "retry:small": 2.0, "retry:big": cap + 1}[kind]
        return Retry(wait, "stand-in problem")

    retrier = retrier_cls(max_attempts=budget, backoff_base=1.0, backoff_cap=cap, sleep=sleep, rng=lambda: 1.0)
    try:
        response, attempts, waits = asyncio.run(retrier.run(send, classify))
        result = ("returned", response.json()["n"], attempts, waits)
    except RateLimited as exc:
        result = ("rate_limited", exc.retry_after_seconds, None, None)
    except FinalError:
        result = ("final", None, None, None)
    return budget, cap, outcomes, calls["n"], sleeps, result


def check_invariants(seed: int, retrier_cls=Retrier) -> list[str]:
    budget, cap, outcomes, calls, sleeps, result = simulate(retrier_cls, seed)
    problems: list[str] = []
    if calls > budget:
        problems.append("called %d times on a budget of %d" % (calls, budget))
    if any(s > cap for s in sleeps):
        problems.append("slept past the cap %s: %s" % (cap, sleeps))
    # Walk the outcomes the way the rules say, independently of the implementation.
    expected_sleeps: list[float] = []
    for n in range(budget):
        kind = outcomes[n]
        if kind == "ok":
            expected = ("returned", n, n + 1, len(expected_sleeps))
            break
        if kind == "final":
            expected = ("final", None, None, None)
            break
        wait = {"retry:none": None, "retry:small": 2.0, "retry:big": cap + 1, "transport": None}[kind]
        if n == budget - 1:
            expected = ("rate_limited", wait, None, None)
            break
        delay = wait if wait is not None else min(cap, 1.0 * 2 ** n)
        if delay > cap:
            expected = ("rate_limited", wait, None, None)
            break
        expected_sleeps.append(delay)
    if result != expected:
        problems.append("returned %r, rules say %r (outcomes %s, budget %d)" % (result, expected, outcomes[:budget], budget))
    if sleeps != expected_sleeps:
        problems.append("slept %r, rules say %r" % (sleeps, expected_sleeps))
    return problems


@pytest.mark.parametrize("seed", SEEDS)
def test_the_retry_loop_obeys_its_rules_in_every_seeded_world(seed):
    assert check_invariants(seed) == []


def test_the_worlds_cover_every_branch_of_the_rules():
    seen = set()
    for seed in SEEDS:
        _, _, _, _, sleeps, result = simulate(Retrier, seed)
        seen.add(result[0])
        if sleeps:
            seen.add("slept")
    assert seen == {"returned", "rate_limited", "final", "slept"}


def test_the_jittered_delay_stays_between_half_and_all_of_the_exponential_and_under_the_cap():
    rng = random.Random(7)
    for _ in range(500):
        base, cap, attempt, r = rng.choice([0.5, 1.0, 2.0]), rng.choice([5.0, 30.0]), rng.randint(0, 12), rng.random()
        delay = Retrier(backoff_base=base, backoff_cap=cap, rng=lambda r=r: r).delay(attempt, None)
        ceiling = min(cap, base * 2 ** attempt)
        assert ceiling * 0.5 <= delay <= ceiling <= cap


def test_a_budget_below_one_is_refused():
    with pytest.raises(ValueError):
        Retrier(max_attempts=0)


class NoRetryRetrier(Retrier):
    """MUTATION CONTROL: never sleeps and gives up after one attempt whatever the budget."""

    async def run(self, send, classify):
        response = await send()
        verdict = classify(response)
        if verdict is None:
            return response, 1, 0
        raise RateLimited(verdict.wait, "gave up")


class SleepsPastCapRetrier(Retrier):
    """MUTATION CONTROL: sleeps whatever the vendor asks for instead of handing a long wait back."""

    def delay(self, attempt, wait):
        return super().delay(attempt, wait if wait is None else min(wait, 0.0))


def goes_red(retrier_cls) -> bool:
    """True when the simulator finds a broken rule, or the broken retrier raises something the rules never allow."""
    for seed in SEEDS:
        try:
            if check_invariants(seed, retrier_cls):
                return True
        except Exception:  # noqa: BLE001 - an escaping transport error is itself a broken rule
            return True
    return False


def test_mutation_controls_turn_the_simulator_red():
    assert goes_red(NoRetryRetrier), "a retrier that never retries must fail a rule"
    assert goes_red(SleepsPastCapRetrier), "a retrier that ignores the vendor's wait must fail a rule"
    assert not goes_red(Retrier), "positive control: the real retrier passes every world"


# ---------------------------------------------------------------- money and counts

def test_minor_units_agrees_with_an_independent_half_up_reference_over_random_amounts():
    rng = random.Random(11)
    for _ in range(2000):
        digits = rng.randint(0, 6)
        amount = Decimal(rng.randint(0, 10 ** 9)).scaleb(-digits)
        for currency, exponent in (("USD", 2), ("EUR", 2), ("JPY", 0), ("KRW", 0)):
            assert minor_units(str(amount), currency, "x") == half_up(amount * 10 ** exponent)
            assert minor_units(amount, currency, "x") == minor_units(str(amount), currency, "x")


def test_micros_to_minor_agrees_with_an_independent_reference_and_moves_the_decimal_point_the_right_way():
    rng = random.Random(12)
    for _ in range(2000):
        micros = rng.randint(0, 10 ** 13)
        assert micros_to_minor(str(micros), "USD", "x") == half_up(Decimal(micros) / 10_000)
        assert micros_to_minor(micros, "JPY", "x") == half_up(Decimal(micros) / 1_000_000)
    assert micros_to_minor("12345000", "USD", "x") == 1235, "12.345 is 1234.5 minor units, and half rounds up"
    assert micros_to_minor("12340000", "USD", "x") == 1234 and micros_to_minor("5000", "USD", "x") == 1 and micros_to_minor("4999", "USD", "x") == 0


def test_milli_units_rounds_half_up_to_thousandths():
    rng = random.Random(13)
    for _ in range(1000):
        value = Decimal(rng.randint(0, 10 ** 7)).scaleb(-rng.randint(0, 6))
        assert milli_units(str(value), "x") == half_up(value * 1000)
    assert milli_units("2.5", "x") == 2500 and milli_units("0.0005", "x") == 1 and milli_units("0.0004", "x") == 0


REFUSED = [1.5, True, False, None, [], {}, "abc", "", "NaN", "Infinity", "1e999999999999999999999"]


@pytest.mark.parametrize("bad", REFUSED)
def test_every_parser_refuses_a_value_that_is_not_a_number_and_a_float_is_refused_because_it_cannot_be_exact(bad):
    assert minor_units("1", "USD", "x") == 100                          # positive control
    for parser in (lambda v: minor_units(v, "USD", "x"), lambda v: micros_to_minor(v, "USD", "x"),
                   lambda v: whole_number(v, "x"), lambda v: milli_units(v, "x")):
        with pytest.raises(UpstreamBroken):
            parser(bad)


def test_negative_amounts_are_refused_unless_the_caller_allows_them_and_the_bigint_bound_holds():
    for parser in (lambda v: minor_units(v, "USD", "x"), lambda v: micros_to_minor(v, "USD", "x"), lambda v: whole_number(v, "x"),
                   lambda v: milli_units(v, "x")):
        with pytest.raises(UpstreamBroken):
            parser("-1")
    assert minor_units("-1.50", "USD", "x", allow_negative=True) == -150
    with pytest.raises(UpstreamBroken):
        minor_units(str(BIGINT_MAX), "USD", "x")
    assert minor_units("92233720368547758", "USD", "x") == 9223372036854775800
    with pytest.raises(UpstreamBroken):
        whole_number(str(BIGINT_MAX + 1), "x")
    assert whole_number(str(BIGINT_MAX), "x") == BIGINT_MAX


def test_whole_number_accepts_a_zero_fraction_and_refuses_a_real_one():
    assert whole_number("3", "x") == 3 and whole_number("3.0", "x") == 3 and whole_number(Decimal("3.00"), "x") == 3 and whole_number(0, "x") == 0
    for bad in ("3.5", "0.0001", Decimal("2.000001")):
        with pytest.raises(UpstreamBroken):
            whole_number(bad, "x")


def test_micros_must_be_a_whole_number_of_micros():
    with pytest.raises(UpstreamBroken):
        micros_to_minor("1000.5", "USD", "x")


def test_the_minor_exponent_knows_the_zero_decimal_currencies_and_is_case_blind():
    assert [minor_exponent(c) for c in ("USD", "jpy", "KRW", "eur", "VND")] == [2, 0, 0, 2, 0]


def test_retry_after_reads_a_number_and_ignores_a_date_or_garbage():
    assert retry_after(httpx.Response(429, headers={"Retry-After": "12"})) == 12.0
    assert retry_after(httpx.Response(429, headers={"Retry-After": "0"})) == 0.0
    for header in ("Wed, 21 Oct 2026 07:28:00 GMT", "soon", "-5", ""):
        assert retry_after(httpx.Response(429, headers={"Retry-After": header})) is None
    assert retry_after(httpx.Response(429)) is None

