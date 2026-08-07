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
from matterhorn.engine.aggregate import AliasNamespace, ClosedCandidateWorld

ADJUDICATION_SCHEMA_ID = "matterhorn-identity-adjudication/v1"
PINNED_ADJUDICATION_EXAMPLE = (
    '{"decision":"attach","subject_key":"matter-204","confidence":0.86,'
    '"evidence_source_ids":["m1"]}'
)


def adjudication_source_aliases(card: EpisodeCard) -> dict[str, str]:
    """Deterministic alias -> source_id map in cited-source order.

    Long record ids get mangled when a model copies them; short aliases do
    not. The gate accepts either form.
    """

    return AliasNamespace.sequential(
        (ref.source_id for ref in card.source_refs),
        prefix="m",
    ).as_dict()


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
    subject_key: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence_source_ids: list[str]

    @field_validator("evidence_source_ids")
    @classmethod
    def evidence_ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("evidence_source_ids MUST be unique")
        return value

    @model_validator(mode="after")
    def non_attach_ignores_subject(self) -> AdjudicationResponse:
        # Live incident: the adjudicator habitually fills subject_key on
        # abstain (or omits the field). Both are still safe abstentions —
        # rejecting them as MALFORMED only mislabels the review queue.
        # Normalize instead of refusing; attach semantics stay strict.
        if self.decision != "attach" and self.subject_key is not None:
            object.__setattr__(self, "subject_key", None)
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
    *,
    aliases: AliasNamespace | None = None,
) -> AdjudicationPrompt:
    alias_map = (
        aliases.as_dict()
        if aliases is not None
        else adjudication_source_aliases(card)
    )
    excerpt_by_id = {ref.source_id: ref.excerpt for ref in card.source_refs}
    system = (
        "You adjudicate whether one evidence-backed card belongs to one offered "
        "open matter. Use only the offered candidates. Cite evidence by the "
        "short source_alias values (m1, m2, ...) shown with the card's "
        "sources. Return exactly one closed JSON object. When the card is "
        "clearly a topic no candidate covers, answer new with confidence - "
        "new is the safe default for novel topics. Abstain ONLY when you are "
        "genuinely torn between candidates. Literal example: "
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
                        "source_alias": alias,
                        "excerpt": excerpt_by_id[source_id],
                    }
                    for alias, source_id in alias_map.items()
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
    world: ClosedCandidateWorld | None = None,
    aliases: AliasNamespace | None = None,
) -> AdjudicationGate:
    try:
        response = AdjudicationResponse.model_validate(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValidationError):
        return AdjudicationGate("review", None, ("MALFORMED_RESPONSE",))

    if response.decision == "abstain":
        return AdjudicationGate("review", None, ("EXPLICIT_ABSTAIN",))

    alias_namespace = aliases or AliasNamespace.from_mapping(
        adjudication_source_aliases(card)
    )
    cited = {item.source_id for item in card.source_refs}
    resolved = alias_namespace.resolve_all(response.evidence_source_ids)
    if not set(resolved).issubset(cited):
        return AdjudicationGate("review", None, ("SOURCE_NOT_TRACEABLE",))
    if response.decision == "new":
        return AdjudicationGate("new", None)

    closed_world = world or ClosedCandidateWorld.from_keys(
        item.subject_key for item in candidates
    )
    if not closed_world.contains(response.subject_key):
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
