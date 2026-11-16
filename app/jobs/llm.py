"""A small structured-output LLM call over the OpenAI chat completions API (OpenAI, or any compatible server such as OpenRouter).

`chat_json` asks for a JSON object that matches a JSON schema (`response_format` of type json_schema, strict), parses the reply and checks
it against the same schema with jsonschema, so a reply that is not the shape asked for is an error here and never a surprise three steps later.
It retries a 429 or a 5xx with a growing wait and gives up after `attempts`; a 4xx other than 429 is not retried, since asking again cannot help.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
import jsonschema


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, *, api_key: str, base_url: str = "https://api.openai.com/v1", model: str = "gpt-4o", attempts: int = 3, timeout: float = 90.0,
                 sleep=time.sleep, extra_headers: dict[str, str] | None = None, max_tokens: int = 4000):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self.model = model
        self._attempts = attempts
        self._timeout = timeout
        self._sleep = sleep
        self._headers = extra_headers or {}
        self._max_tokens = max_tokens

    def chat_json(self, system: str, user: str, schema: dict[str, Any], *, name: str = "answer", temperature: float = 0.2) -> dict[str, Any]:
        payload = {
            "model": self.model, "temperature": temperature, "max_tokens": self._max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        }
        last = "no attempt was made"
        for attempt in range(self._attempts):
            try:
                r = httpx.post(self._base + "/chat/completions", json=payload, headers={"Authorization": "Bearer " + self._key, **self._headers}, timeout=self._timeout)
            except httpx.TransportError as exc:
                last = "transport: %s" % exc
                if attempt + 1 < self._attempts:
                    self._sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = "HTTP %d" % r.status_code
                if attempt + 1 < self._attempts:
                    self._sleep(float(r.headers.get("retry-after", 2 ** attempt)) if r.status_code == 429 else 2 ** attempt)
                continue
            if r.status_code >= 400:
                raise LLMError("HTTP %d: %s" % (r.status_code, r.text[:300]))
            try:
                content = r.json()["choices"][0]["message"]["content"]
                data = json.loads(content)
                jsonschema.validate(data, schema)
            except (KeyError, IndexError, ValueError, jsonschema.ValidationError) as exc:
                raise LLMError("the reply is not the shape asked for: %s" % str(exc)[:300]) from exc
            return data
        raise LLMError("gave up after %d attempts (%s)" % (self._attempts, last))
