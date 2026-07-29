from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LlmGateway(Protocol):
    def complete(self, *, system: str, user: str, response_schema: dict) -> str:
        """Return one JSON document conforming to response_schema."""


class NullGateway:
    def complete(self, *, system: str, user: str, response_schema: dict) -> str:
        raise RuntimeError("semantic distillation requires an LLM gateway")


class OpenAICompatibleGateway:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._client: Any = None
        self._format_index = 0

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def complete(self, *, system: str, user: str, response_schema: dict) -> str:
        import httpx

        # Structured-output support varies across OpenAI-compatible providers
        # (DeepSeek and many local servers reject json_schema). The validation
        # gate is the real enforcement (P3); the response_format is only an
        # optimization, so degrade json_schema -> json_object -> none on a 400
        # that names response_format, remembering what worked.
        formats: list[dict | None] = [
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "matterhorn_semantic_candidates",
                    "strict": True,
                    "schema": response_schema,
                },
            },
            {"type": "json_object"},
            None,
        ]
        start = self._format_index
        last_error: httpx.HTTPStatusError | None = None
        for index in range(start, len(formats)):
            payload: dict = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            if formats[index] is not None:
                payload["response_format"] = formats[index]
            response = self._http().post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            if response.status_code == 400 and "response_format" in response.text:
                last_error = httpx.HTTPStatusError(
                    response.text, request=response.request, response=response
                )
                continue
            response.raise_for_status()
            self._format_index = index
            return response.json()["choices"][0]["message"]["content"]
        raise last_error if last_error is not None else RuntimeError(
            "no response_format variant accepted"
        )


class AnthropicGateway:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com",
        timeout: float = 60.0,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: Any = None

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def complete(self, *, system: str, user: str, response_schema: dict) -> str:
        response = self._http().post(
            f"{self.base_url}/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": self.model,
                "max_tokens": 2048,
                "system": system,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"{user}\n\nThe response MUST match this JSON Schema:\n"
                            f"{json.dumps(response_schema, separators=(',', ':'))}"
                        ),
                    }
                ],
            },
        )
        response.raise_for_status()
        blocks = response.json()["content"]
        return "".join(block["text"] for block in blocks if block["type"] == "text")
