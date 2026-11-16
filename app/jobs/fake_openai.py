"""A stand-in OpenAI-compatible server on 127.0.0.1, written from the API reference pages for `POST /v1/embeddings` and `POST /v1/chat/completions`.

It is not a model. Embeddings are a hashed bag of words, the same text always gives the same vector, and two texts that share words are
closer, so a nearest neighbour search over them ranks by shared vocabulary, which is enough to test the plumbing (vectors stored, tenant
filter, ranking order) without a network call. Chat answers come from a script the test writes: each call pops the next entry, so a test can
make the "model" invent a skill on the first round and behave on the second, and read every request it was sent.

What it follows from the reference: bearer auth (a wrong key is 401 with an `error` object), `input` as a string or a list, `data[i].embedding`
and `index`, a `usage` block, `model` echoed back, and for chat `choices[0].message.content`. What it invents, so it must not be read as the
service's behaviour: the vector width (`dimensions` when asked, else 256), the hashing, and every error body beyond the 401.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from app.connectors.fake_http import Answer, FakeHTTP, Seen

WORD = re.compile(r"[a-z0-9+#.]+")


def embed_text(text: str, dim: int = 256) -> list[float]:
    v = [0.0] * dim
    for w in WORD.findall(text.lower()):
        h = int.from_bytes(hashlib.sha256(w.encode("utf-8")).digest()[:8], "big")
        v[h % dim] += 1.0 if (h >> 63) & 1 else -1.0
        v[(h >> 20) % dim] += 0.5
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class FakeOpenAI(FakeHTTP):
    def __init__(self, *, api_key: str = "sk-test") -> None:
        super().__init__()
        self.api_key = api_key
        #: Each chat call pops the first entry: a string (the message content) or a dict (sent as the whole `message`).
        self.chat_script: list[Any] = []
        self.chat_requests: list[dict[str, Any]] = []
        self.embedding_requests: list[dict[str, Any]] = []

    def answer(self, seen: Seen) -> Answer:
        auth = seen.headers.get("authorization", "")
        if auth != "Bearer %s" % self.api_key:
            return 401, {}, {"error": {"message": "Incorrect API key provided.", "type": "invalid_request_error", "code": "invalid_api_key"}}
        body = json.loads(seen.body or b"{}")
        if seen.method == "POST" and seen.path.endswith("/embeddings"):
            self.embedding_requests.append(body)
            inputs = body.get("input")
            if isinstance(inputs, str):
                inputs = [inputs]
            if not isinstance(inputs, list) or not all(isinstance(t, str) for t in inputs):
                return 400, {}, {"error": {"message": "'input' must be a string or an array of strings", "type": "invalid_request_error"}}
            dim = int(body.get("dimensions") or 256)
            data = [{"object": "embedding", "index": i, "embedding": embed_text(t, dim)} for i, t in enumerate(inputs)]
            return 200, {}, {"object": "list", "data": data, "model": body.get("model"), "usage": {"prompt_tokens": sum(len(t.split()) for t in inputs), "total_tokens": sum(len(t.split()) for t in inputs)}}
        if seen.method == "POST" and seen.path.endswith("/chat/completions"):
            self.chat_requests.append(body)
            if not self.chat_script:
                return 500, {}, {"error": {"message": "the stand-in has no scripted answer left", "type": "server_error"}}
            nxt = self.chat_script.pop(0)
            message = nxt if isinstance(nxt, dict) else {"role": "assistant", "content": nxt}
            return 200, {}, {"id": "chatcmpl-test", "object": "chat.completion", "model": body.get("model"),
                             "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
        return 404, {}, {"error": {"message": "not found: %s" % seen.path}}
