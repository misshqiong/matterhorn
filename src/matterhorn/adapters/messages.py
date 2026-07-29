from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import Field, ValidationError

from matterhorn.contracts import (
    EpisodeCard,
    Outcome,
    Participant,
    SchemaProfile,
    SourceRef,
)
from matterhorn.contracts.models import StrictModel
from matterhorn.contracts.schema import resolve_schema
from matterhorn.distill.gateway import LlmGateway
from matterhorn.distill.traceability import resolve_traceable_sources
from matterhorn.engine.canonical import canonical_json, stable_hash


class ChatMessage(StrictModel):
    message_id: str
    sent_at: datetime
    sender: str
    content: str


class MessageCardCandidate(StrictModel):
    date: date
    title: str
    status: str | None = None
    participants: list[Participant] = Field(default_factory=list)
    progress: str | None = None
    blocker: str | None = None
    next_step: str | None = None
    due: datetime | None = None
    outcome: Outcome | None = None
    occurred_at: datetime | None = None
    last_active_at: datetime | None = None
    source_ids: list[str]
    cleared_fields: list[str] = Field(default_factory=list)
    subject_key: str | None = None


class MessageCardResponse(StrictModel):
    cards: list[MessageCardCandidate]


class MessageDropReason(str, Enum):
    UNPARSEABLE = "UNPARSEABLE"
    NO_SOURCES = "NO_SOURCES"
    SOURCE_NOT_TRACEABLE = "SOURCE_NOT_TRACEABLE"
    FIELD_NOT_IN_PROFILE = "FIELD_NOT_IN_PROFILE"
    VALUE_OUT_OF_DOMAIN = "VALUE_OUT_OF_DOMAIN"
    CARD_VALIDATION_FAILED = "CARD_VALIDATION_FAILED"


class MessageCardRejection(StrictModel):
    reason: MessageDropReason
    candidate_index: int | None = None
    detail: str | None = None


class MessageExtractionReport(StrictModel):
    cards: list[EpisodeCard] = Field(default_factory=list)
    rejections: list[MessageCardRejection] = Field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return len(self.cards)

    @property
    def rejected_count(self) -> int:
        return len(self.rejections)

    @property
    def rejection_counts(self) -> dict[str, int]:
        return dict(Counter(item.reason.value for item in self.rejections))


class MessageCardExtractor:
    """Gated message-to-card extraction. This component is write-path only."""

    def __init__(
        self,
        gateway: LlmGateway,
        schema: str | SchemaProfile,
    ) -> None:
        self.gateway = gateway
        self.profile = resolve_schema(schema)

    def extract(
        self,
        *,
        scope_id: str,
        messages: list[ChatMessage | dict[str, Any]],
    ) -> MessageExtractionReport:
        validated_messages = [
            item if isinstance(item, ChatMessage) else ChatMessage.model_validate(item)
            for item in messages
        ]
        prompt = build_message_prompt(self.profile, scope_id, validated_messages)
        raw = self.gateway.complete(
            system=prompt["system"],
            user=prompt["user"],
            response_schema=prompt["response_schema"],
        )
        try:
            response = MessageCardResponse.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError, TypeError) as error:
            return MessageExtractionReport(
                rejections=[
                    MessageCardRejection(
                        reason=MessageDropReason.UNPARSEABLE,
                        detail=str(error),
                    )
                ]
            )

        available_refs = [
            SourceRef(
                source_id=item.message_id,
                sent_at=item.sent_at,
                sender=item.sender,
                excerpt=item.content,
            )
            for item in validated_messages
        ]
        accepted: list[EpisodeCard] = []
        rejected: list[MessageCardRejection] = []
        input_fingerprint = stable_hash(
            {
                "schema": self.profile.schema_id,
                "scope_id": scope_id,
                "messages": [
                    item.model_dump(mode="json") for item in validated_messages
                ],
            }
        )
        allowed_fields = _profile_card_fields(self.profile)

        for index, candidate in enumerate(response.cards):
            dumped = candidate.model_dump(mode="python")
            supplied_fields = {
                field
                for field in candidate.model_fields_set
                if field not in {"date", "title", "source_ids"}
            }
            disallowed = sorted(supplied_fields - allowed_fields)
            if disallowed:
                rejected.append(
                    MessageCardRejection(
                        reason=MessageDropReason.FIELD_NOT_IN_PROFILE,
                        candidate_index=index,
                        detail=", ".join(disallowed),
                    )
                )
                continue
            evidence = resolve_traceable_sources(
                candidate.source_ids, available_refs
            )
            if evidence.failure is not None:
                rejected.append(
                    MessageCardRejection(
                        reason=MessageDropReason(evidence.failure.value),
                        candidate_index=index,
                    )
                )
                continue
            domain_error = _value_domain_error(candidate, self.profile)
            if domain_error is not None:
                rejected.append(
                    MessageCardRejection(
                        reason=MessageDropReason.VALUE_OUT_OF_DOMAIN,
                        candidate_index=index,
                        detail=domain_error,
                    )
                )
                continue
            dumped.pop("source_ids")
            try:
                card = EpisodeCard.model_validate(
                    {
                        **dumped,
                        "card_id": f"msg_{stable_hash([input_fingerprint, index])}",
                        "scope_id": scope_id,
                        "source_refs": evidence.source_refs,
                    }
                )
            except ValidationError as error:
                rejected.append(
                    MessageCardRejection(
                        reason=MessageDropReason.CARD_VALIDATION_FAILED,
                        candidate_index=index,
                        detail=str(error),
                    )
                )
                continue
            accepted.append(card)
        return MessageExtractionReport(cards=accepted, rejections=rejected)


def build_message_prompt(
    profile: SchemaProfile,
    scope_id: str,
    messages: list[ChatMessage],
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for predicate in profile.predicates:
        if predicate.source_field is None:
            continue
        fields.setdefault(
            predicate.source_field,
            {
                "predicates": [],
                "value_domain": predicate.value_domain,
                "object": predicate.object,
            },
        )
        fields[predicate.source_field]["predicates"].append(predicate.name)
    system = (
        "Convert the supplied chat window into evidence-backed EpisodeCards. "
        "Return closed JSON only. Cite only message_id values supplied below. "
        "Omit fields not listed in active_card_fields. Empty source_ids are invalid. "
        f"schema={profile.schema_id}; "
        f"active_card_fields={canonical_json(fields)}"
    )
    user = canonical_json(
        {
            "scope_id": scope_id,
            "messages": [item.model_dump(mode="json") for item in messages],
        }
    )
    schema = MessageCardResponse.model_json_schema()
    properties = schema["$defs"]["MessageCardCandidate"]["properties"]
    allowed = {"date", "title", "source_ids"} | _profile_card_fields(profile)
    schema["$defs"]["MessageCardCandidate"]["properties"] = {
        key: value for key, value in properties.items() if key in allowed
    }
    schema["$defs"]["MessageCardCandidate"]["required"] = [
        key
        for key in schema["$defs"]["MessageCardCandidate"].get("required", [])
        if key in allowed
    ]
    return {"system": system, "user": user, "response_schema": schema}


def _profile_card_fields(profile: SchemaProfile) -> set[str]:
    return {
        predicate.source_field
        for predicate in profile.predicates
        if predicate.source_field is not None
    } | {"occurred_at", "last_active_at", "subject_key", "cleared_fields"}


def _value_domain_error(
    candidate: MessageCardCandidate,
    profile: SchemaProfile,
) -> str | None:
    dumped = candidate.model_dump(mode="python")
    for predicate in profile.predicates:
        if predicate.source_field is None or predicate.value_domain is None:
            continue
        value = dumped.get(predicate.source_field)
        if value is not None and canonical_json(value) not in {
            canonical_json(item) for item in predicate.value_domain
        }:
            return f"{predicate.source_field} is outside {predicate.value_domain!r}"
    return None
