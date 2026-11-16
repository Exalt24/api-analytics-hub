"""A sliding window of "seen" fingerprints in Redis (Upstash or any Redis 6+), checked before the database is touched.

THE QUESTION IT ANSWERS: has this job been reported in the last `window_seconds`? Each fingerprint is a member of one sorted set scored by
the time it was last seen. One Lua script, run atomically, drops members older than the window, reads whether this member is still there,
and writes it with the new time. So the window slides: a job that keeps being reported every few hours stays a duplicate for as long as it
keeps coming, and one that goes quiet for the whole window is new again.

WHY ONE SCRIPT AND NOT ZSCORE THEN ZADD. Two workers handling the same job at the same moment would both read "not seen" and both write
it. In a script the read and the write cannot be interleaved, so exactly one caller sees `new`.

WHY THE TIME COMES FROM REDIS. Workers on different machines disagree about the clock by seconds; the server's TIME is one clock for all
of them. `now` can be passed in for a test, which is the only reason the parameter exists.

THE DATABASE IS THE BACKSTOP. If Redis is flushed or unreachable the window is empty, so a job is treated as new and reaches the upsert in
store.py, where the unique (tenant, fingerprint) index makes it one row regardless. The window saves work, it does not decide truth.
When Redis is unreachable `seen_many` raises `DedupeUnavailable` and the pipeline carries on without it, and counts that in the run.

SIZE. The set holds one member per fingerprint seen in the last window and is trimmed on every call, so its size is the number of distinct
jobs per window, not per all time. The key itself expires after one window with no traffic.
"""
from __future__ import annotations

import hashlib
from typing import Iterable

import redis

SCRIPT = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local now
if ARGV[2] ~= '' then
  now = tonumber(ARGV[2])
else
  local t = redis.call('TIME')
  now = tonumber(t[1]) + tonumber(t[2]) / 1000000
end
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local result = {}
for i = 3, #ARGV do
  local was = redis.call('ZSCORE', key, ARGV[i])
  if was then result[#result + 1] = 1 else result[#result + 1] = 0 end
  redis.call('ZADD', key, now, ARGV[i])
end
redis.call('EXPIRE', key, math.ceil(window))
return result
"""


class DedupeUnavailable(RuntimeError):
    """Redis could not be reached or refused the script. The caller decides; the pipeline falls back to the database."""


def fingerprint(dedupe_key: str) -> str:
    """The 64 character hash of the normaliser's dedupe key, which is what both Redis and Postgres hold."""
    return hashlib.sha256(dedupe_key.strip().lower().encode("utf-8")).hexdigest()


class SlidingWindowDedupe:
    def __init__(self, client: "redis.Redis", *, window_seconds: float = 72 * 3600, prefix: str = "jobs:seen"):
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._client = client
        self._window = float(window_seconds)
        self._prefix = prefix
        self._script = client.register_script(SCRIPT)

    def _key(self, tenant_id: str) -> str:
        # one set per tenant, so one tenant's volume never evicts another's and a key scan never mixes them
        return "%s:%s" % (self._prefix, tenant_id)

    def seen_many(self, tenant_id: str, fingerprints: Iterable[str], *, now: float | None = None) -> list[bool]:
        """True for each fingerprint already seen inside the window. Every one is recorded as seen now, repeats in one call included
        (the second copy of a fingerprint inside a single batch is reported as seen)."""
        fps = list(fingerprints)
        if not fps:
            return []
        try:
            out = self._script(keys=[self._key(tenant_id)], args=[self._window, "" if now is None else repr(float(now)), *fps])
        except redis.RedisError as exc:  # connection refused, a timeout, a server that does not run scripts
            raise DedupeUnavailable(str(exc)) from exc
        return [bool(v) for v in out]

    def size(self, tenant_id: str) -> int:
        return int(self._client.zcard(self._key(tenant_id)))
