"""Reports over the ledger: a tenant statement, a daily revenue and usage report, and the reconciliation that proves the books agree. Markdown and CSV.

All of it is read from the entries, never from a stored total, so a report can disagree with a balance and show it:

  STATEMENT        the wallet's lines for a date range with an opening balance, a running balance and a closing balance. Opening plus the lines is the
                   closing balance by construction; `Statement.agrees_with(spendable)` checks the closing figure against the wallet when the range ends today.
  DAILY REPORT     for each UTC day: money in (top-ups, subscription credits), usage charged, refunds, service credits, adjustments, platform revenue,
                   failed payments, and the usage by event type.
  RECONCILIATION   `reconcile` runs billing_discrepancies() (stored balance against the sum of the entries, a transaction that does not sum to zero or
                   has fewer than two lines, a payment's credited and refunded totals against the ledger) and adds the checks a person would make by hand
                   (every currency's trial balance is zero; no wallet below zero). `assert_reconciled` raises ReconciliationFailed, so a script that must
                   not carry on over bad books fails loudly instead of printing a warning.

SIGNS. A statement shows money the way the customer sees it: a top-up is positive, a usage charge negative. The ledger's own signs (debit positive,
credit negative) are in ledger.py; a wallet is a liability, so its entries are the customer's amounts with the sign reversed.

CSV. A spreadsheet runs a cell that starts with = + - @ (or a tab or a carriage return) as a formula, and a reason, an actor or a provider id is text a
customer or a provider chose. `cell` puts an apostrophe in front of such a text cell. Amounts are written from integers by this module and are not run
through it, so a negative amount stays a number.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

import asyncpg

from app.price_watch.money import format_minor

from .errors import ReconciliationFailed

_UTC = timezone.utc


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """[start 00:00 UTC, the day after end 00:00 UTC), so `end` is included whole."""
    if end < start:
        raise ValueError("the range ends before it starts")
    return datetime.combine(start, time.min, _UTC), datetime.combine(end + timedelta(days=1), time.min, _UTC)


# ----------------------------------------------------------------------------------------------------- cells

def cell(value: object) -> str:
    """Text for a CSV cell, safe to open in a spreadsheet: a leading = + - @ tab or carriage return gets an apostrophe in front."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _md(value: object) -> str:
    return ("" if value is None else str(value)).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


# ----------------------------------------------------------------------------------------------------- the statement

@dataclass(frozen=True)
class StatementLine:
    occurred_at: datetime
    kind: str
    description: str
    amount_minor: int          # what the customer can spend changed by this: a top-up positive, a usage charge negative
    balance_minor: int         # what they could spend after it
    actor: str
    transaction_id: str


@dataclass(frozen=True)
class Statement:
    tenant_id: str
    currency: str
    start: date
    end: date
    opening_minor: int
    lines: tuple[StatementLine, ...]
    closing_minor: int

    @property
    def total_minor(self) -> int:
        return sum(l.amount_minor for l in self.lines)

    def agrees_with(self, spendable_minor: int) -> bool:
        return self.closing_minor == spendable_minor


async def statement(conn: asyncpg.Connection, tenant_id: str, currency: str, start: date, end: date) -> Statement:
    lo, hi = _bounds(start, end)
    opening = await conn.fetchval(
        """select coalesce(-sum(e.amount_minor), 0) from billing_entry e join billing_transaction t on t.id = e.transaction_id
             join billing_account a on a.id = e.account_id and a.kind = 'wallet'
            where e.tenant_id = $1::uuid and t.currency = $2 and t.occurred_at < $3""", tenant_id, currency, lo)
    rows = await conn.fetch(
        """select t.id, t.occurred_at, t.kind, t.reason, t.provider_ref, t.actor, -e.amount_minor as amount, u.event_type, u.quantity
             from billing_entry e join billing_transaction t on t.id = e.transaction_id
             join billing_account a on a.id = e.account_id and a.kind = 'wallet'
             left join billing_usage_event u on u.transaction_id = t.id
            where e.tenant_id = $1::uuid and t.currency = $2 and t.occurred_at >= $3 and t.occurred_at < $4
            order by t.occurred_at, e.id""", tenant_id, currency, lo, hi)
    balance, lines = int(opening), []
    for r in rows:
        balance += r["amount"]
        what = ("%d x %s" % (r["quantity"], r["event_type"])) if r["event_type"] else (r["reason"] or r["provider_ref"] or "")
        lines.append(StatementLine(r["occurred_at"], r["kind"], what, int(r["amount"]), balance, r["actor"], str(r["id"])))
    return Statement(tenant_id, currency, start, end, int(opening), tuple(lines), balance)


def statement_markdown(s: Statement, *, tenant_name: str | None = None) -> str:
    f = lambda m: format_minor(m, s.currency)  # noqa: E731
    out = ["# Statement%s, %s to %s (%s)" % (" for %s" % tenant_name if tenant_name else "", s.start.isoformat(), s.end.isoformat(), s.currency), "",
           "Opening balance %s, %d line(s), closing balance %s. Amounts are what the customer can spend: a top-up adds, usage takes away. Days are UTC." % (
               f(s.opening_minor), len(s.lines), f(s.closing_minor)), ""]
    out += ["| When (UTC) | Kind | What | Amount | Balance | By |", "|---|---|---|---|---|---|"]
    out.append("| | opening | | | %s | |" % f(s.opening_minor))
    for l in s.lines:
        out.append("| %s | %s | %s | %s | %s | %s |" % (l.occurred_at.astimezone(_UTC).strftime("%Y-%m-%d %H:%M"), _md(l.kind), _md(l.description), f(l.amount_minor),
                                                         f(l.balance_minor), _md(l.actor)))
    out.append("| | closing | | | %s | |" % f(s.closing_minor))
    return "\n".join(out) + "\n"


def statement_csv(s: Statement) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["when_utc", "kind", "description", "amount", "balance", "currency", "actor", "transaction_id"])
    w.writerow(["", "opening", "", "", format_minor(s.opening_minor, s.currency), s.currency, "", ""])
    for l in s.lines:
        w.writerow([l.occurred_at.astimezone(_UTC).strftime("%Y-%m-%d %H:%M:%S"), cell(l.kind), cell(l.description), format_minor(l.amount_minor, s.currency),
                    format_minor(l.balance_minor, s.currency), s.currency, cell(l.actor), l.transaction_id])
    w.writerow(["", "closing", "", "", format_minor(s.closing_minor, s.currency), s.currency, "", ""])
    return buf.getvalue()


# ----------------------------------------------------------------------------------------------------- the daily report

@dataclass(frozen=True)
class DayRow:
    day: date
    topups_minor: int = 0
    subscription_credits_minor: int = 0
    usage_minor: int = 0
    refunds_minor: int = 0              # money handed back to the card, taken out of the wallet
    service_credits_minor: int = 0      # usage credited back
    adjustments_minor: int = 0          # signed: what an admin added to or took from the wallet
    revenue_minor: int = 0              # what landed in platform revenue (usage, and the part of a subscription payment that is not credit)
    failed_payments: int = 0


@dataclass(frozen=True)
class UsageRow:
    event_type: str
    quantity: int
    amount_minor: int


@dataclass(frozen=True)
class DailyReport:
    tenant_id: str
    currency: str
    start: date
    end: date
    days: tuple[DayRow, ...]
    usage: tuple[UsageRow, ...]

    def total(self, name: str) -> int:
        return sum(getattr(d, name) for d in self.days)


async def daily_report(conn: asyncpg.Connection, tenant_id: str, currency: str, start: date, end: date) -> DailyReport:
    lo, hi = _bounds(start, end)
    effects = await conn.fetch(
        """select (t.occurred_at at time zone 'UTC')::date as day, t.kind, sum(-e.amount_minor)::bigint as effect
             from billing_transaction t join billing_entry e on e.transaction_id = t.id join billing_account a on a.id = e.account_id and a.kind = 'wallet'
            where t.tenant_id = $1::uuid and t.currency = $2 and t.occurred_at >= $3 and t.occurred_at < $4 group by 1, 2""", tenant_id, currency, lo, hi)
    revenue = await conn.fetch(
        """select (t.occurred_at at time zone 'UTC')::date as day, sum(-e.amount_minor)::bigint as revenue
             from billing_transaction t join billing_entry e on e.transaction_id = t.id join billing_account a on a.id = e.account_id and a.kind = 'platform_revenue'
            where t.tenant_id = $1::uuid and t.currency = $2 and t.occurred_at >= $3 and t.occurred_at < $4 group by 1""", tenant_id, currency, lo, hi)
    failed = await conn.fetch(
        """select (updated_at at time zone 'UTC')::date as day, count(*) as n from billing_payment
            where tenant_id = $1::uuid and currency = $2 and status = 'failed' and updated_at >= $3 and updated_at < $4 group by 1""", tenant_id, currency, lo, hi)
    usage = await conn.fetch(
        """select event_type, sum(quantity)::bigint as quantity, sum(amount_minor)::bigint as amount from billing_usage_event
            where tenant_id = $1::uuid and currency = $2 and occurred_at >= $3 and occurred_at < $4 group by event_type order by event_type""", tenant_id, currency, lo, hi)
    by_day: dict[date, dict[str, int]] = {}
    for r in effects:
        by_day.setdefault(r["day"], {})[r["kind"]] = int(r["effect"])
    rev = {r["day"]: int(r["revenue"]) for r in revenue}
    bad = {r["day"]: int(r["n"]) for r in failed}
    days = []
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        k = by_day.get(d, {})
        days.append(DayRow(d, topups_minor=k.get("topup", 0), subscription_credits_minor=k.get("subscription_payment", 0), usage_minor=-k.get("usage", 0),
                           refunds_minor=-k.get("refund", 0), service_credits_minor=k.get("usage_refund", 0), adjustments_minor=k.get("adjustment", 0),
                           revenue_minor=rev.get(d, 0), failed_payments=bad.get(d, 0)))
    return DailyReport(tenant_id, currency, start, end, tuple(days), tuple(UsageRow(r["event_type"], int(r["quantity"]), int(r["amount"])) for r in usage))


_DAY_COLUMNS = ("topups", "subscription_credits", "usage", "refunds", "service_credits", "adjustments", "revenue", "failed_payments")


def daily_markdown(r: DailyReport, *, tenant_name: str | None = None) -> str:
    f = lambda m: format_minor(m, r.currency)  # noqa: E731
    out = ["# Daily revenue and usage%s, %s to %s (%s)" % (" for %s" % tenant_name if tenant_name else "", r.start.isoformat(), r.end.isoformat(), r.currency), "",
           "Usage charged %s, platform revenue %s, top-ups %s, refunds %s over %d day(s). Days are UTC; a refund and a usage charge are shown as positive amounts in their own columns."
           % (f(r.total("usage_minor")), f(r.total("revenue_minor")), f(r.total("topups_minor")), f(r.total("refunds_minor")), len(r.days)), "",
           "| Day | Top-ups | Subscription credits | Usage | Refunds | Service credits | Adjustments | Platform revenue | Failed payments |", "|---|---|---|---|---|---|---|---|---|"]
    for d in r.days:
        out.append("| %s | %s | %s | %s | %s | %s | %s | %s | %d |" % (d.day.isoformat(), f(d.topups_minor), f(d.subscription_credits_minor), f(d.usage_minor), f(d.refunds_minor),
                                                                       f(d.service_credits_minor), f(d.adjustments_minor), f(d.revenue_minor), d.failed_payments))
    out += ["", "## Usage by event type", ""]
    if r.usage:
        out += ["| Event | Quantity | Charged |", "|---|---|---|"] + ["| %s | %d | %s |" % (_md(u.event_type), u.quantity, f(u.amount_minor)) for u in r.usage]
    else:
        out.append("No usage in this range.")
    return "\n".join(out) + "\n"


def daily_csv(r: DailyReport) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["day", *_DAY_COLUMNS, "currency"])
    for d in r.days:
        w.writerow([d.day.isoformat(), format_minor(d.topups_minor, r.currency), format_minor(d.subscription_credits_minor, r.currency), format_minor(d.usage_minor, r.currency),
                    format_minor(d.refunds_minor, r.currency), format_minor(d.service_credits_minor, r.currency), format_minor(d.adjustments_minor, r.currency),
                    format_minor(d.revenue_minor, r.currency), d.failed_payments, r.currency])
    w.writerow([])
    w.writerow(["event_type", "quantity", "charged", "currency"])
    for u in r.usage:
        w.writerow([cell(u.event_type), u.quantity, format_minor(u.amount_minor, r.currency), r.currency])
    return buf.getvalue()


# ----------------------------------------------------------------------------------------------------- reconciliation

@dataclass(frozen=True)
class Discrepancy:
    problem: str
    ref: str
    stored_minor: int
    derived_minor: int


@dataclass(frozen=True)
class ReconciliationReport:
    tenant_id: str
    discrepancies: tuple[Discrepancy, ...]
    accounts: int
    transactions: int
    entries: int
    trial_balance: dict[str, int] = field(default_factory=dict)      # currency -> sum of every entry; zero when the books balance

    @property
    def ok(self) -> bool:
        return not self.discrepancies


async def reconcile(conn: asyncpg.Connection, tenant_id: str) -> ReconciliationReport:
    found = [Discrepancy(r["problem"], r["ref"] or "", int(r["stored_minor"] or 0), int(r["derived_minor"] or 0))
             for r in await conn.fetch("select * from billing_discrepancies($1::uuid)", tenant_id)]
    trial = {r["currency"].strip(): int(r["total"]) for r in await conn.fetch(
        "select currency, sum(amount_minor)::bigint as total from billing_entry where tenant_id = $1::uuid group by currency", tenant_id)}
    for currency, total in sorted(trial.items()):
        if total != 0:
            found.append(Discrepancy("trial balance", currency, total, 0))
    for r in await conn.fetch("select kind, currency, balance_minor from billing_account where tenant_id = $1::uuid and kind = 'wallet' and balance_minor > 0", tenant_id):
        found.append(Discrepancy("wallet below zero", "%s %s" % (r["kind"], r["currency"].strip()), int(r["balance_minor"]), 0))
    counts = await conn.fetchrow(
        """select (select count(*) from billing_account where tenant_id = $1::uuid) as accounts,
                  (select count(*) from billing_transaction where tenant_id = $1::uuid) as transactions,
                  (select count(*) from billing_entry where tenant_id = $1::uuid) as entries""", tenant_id)
    return ReconciliationReport(tenant_id, tuple(found), counts["accounts"], counts["transactions"], counts["entries"], trial)


async def assert_reconciled(conn: asyncpg.Connection, tenant_id: str) -> ReconciliationReport:
    """The report when the books agree; ReconciliationFailed (carrying it) when they do not."""
    report = await reconcile(conn, tenant_id)
    if not report.ok:
        raise ReconciliationFailed(report)
    return report


def reconciliation_markdown(r: ReconciliationReport) -> str:
    out = ["# Reconciliation", "",
           "%s: %d account(s), %d transaction(s), %d entr%s checked." % ("BOOKS AGREE" if r.ok else "BOOKS DO NOT AGREE (%d problem(s))" % len(r.discrepancies),
                                                                      r.accounts, r.transactions, r.entries, "y" if r.entries == 1 else "ies"), ""]
    out.append("Trial balance by currency (every entry added up; zero when the books balance): %s." % (", ".join("%s %d" % kv for kv in sorted(r.trial_balance.items())) or "no entries"))
    if r.discrepancies:
        out += ["", "| Problem | Subject | Stored | Derived from the entries |", "|---|---|---|---|"]
        out += ["| %s | %s | %d | %d |" % (_md(d.problem), _md(d.ref), d.stored_minor, d.derived_minor) for d in r.discrepancies]
    return "\n".join(out) + "\n"


def reconciliation_csv(r: ReconciliationReport) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["problem", "subject", "stored_minor", "derived_minor"])
    for d in r.discrepancies:
        w.writerow([cell(d.problem), cell(d.ref), d.stored_minor, d.derived_minor])
    return buf.getvalue()

