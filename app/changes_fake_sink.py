"""Stand-ins for a CRM, a Slack webhook or an n8n flow, for tests and the lab run. No real CRM was used anywhere in this project.

A sink is any callable `(payload, idempotency_key) -> SinkAnswer`. The key reaches a real CRM as the value of a unique field on the record (Salesforce external id, a HubSpot custom unique
property, a Zoho duplicate check field), and the sink upserts on it. None of the CRM developer pages read for this project documents an idempotency key header, so nothing here assumes one.

`FakeSink` upserts: it keeps one record per `external_key` in the payload, the way a CRM matched on a unique field would. `AppendSink` does not dedupe at all, the way a plain "create a
task" call behaves, so a resend leaves a second record and the tests can show the limit instead of asserting it.

`script` decides the answer to each call: a list of answers or exceptions consumed in order (then "ok" once the list runs out), or a function of the call number. `acts_before_failing` makes
the sink do the work and THEN fail, which is what a timeout after the CRM already stored the record looks like.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from app.changes import SinkAnswer

__all__ = ["FakeSink", "AppendSink"]


class FakeSink:
    def __init__(self, script: Sequence[Any] | Callable[[int], Any] | None = None, *, acts_before_failing: bool = False):
        self.script = script
        self.acts_before_failing = acts_before_failing
        self.calls: list[tuple[dict[str, Any], str]] = []
        self.tasks: dict[str, str] = {}

    def _task(self, payload: dict[str, Any], key: str) -> str:
        return self.tasks.setdefault(payload.get("external_key", key), "task-%04d" % (len(self.tasks) + 1))

    def _next(self) -> Any:
        n = len(self.calls)
        if callable(self.script):
            return self.script(n)
        if self.script is not None and n <= len(self.script):
            return self.script[n - 1]
        return "ok"

    def __call__(self, payload: dict[str, Any], key: str) -> SinkAnswer:
        self.calls.append((payload, key))
        step = self._next()
        if self.acts_before_failing and step != "ok":
            self._task(payload, key)
        if isinstance(step, BaseException):
            raise step
        if step == "ok":
            return SinkAnswer("ok", self._task(payload, key))
        if step == "rejected":
            return SinkAnswer("rejected", None, "the sink refused the content")
        if step == "ambiguous":
            return SinkAnswer("ambiguous", None, "timeout")
        return step


class AppendSink:
    """A sink with no dedupe: every call stores another record."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def __call__(self, payload: dict[str, Any], key: str) -> SinkAnswer:
        self.records.append(payload)
        return SinkAnswer("ok", "record-%04d" % len(self.records))
