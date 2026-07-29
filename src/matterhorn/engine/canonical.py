from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from matterhorn.contracts import Operation, SourceRef


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def instant_text(value: datetime) -> str:
    return as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return instant_text(value)
    if isinstance(value, BaseModel):
        return json_value(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, (set, frozenset, tuple)):
        return [json_value(item) for item in sorted(value)]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        json_value(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def object_key(value: Any) -> str:
    return canonical_json(value)


def derive_assertion_id(
    scope_id: str,
    subject_key: str,
    predicate: str,
    operation: Operation | str,
    value_key: str,
    valid_from: datetime,
    source_refs: list[SourceRef],
) -> str:
    payload = [
        scope_id,
        subject_key,
        predicate,
        operation.value if isinstance(operation, Operation) else operation,
        value_key,
        instant_text(valid_from),
        sorted(ref.source_id for ref in source_refs),
    ]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
