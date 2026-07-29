from __future__ import annotations

import json
import warnings
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import Field, ValidationError

from matterhorn.contracts import (
    EpisodeCard,
    Outcome,
    Participant,
    Record,
    SchemaProfile,
    SourceRef,
)
from matterhorn.contracts.models import StrictModel
from matterhorn.contracts.schema import resolve_schema
from matterhorn.distill.gateway import LlmGateway
from matterhorn.distill.traceability import resolve_traceable_sources
from matterhorn.engine.canonical import canonical_json, stable_hash


class ChatMessage(StrictModel):
    """Deprecated M3 input alias retained for source compatibility."""

    message_id: str
    sent_at: datetime
    sender: str
    content: str

    def as_record(self) -> Record:
        warnings.warn(
            "ChatMessage is deprecated; use matterhorn.Record",
            DeprecationWarning,
            stacklevel=2,
        )
        return Record.model_validate(
            {
                "record_id": f"legacy:{self.message_id}",
                "native_id": self.message_id,
                "container_id": "legacy",
                "sent_at": self.sent_at,
                "author": {
                    "id": self.sender,
                    "display_name": self.sender,
                    "kind": "human",
                },
                "content": self.content,
                "kind": "message",
            }
        )


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


@dataclass(frozen=True)
class _ExtractionRecord:
    record: Record
    source_id: str
    legacy: bool

    @property
    def source_ref(self) -> SourceRef:
        if not self.legacy:
            return self.record.to_source_ref()
        return SourceRef(
            source_id=self.source_id,
            sent_at=self.record.sent_at,
            sender=self.record.author.display_name or self.record.author.id,
            excerpt=self.record.content,
            uri=self.record.uri,
        )


class MessageCardExtractor:
    """Gated Record-to-card extraction. This component is write-path only."""

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
        records: list[Record | ChatMessage | dict[str, Any]] | None = None,
        messages: list[ChatMessage | dict[str, Any]] | None = None,
    ) -> MessageExtractionReport:
        if records is not None and messages is not None:
            raise ValueError("pass records or deprecated messages, not both")
        supplied = records if records is not None else messages
        if supplied is None:
            raise ValueError("records are required")
        inputs = _validate_inputs(supplied)
        if not inputs:
            return MessageExtractionReport()
        modern = any(not item.legacy for item in inputs)
        prompt = build_message_prompt(
            self.profile,
            scope_id,
            inputs,
            allow_subject_key=not modern,
        )
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

        by_source = {item.source_id: item for item in inputs}
        available_refs = [item.source_ref for item in inputs]
        accepted: list[EpisodeCard] = []
        rejected: list[MessageCardRejection] = []
        allowed_fields = _profile_card_fields(
            self.profile,
            allow_subject_key=not modern,
        )

        for index, candidate in enumerate(response.cards):
            dumped = candidate.model_dump(mode="json")
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
                candidate.source_ids,
                available_refs,
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
            if modern:
                dumped.pop("subject_key", None)
                boundaries = sorted(
                    {by_source[source_id].record.matter_boundary for source_id in candidate.source_ids}
                )
                thread_id = (
                    boundaries[0]
                    if len(boundaries) == 1
                    else f"threads:{stable_hash(boundaries)}"
                )
                dumped["thread_id"] = thread_id
            card_payload = {
                **dumped,
                "scope_id": scope_id,
                "source_refs": evidence.source_refs,
            }
            card_id_payload = {
                "schema": self.profile.schema_id,
                "scope_id": scope_id,
                "card": card_payload,
                "observations": sorted(
                    [
                        source_id,
                        stable_hash(
                            by_source[source_id].record.model_dump(mode="json")
                        ),
                    ]
                    for source_id in candidate.source_ids
                ),
            }
            try:
                card = EpisodeCard.model_validate(
                    {
                        **card_payload,
                        "card_id": f"rec_{stable_hash(card_id_payload)}",
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


RecordCardExtractor = MessageCardExtractor


def build_message_prompt(
    profile: SchemaProfile,
    scope_id: str,
    records: list[_ExtractionRecord] | list[Record] | list[ChatMessage],
    *,
    allow_subject_key: bool = True,
) -> dict[str, Any]:
    normalized = (
        records
        if not records or isinstance(records[0], _ExtractionRecord)
        else _validate_inputs(records)
    )
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
        "Convert the supplied communication Records into evidence-backed "
        "EpisodeCards. Return closed JSON only. Cite only record_id values "
        "supplied below. Omit fields not listed in active_card_fields. "
        "Empty source_ids are invalid. Thread identity is assigned by the engine. "
        f"schema={profile.schema_id}; "
        f"active_card_fields={canonical_json(fields)}"
    )
    user = canonical_json(
        {
            "scope_id": scope_id,
            "records": [
                {
                    **item.record.model_dump(mode="json"),
                    "record_id": item.source_id,
                }
                for item in normalized
            ],
        }
    )
    schema = MessageCardResponse.model_json_schema()
    properties = schema["$defs"]["MessageCardCandidate"]["properties"]
    allowed = {"date", "title", "source_ids"} | _profile_card_fields(
        profile,
        allow_subject_key=allow_subject_key,
    )
    schema["$defs"]["MessageCardCandidate"]["properties"] = {
        key: value for key, value in properties.items() if key in allowed
    }
    schema["$defs"]["MessageCardCandidate"]["required"] = [
        key
        for key in schema["$defs"]["MessageCardCandidate"].get("required", [])
        if key in allowed
    ]
    return {"system": system, "user": user, "response_schema": schema}


def _validate_inputs(
    values: list[Record | ChatMessage | dict[str, Any]],
) -> list[_ExtractionRecord]:
    result: list[_ExtractionRecord] = []
    for value in values:
        if isinstance(value, Record):
            record = value
            source_id = value.record_id
            legacy = False
        elif isinstance(value, ChatMessage):
            record = value.as_record()
            source_id = value.message_id
            legacy = True
        elif "record_id" in value:
            record = Record.model_validate(value)
            source_id = record.record_id
            legacy = False
        else:
            message = ChatMessage.model_validate(value)
            record = message.as_record()
            source_id = message.message_id
            legacy = True
        if record.revoked_at is None:
            result.append(
                _ExtractionRecord(
                    record=record,
                    source_id=source_id,
                    legacy=legacy,
                )
            )
    return result


def _profile_card_fields(
    profile: SchemaProfile,
    *,
    allow_subject_key: bool = True,
) -> set[str]:
    result = {
        predicate.source_field
        for predicate in profile.predicates
        if predicate.source_field is not None
    } | {"occurred_at", "last_active_at", "cleared_fields"}
    if allow_subject_key:
        result.add("subject_key")
    return result


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
