from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import (
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from matterhorn.canonical import canonical_json, normalize_title
from matterhorn.contracts import EpisodeCard
from matterhorn.contracts.models import StrictModel

ADJUDICATION_SCHEMA_ID = "matterhorn-identity-adjudication/v1"
PINNED_ADJUDICATION_EXAMPLE = (
    '{"decision":"attach","subject_key":"matter-204","confidence":0.86,'
    '"evidence_source_ids":["chat:release:r17"]}'
)


class AdjudicationCandidate(StrictModel):
    subject_key: str
    title: str
    aliases: list[str] = Field(default_factory=list)
    handles: list[str] = Field(default_factory=list)
    status: Any = None
    next_step: Any = None
    participants: list[Any] = Field(default_factory=list)
    recent_evidence: str = ""


class AdjudicationResponse(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["attach", "new", "abstain"]
    subject_key: str | None
    confidence: float = Field(ge=0, le=1)
    evidence_source_ids: list[str]

    @field_validator("evidence_source_ids")
    @classmethod
    def evidence_ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("evidence_source_ids MUST be unique")
        return value

    @model_validator(mode="after")
    def non_attach_has_no_subject(self) -> AdjudicationResponse:
        if self.decision != "attach" and self.subject_key is not None:
            raise ValueError("new and abstain decisions MUST have null subject_key")
        return self


@dataclass(frozen=True)
class AdjudicationPrompt:
    system: str
    user: str
    response_schema: dict[str, Any]


@dataclass(frozen=True)
class AdjudicationGate:
    outcome: Literal["attach", "new", "review"]
    subject_key: str | None
    reasons: tuple[str, ...] = ()


def build_adjudication_prompt(
    card: EpisodeCard,
    candidates: list[AdjudicationCandidate],
) -> AdjudicationPrompt:
    system = (
        "You adjudicate whether one evidence-backed card belongs to one offered "
        "open matter. Use only the offered candidates and the card's real cited "
        "source IDs. Return exactly one closed JSON object. Choose abstain when "
        "the evidence does not safely support attach or new. Literal example: "
        + PINNED_ADJUDICATION_EXAMPLE
    )
    user = canonical_json(
        {
            "card": {
                "title": card.title,
                "status": card.status,
                "progress": card.progress,
                "blocker": card.blocker,
                "next_step": card.next_step,
                "outcome": (
                    card.outcome.model_dump(mode="json")
                    if card.outcome is not None
                    else None
                ),
                "participants": [
                    item.model_dump(mode="json") for item in card.participants
                ],
                "sources": [
                    {
                        "source_id": item.source_id,
                        "excerpt": item.excerpt,
                    }
                    for item in card.source_refs
                ],
            },
            "candidates": [
                item.model_dump(mode="json") for item in candidates
            ],
        }
    )
    response_schema = {
        "$id": ADJUDICATION_SCHEMA_ID,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "decision",
            "subject_key",
            "confidence",
            "evidence_source_ids",
        ],
        "properties": {
            "decision": {"enum": ["attach", "new", "abstain"]},
            "subject_key": {"type": ["string", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence_source_ids": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
        },
    }
    return AdjudicationPrompt(system, user, response_schema)


def gate_adjudication(
    raw: str,
    *,
    card: EpisodeCard,
    candidates: list[AdjudicationCandidate],
    confidence_threshold: float,
) -> AdjudicationGate:
    try:
        response = AdjudicationResponse.model_validate(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValidationError):
        return AdjudicationGate("review", None, ("MALFORMED_RESPONSE",))

    if response.decision == "abstain":
        return AdjudicationGate("review", None, ("EXPLICIT_ABSTAIN",))

    cited = {item.source_id for item in card.source_refs}
    if not set(response.evidence_source_ids).issubset(cited):
        return AdjudicationGate("review", None, ("SOURCE_NOT_TRACEABLE",))
    if response.decision == "new":
        return AdjudicationGate("new", None)

    offered = {item.subject_key for item in candidates}
    if response.subject_key not in offered:
        return AdjudicationGate("review", None, ("SUBJECT_NOT_OFFERED",))
    if not response.evidence_source_ids:
        return AdjudicationGate("review", None, ("SOURCE_NOT_TRACEABLE",))
    if response.confidence < confidence_threshold:
        return AdjudicationGate("review", None, ("LOW_CONFIDENCE",))
    return AdjudicationGate("attach", response.subject_key)


def lexical_tokens(*values: str | None) -> set[str]:
    return {
        token
        for token in normalize_title(" ".join(value or "" for value in values)).split()
        if token
    }


def candidate_score(card: EpisodeCard, candidate: AdjudicationCandidate) -> int:
    card_tokens = lexical_tokens(
        card.title,
        *(source.excerpt for source in card.source_refs),
    )
    candidate_tokens = lexical_tokens(
        candidate.title,
        *candidate.aliases,
        *(
            handle.split(":", 1)[1] if ":" in handle else handle
            for handle in candidate.handles
        ),
        candidate.recent_evidence,
    )
    return len(card_tokens & candidate_tokens)
