"""INV-23 gather references, admission gates, and federated read models.

Cross-scope subject objects use one canonical string encoding:
``<percent-encoded-scope-id>:<percent-encoded-subject-key>``. Percent encoding
makes the separator unambiguous even when either component itself contains a
colon. The assertion's ordinary ``object_key`` identifies an independent SET
membership slot; by default callers derive it from this canonical value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from urllib.parse import quote, unquote

from matterhorn.contracts import Assertion, Operation, SubjectRecord
from matterhorn.engine.goal_graph import StructuralRollup
from matterhorn.engine.structure_election import GATHERS


class GatherRejection(str, Enum):
    INVALID_TARGET = "GATHER_INVALID_TARGET"
    UNKNOWN_TARGET = "GATHER_UNKNOWN_TARGET"
    SELF_REFERENCE = "GATHER_SELF_REFERENCE"
    LAYER_VIOLATION = "GATHER_LAYER_VIOLATION"


@dataclass(frozen=True)
class GatherMember:
    scope_id: str
    subject_key: str
    title: str
    layer: int
    status: Any
    blocker: list[Any]
    rollup: StructuralRollup
    source_ids: list[str]
    supporting_assertion_ids: list[str]
    origin: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "rollup": self.rollup.to_dict(),
        }


@dataclass(frozen=True)
class GatherScope:
    scope_id: str
    members: list[GatherMember]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "members": [item.to_dict() for item in self.members],
        }


@dataclass(frozen=True)
class FederatedRollup:
    total: int
    completed: int
    blocked: int
    bubbled_blockers: list[dict[str, Any]]
    latest_activity: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GatherView:
    scope_id: str
    subject_key: str
    subject: SubjectRecord
    members_by_scope: list[GatherScope]
    aggregate: FederatedRollup

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "subject_key": self.subject_key,
            "subject": {
                "scope_id": self.subject.scope_id,
                "subject_key": self.subject.subject_key,
                "subject_type": self.subject.subject_type,
                "title": self.subject.title,
                "normalized_title": self.subject.normalized_title,
                "source_ids": sorted(
                    self.subject.source_ids, key=lambda item: item.encode()
                ),
                "parent_subject_key": self.subject.parent_subject_key,
                "thread_ids": sorted(
                    self.subject.thread_ids, key=lambda item: item.encode()
                ),
                "layer": self.subject.layer,
            },
            "members_by_scope": [
                item.to_dict() for item in self.members_by_scope
            ],
            "aggregate": self.aggregate.to_dict(),
        }


def encode_subject_ref(scope_id: str, subject_key: str) -> str:
    """Encode one cross-scope subject identity as a canonical object value."""

    if not scope_id or not subject_key:
        raise ValueError("gather subject references require non-empty components")
    return f"{quote(scope_id, safe='')}:{quote(subject_key, safe='')}"


def decode_subject_ref(value: str) -> tuple[str, str]:
    """Decode and validate one canonical gather object value."""

    if not isinstance(value, str) or value.count(":") != 1:
        raise ValueError("gather target MUST be a canonical cross-scope reference")
    encoded_scope, encoded_subject = value.split(":", 1)
    scope_id = unquote(encoded_scope)
    subject_key = unquote(encoded_subject)
    if not scope_id or not subject_key:
        raise ValueError("gather subject references require non-empty components")
    if encode_subject_ref(scope_id, subject_key) != value:
        raise ValueError("gather target MUST use canonical percent encoding")
    return scope_id, subject_key


def gather_rejection(
    assertion: Assertion,
    *,
    subjects: Mapping[tuple[str, str], SubjectRecord],
    source_layer_min: int = 2,
    subject_key_resolver: Callable[[str, str], str] | None = None,
) -> GatherRejection | None:
    """Apply INV-23 without consulting or mutating any scope-local goal tree."""

    if assertion.operation != Operation.ASSERT or assertion.predicate != GATHERS:
        return None
    if not isinstance(assertion.object_value, str):
        return GatherRejection.INVALID_TARGET
    try:
        target_ref = decode_subject_ref(assertion.object_value)
    except ValueError:
        return GatherRejection.INVALID_TARGET
    source_ref = (assertion.scope_id, assertion.subject_key)
    source = subjects.get(source_ref)
    target = subjects.get(target_ref)
    if target is None:
        return GatherRejection.UNKNOWN_TARGET
    resolver = subject_key_resolver or (lambda _scope_id, key: key)
    canonical_source_ref = (
        source_ref[0],
        resolver(source_ref[0], source_ref[1]),
    )
    canonical_target_ref = (
        target_ref[0],
        resolver(target_ref[0], target_ref[1]),
    )
    if canonical_source_ref == canonical_target_ref:
        return GatherRejection.SELF_REFERENCE
    source = subjects.get(canonical_source_ref, source)
    target = subjects.get(canonical_target_ref, target)
    if (
        source is None
        or source.layer < source_layer_min
        or source.layer <= target.layer
    ):
        return GatherRejection.LAYER_VIOLATION
    return None


__all__ = [
    "FederatedRollup",
    "GatherMember",
    "GatherRejection",
    "GatherScope",
    "GatherView",
    "decode_subject_ref",
    "encode_subject_ref",
    "gather_rejection",
]
