"""Bounded provider tool-use loop for the Console chat endpoint."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from matterhorn.gateway_config import _timeout_seconds

MAX_TOOL_CALLS = 6
SYSTEM_PROMPT = (
    "You are the Matterhorn Console assistant. Answer only from the supplied "
    "deterministic query tools. Use tools before making factual claims. "
    "Never invent matters, values, people, dates, or evidence. Keep answers "
    "concise and mention uncertainty when a query returns no data. "
    "Start with list_matters: it already returns every matter's title and "
    "complete current-value dictionary, including status, owned_by, blocked_by, "
    "next_step, and due_at when present. Use per-predicate queries only for "
    "history, effective-time questions, or unusual predicates not already "
    "answered by list_matters. Predicate arguments accept exactly one registered "
    "predicate name; wildcards are invalid."
)
FINALIZE_PROMPT = (
    "Give the best final answer now using only the tool results gathered so far. "
    "Do not request another tool. Say plainly what you could not verify."
)
FALLBACK_ANSWER = (
    "I could not complete this query. Please review the evidence gathered so far."
)

_PREDICATE_PARAMETER = {
    "type": "string",
    "description": (
        "One registered predicate name, for example: status, owned_by, "
        "blocked_by, next_step, or due_at. Wildcards are invalid."
    ),
}

_TOOL_PARAMETERS = {
    "list_matters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "query_current": {
        "type": "object",
        "properties": {
            "subject_key": {"type": "string"},
            "predicate": _PREDICATE_PARAMETER,
        },
        "required": ["subject_key", "predicate"],
        "additionalProperties": False,
    },
    "query_timeline": {
        "type": "object",
        "properties": {
            "subject_key": {"type": "string"},
            "predicate": _PREDICATE_PARAMETER,
        },
        "required": ["subject_key", "predicate"],
        "additionalProperties": False,
    },
    "query_at": {
        "type": "object",
        "properties": {
            "subject_key": {"type": "string"},
            "predicate": _PREDICATE_PARAMETER,
            "instant": {
                "type": "string",
                "description": "RFC 3339 timestamp",
            },
        },
        "required": ["subject_key", "predicate", "instant"],
        "additionalProperties": False,
    },
    "query_by_person": {
        "type": "object",
        "properties": {"person_id": {"type": "string"}},
        "required": ["person_id"],
        "additionalProperties": False,
    },
}
_DESCRIPTIONS = {
    "list_matters": (
        "List matters with each title and complete current dictionary, including "
        "status, owned_by, blocked_by, next_step, and due_at when present. This "
        "already answers normal current-state questions."
    ),
    "query_current": (
        "Read an unusual current predicate not already answered by list_matters. "
        "Use one literal registered predicate such as status, owned_by, "
        "blocked_by, next_step, or due_at; never use a wildcard."
    ),
    "query_timeline": (
        "Read history for one literal registered predicate such as status, "
        "owned_by, blocked_by, next_step, or due_at; never use a wildcard."
    ),
    "query_at": (
        "Read one literal registered predicate at an effective-time instant, "
        "such as status, owned_by, blocked_by, next_step, or due_at; never use "
        "a wildcard."
    ),
    "query_by_person": "List matters currently related to a person id.",
}


class ConsoleChatRunner:
    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float,
        client: Any = None,
        max_tool_calls: int = MAX_TOOL_CALLS,
    ):
        if provider not in {"openai-compatible", "anthropic"}:
            raise ValueError("unsupported Console chat provider")
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tool_calls = min(max_tool_calls, MAX_TOOL_CALLS)
        self._client = client

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def run(
        self,
        *,
        service: Any,
        scope_id: str,
        message: str,
        history: list[dict[str, str]],
    ) -> dict[str, Any]:
        if self.provider == "anthropic":
            return self._run_anthropic(service, scope_id, message, history)
        return self._run_openai(service, scope_id, message, history)

    def _run_openai(
        self,
        service: Any,
        scope_id: str,
        message: str,
        history: list[dict[str, str]],
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": message},
        ]
        evidence: list[dict[str, Any]] = []
        tool_count = 0
        while tool_count < self.max_tool_calls:
            response = self._http().post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "tools": _openai_tools(),
                    "tool_choice": "auto",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            assistant = response.json()["choices"][0]["message"]
            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                answer = assistant.get("content") or ""
                if answer.strip():
                    return _chat_result(answer, evidence, tool_count)
                break
            if tool_count + len(tool_calls) > self.max_tool_calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant.get("content"),
                    "tool_calls": tool_calls,
                }
            )
            for tool_call in tool_calls:
                function = tool_call["function"]
                args = json.loads(function.get("arguments") or "{}")
                result, trace = _execute_tool_result(
                    service, scope_id, function["name"], args
                )
                evidence.append(trace)
                tool_count += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": json.dumps(
                            result, ensure_ascii=False, default=str
                        ),
                    }
                )
        return self._finalize_openai(messages, evidence, tool_count)

    def _run_anthropic(
        self,
        service: Any,
        scope_id: str,
        message: str,
        history: list[dict[str, str]],
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            *history,
            {"role": "user", "content": message},
        ]
        evidence: list[dict[str, Any]] = []
        tool_count = 0
        while tool_count < self.max_tool_calls:
            response = self._http().post(
                f"{self.base_url}/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": self.model,
                    "max_tokens": 2048,
                    "system": SYSTEM_PROMPT,
                    "messages": messages,
                    "tools": _anthropic_tools(),
                    "tool_choice": {"type": "auto"},
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            content = payload["content"]
            tool_uses = [
                block for block in content if block.get("type") == "tool_use"
            ]
            if not tool_uses:
                answer = "".join(
                    block.get("text", "")
                    for block in content
                    if block.get("type") == "text"
                )
                if answer.strip():
                    return _chat_result(answer, evidence, tool_count)
                break
            if tool_count + len(tool_uses) > self.max_tool_calls:
                break
            messages.append({"role": "assistant", "content": content})
            results = []
            for tool_use in tool_uses:
                result, trace = _execute_tool_result(
                    service,
                    scope_id,
                    tool_use["name"],
                    tool_use.get("input") or {},
                )
                evidence.append(trace)
                tool_count += 1
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use["id"],
                        "content": json.dumps(
                            result, ensure_ascii=False, default=str
                        ),
                    }
                )
            messages.append({"role": "user", "content": results})
        return self._finalize_anthropic(messages, evidence, tool_count)

    def _finalize_openai(
        self,
        messages: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        tool_count: int,
    ) -> dict[str, Any]:
        final_messages = [*messages, {"role": "user", "content": FINALIZE_PROMPT}]
        try:
            response = self._http().post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": final_messages,
                    "tools": _openai_tools(),
                    "tool_choice": "none",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"].get("content") or ""
            if answer.strip():
                return _chat_result(answer, evidence, tool_count)
        except Exception:  # noqa: BLE001
            # The bounded fallback must survive every provider/client failure.
            return _chat_result(FALLBACK_ANSWER, evidence, tool_count)
        return _chat_result(FALLBACK_ANSWER, evidence, tool_count)

    def _finalize_anthropic(
        self,
        messages: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        tool_count: int,
    ) -> dict[str, Any]:
        final_messages = [*messages, {"role": "user", "content": FINALIZE_PROMPT}]
        try:
            response = self._http().post(
                f"{self.base_url}/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": self.model,
                    "max_tokens": 2048,
                    "system": SYSTEM_PROMPT,
                    "messages": final_messages,
                    "tools": _anthropic_tools(),
                    "tool_choice": {"type": "none"},
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            answer = "".join(
                block.get("text", "")
                for block in response.json()["content"]
                if block.get("type") == "text"
            )
            if answer.strip():
                return _chat_result(answer, evidence, tool_count)
        except Exception:  # noqa: BLE001
            # The bounded fallback must survive every provider/client failure.
            return _chat_result(FALLBACK_ANSWER, evidence, tool_count)
        return _chat_result(FALLBACK_ANSWER, evidence, tool_count)


def chat_runner_from_environment(*, client: Any = None) -> ConsoleChatRunner | None:
    provider = os.environ.get("MATTERHORN_PROVIDER", "null")
    if provider not in {"openai-compatible", "anthropic"}:
        return None
    fallback = (
        "OPENAI_API_KEY"
        if provider == "openai-compatible"
        else "ANTHROPIC_API_KEY"
    )
    api_key = os.environ.get("MATTERHORN_API_KEY") or os.environ.get(fallback)
    model = os.environ.get("MATTERHORN_MODEL")
    base_url = os.environ.get("MATTERHORN_BASE_URL")
    if provider == "anthropic":
        base_url = base_url or "https://api.anthropic.com"
    if not api_key or not model or not base_url:
        return None
    return ConsoleChatRunner(
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout=_timeout_seconds(),
        client=client,
    )


def _openai_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": _DESCRIPTIONS[name],
                "parameters": parameters,
            },
        }
        for name, parameters in _TOOL_PARAMETERS.items()
    ]


def _anthropic_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": _DESCRIPTIONS[name],
            "input_schema": parameters,
        }
        for name, parameters in _TOOL_PARAMETERS.items()
    ]


def _execute_tool(
    service: Any,
    scope_id: str,
    name: str,
    args: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    expected = {
        "list_matters": set(),
        "query_current": {"subject_key", "predicate"},
        "query_timeline": {"subject_key", "predicate"},
        "query_at": {"subject_key", "predicate", "instant"},
        "query_by_person": {"person_id"},
    }
    if name not in expected:
        raise ValueError(f"unknown Console query tool: {name}")
    if set(args) != expected[name]:
        raise ValueError(f"invalid arguments for Console query tool {name}")
    if not all(isinstance(value, str) and value for value in args.values()):
        raise ValueError(f"Console query tool {name} requires string arguments")

    if name == "list_matters":
        result = [
            item.to_dict()
            for item in service.engine.query.list_matters(scope_id)
        ]
    elif name == "query_current":
        result = service.query_current(scope_id=scope_id, **args)
    elif name == "query_timeline":
        result = service.query_timeline(scope_id=scope_id, **args)
    elif name == "query_at":
        parsed = datetime.fromisoformat(args["instant"])
        result = service.query_at(
            scope_id=scope_id,
            subject_key=args["subject_key"],
            predicate=args["predicate"],
            instant=parsed,
        )
    else:
        result = service.query_by_person(scope_id=scope_id, **args)

    source_ids = sorted(_collect(result, "source_ids"))
    subject_keys = sorted(_collect(result, "subject_key"))
    return result, {
        "name": name,
        "args": args,
        "source_ids": source_ids,
        "subject_keys": subject_keys,
    }


def _execute_tool_result(
    service: Any,
    scope_id: str,
    name: str,
    args: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    try:
        return _execute_tool(service, scope_id, name, args)
    except ValueError as error:
        message = str(error)
        subject_key = args.get("subject_key")
        subject_keys = [subject_key] if isinstance(subject_key, str) else []
        return {"error": message}, {
            "name": name,
            "args": args,
            "source_ids": [],
            "subject_keys": subject_keys,
            "error": message,
        }


def _collect(value: Any, key: str) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        selected = value.get(key)
        if isinstance(selected, str):
            found.add(selected)
        elif isinstance(selected, list):
            found.update(item for item in selected if isinstance(item, str))
        for item in value.values():
            found.update(_collect(item, key))
    elif isinstance(value, list):
        for item in value:
            found.update(_collect(item, key))
    return found


def _chat_result(
    answer: str,
    evidence: list[dict[str, Any]],
    tool_count: int,
) -> dict[str, Any]:
    return {
        "answer": answer.strip(),
        "evidence": evidence,
        "tool_calls": tool_count,
    }
