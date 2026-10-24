"""A stand-in for a CRM, a Slack webhook or an n8n flow, for tests and the lab run. No real CRM was used anywhere in this project.

A sink is any callable `(payload, idempotency_key) -> SinkAnswer`. This one keeps every call it receives and creates at most one "task" per idempotency key, the way a CRM that honours an
idempotency key would. `script` decides the answer to each call: a list of answers or exceptions consumed in order (then "ok" once the list runs out), or a function of the call number.
`acts_before_failing` makes the sink do the work and THEN fail, which is what a timeout after the CRM already created the task looks like.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from app.changes import SinkAnswer

__all__ = ["FakeSink"]


class FakeSink:
    def __init__(self, script: Sequence[Any] | Callable[[int], Any] | None = None, *, acts_before_failing: bool = False):
        self.script = script
        self.acts_before_failing = acts_before_failing
        self.calls: list[tuple[dict[str, Any], str]] = []
        self.tasks: dict[str, str] = {}

    def _task(self, key: str) -> str:
        return self.tasks.setdefault(key, "task-%04d" % (len(self.tasks) + 1))

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
            self._task(key)
        if isinstance(step, BaseException):
            raise step
        if step == "ok":
            return SinkAnswer("ok", self._task(key))
        if step == "rejected":
            return SinkAnswer("rejected", None, "the sink refused the content")
        if step == "ambiguous":
            return SinkAnswer("ambiguous", None, "timeout")
        return step
