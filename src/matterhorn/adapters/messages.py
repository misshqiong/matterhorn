from __future__ import annotations

import json
import warnings
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import Field, ValidationError, field_validator

from matterhorn.canonical import canonical_json, stable_hash
from matterhorn.contracts import (
    EpisodeCard,
    Outcome,
    Participant,
    Record,
    SchemaProfile,
    SourceRef,
    SubjectAnchor,
)
from matterhorn.contracts.models import StrictModel
from matterhorn.contracts.schema import resolve_schema
from matterhorn.distill.gateway import LlmGateway
from matterhorn.distill.traceability import (
    resolve_traceable_sources,
    restore_source_aliases,
    source_aliases,
)

DEFAULT_EXTRACTION_BATCH_SIZE = 5


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

    @field_validator("date", mode="before")
    @classmethod
    def date_accepts_model_datetime_drift(cls, value: Any) -> Any:
        # Models occasionally emit the record timestamp ("2026-07-30T10:00:00Z")
        # for the date field; keep the calendar date instead of dropping the
        # whole batch as UNPARSEABLE.
        if isinstance(value, str) and "T" in value:
            candidate = value.split("T", 1)[0]
            try:
                return date.fromisoformat(candidate)
            except ValueError:
                return value
        return value


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
        context: list[Record | dict[str, Any]] | None = None,
        batch_size: int = DEFAULT_EXTRACTION_BATCH_SIZE,
        anchors: list[SubjectAnchor] | None = None,
    ) -> MessageExtractionReport:
        if records is not None and messages is not None:
            raise ValueError("pass records or deprecated messages, not both")
        supplied = records if records is not None else messages
        if supplied is None:
            raise ValueError("records are required")
        if batch_size < 1:
            raise ValueError("batch_size MUST be positive")
        inputs = _validate_inputs(supplied)
        context_inputs = _validate_inputs(context or [])
        offered_anchors = list(anchors or [])
        if not inputs:
            return MessageExtractionReport()
        cards: list[EpisodeCard] = []
        rejections: list[MessageCardRejection] = []
        for batch in _thread_batches(inputs, batch_size):
            report = self._extract_batch(
                scope_id=scope_id,
                inputs=batch,
                context=context_inputs,
                anchors=offered_anchors,
            )
            cards.extend(report.cards)
            rejections.extend(report.rejections)
        return MessageExtractionReport(cards=cards, rejections=rejections)

    def _extract_batch(
        self,
        *,
        scope_id: str,
        inputs: list[_ExtractionRecord],
        context: list[_ExtractionRecord],
        anchors: list[SubjectAnchor],
    ) -> MessageExtractionReport:
        all_inputs = [*context, *inputs]
        modern = any(not item.legacy for item in all_inputs)
        prompt = build_message_prompt(
            self.profile,
            scope_id,
            inputs,
            context=context,
            anchors=anchors,
            allow_subject_key=not modern or bool(anchors),
        )
        raw = self.gateway.complete(
            system=prompt["system"],
            user=prompt["user"],
            response_schema=prompt["response_schema"],
        )
        try:
            decoded = json.loads(raw)
            restored = restore_source_aliases(
                decoded,
                collection_key="cards",
                aliases=prompt["source_aliases"],
                available_source_ids=(item.source_id for item in all_inputs),
            )
            response = MessageCardResponse.model_validate(restored)
        except (json.JSONDecodeError, ValidationError, TypeError) as error:
            return MessageExtractionReport(
                rejections=[
                    MessageCardRejection(
                        reason=MessageDropReason.UNPARSEABLE,
                        detail=str(error),
                    )
                ]
            )

        by_source = {item.source_id: item for item in all_inputs}
        available_refs = [item.source_ref for item in all_inputs]
        accepted: list[EpisodeCard] = []
        rejected: list[MessageCardRejection] = []
        allowed_fields = _profile_card_fields(
            self.profile,
            # Modern subject keys are decoded so the deterministic anchor gate
            # can silently discard fabricated values.
            allow_subject_key=True,
        )
        anchor_keys = {anchor.subject_key for anchor in anchors}

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
                dumped["subject_key"] = (
                    candidate.subject_key
                    if candidate.subject_key in anchor_keys
                    else None
                )
                boundaries = sorted(
                    {
                        by_source[source_id].record.matter_boundary
                        for source_id in candidate.source_ids
                    }
                )
                thread_id = (
                    boundaries[0]
                    if len(boundaries) == 1
                    else f"threads:{stable_hash(boundaries)}"
                )
                dumped["thread_id"] = thread_id
                mail_subject_key = _mail_subject_key(
                    by_source=by_source,
                    source_ids=candidate.source_ids,
                )
                if mail_subject_key is not None:
                    dumped["subject_key"] = mail_subject_key
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
    context: list[_ExtractionRecord] | list[Record] | None = None,
    anchors: list[SubjectAnchor] | None = None,
    allow_subject_key: bool = True,
) -> dict[str, Any]:
    normalized = (
        records
        if not records or isinstance(records[0], _ExtractionRecord)
        else _validate_inputs(records)
    )
    normalized_context = (
        context
        if not context or isinstance(context[0], _ExtractionRecord)
        else _validate_inputs(context)
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
        "EpisodeCards. Return closed JSON only. The top-level key MUST be "
        '"cards" (exactly), holding an array of card objects, e.g. '
        '{"cards":[{"date":"2026-01-31","title":"...","status":"open",'
        '"participants":[{"id":"u1","display_name":"..."}],'
        '"outcome":{"type":"...","content":"..."},"source_ids":["m1"]}]}. '
        "Use exactly these field names; any other envelope key or field "
        "shape is rejected. Each Record has a short "
        "source_alias. Cite only those aliases (m1, m2, ...) in source_ids; "
        "never cite a scope, container, thread, native id, or URI. "
        "Omit fields not listed in active_card_fields. "
        "Empty source_ids are invalid. Thread identity is assigned by the engine. "
        "A card describes one underlying matter end to end. Investigating, "
        "diagnosing, developing, fixing, testing, verifying, accepting, "
        "submitting, deploying, paying, and adjusting or rescheduling are "
        "lifecycle phases of ONE underlying task, not separate matters. One "
        "purchase, one incident, and one change request each stay a single "
        "matter from start to finish. Emit separate cards only for genuinely "
        "different tasks. "
        f"schema={profile.schema_id}; "
        f"active_card_fields={canonical_json(fields)}"
    )
    if any(_is_email_record(item.record) for item in normalized):
        system += (
            " For email, aggregate all Records in each conversation before "
            "deriving its progress. Emit one card per conversation-level update, "
            "not one card per email. Title the underlying matter; a cleaned "
            "subject is a good default, and reply/forward prefixes such as Re: "
            "or 回复: MUST NOT be repeated in the title. Derive status only "
            "when the aggregated conversation supports it; do not default "
            "status to done."
        )
    if normalized_context:
        system += (
            " Prior conversation context, oldest first. Use it to understand "
            "what the new Records continue. You MAY cite a context Record's "
            "alias in source_ids when a card's claim rests on it."
        )
    offered_anchors = list(anchors or [])
    if offered_anchors:
        system += (
            " Known open matters: "
            f"{canonical_json([anchor.model_dump(mode='json') for anchor in offered_anchors])}. "
            "Attach a card to a known matter by copying its exact subject_key "
            "when the Records carry linking signals to it: shared identifiers "
            "such as order numbers, ticket, flight, or issue IDs, amounts, or "
            "names; a complementary status transition such as an anchor "
            "awaiting payment and Records reporting payment success; close "
            "time proximity plus the same topic; or lifecycle continuation. "
            "Attachment is an assertion and needs evidence. If the Records "
            "carry NO linking signal to any known matter, do NOT attach: emit "
            "the card without subject_key. A separate card is cheap to merge "
            "later; a wrong attachment corrupts two timelines. When one batch "
            "touches several known matters, emit several cards and make each "
            "card cite only its own source_ids. Keep the closed envelope "
            "exactly like this "
            "literal example, where the first card continues a known matter "
            "and the second is a new unrelated matter: "
            '{"cards":[{"date":"2026-01-31","title":"Release readiness",'
            '"status":"in_progress","subject_key":"sub_known_release",'
            '"source_ids":["m1"]},{"date":"2026-01-31",'
            '"title":"Gateway H2 research","status":"open",'
            '"source_ids":["m2"]}]}.'
        )
    all_inputs = [*normalized_context, *normalized]
    aliases = source_aliases(item.source_id for item in all_inputs)
    alias_names = list(aliases)
    context_aliases = alias_names[: len(normalized_context)]
    record_aliases = alias_names[len(normalized_context) :]
    user_payload = {
        "scope_id": scope_id,
        "records": _prompt_records(record_aliases, normalized),
    }
    if normalized_context:
        user_payload["context"] = _prompt_records(
            context_aliases,
            normalized_context,
        )
    user = canonical_json(user_payload)
    schema = MessageCardResponse.model_json_schema()
    properties = schema["$defs"]["MessageCardCandidate"]["properties"]
    allowed = {"date", "title", "source_ids"} | _profile_card_fields(
        profile,
        allow_subject_key=allow_subject_key or bool(offered_anchors),
    )
    schema["$defs"]["MessageCardCandidate"]["properties"] = {
        key: value for key, value in properties.items() if key in allowed
    }
    schema["$defs"]["MessageCardCandidate"]["required"] = [
        key
        for key in schema["$defs"]["MessageCardCandidate"].get("required", [])
        if key in allowed
    ]
    return {
        "system": system,
        "user": user,
        "response_schema": schema,
        "source_aliases": aliases,
    }


def _prompt_records(
    aliases: list[str],
    records: list[_ExtractionRecord],
) -> list[dict[str, Any]]:
    return [
        {
            "source_alias": alias,
            "record": {
                key: value
                for key, value in item.record.model_dump(mode="json").items()
                if key not in {"record_id", "native_id"}
            },
        }
        for alias, item in zip(aliases, records, strict=True)
    ]


def _thread_batches(
    records: list[_ExtractionRecord],
    batch_size: int,
) -> list[list[_ExtractionRecord]]:
    """Pack complete matter boundaries without splitting a thread."""

    grouped: dict[str, list[_ExtractionRecord]] = {}
    for item in records:
        grouped.setdefault(item.record.matter_boundary, []).append(item)

    batches: list[list[_ExtractionRecord]] = []
    current: list[_ExtractionRecord] = []
    for thread in grouped.values():
        if len(thread) > batch_size:
            if current:
                batches.append(current)
                current = []
            batches.append(thread)
            continue
        if current and len(current) + len(thread) > batch_size:
            batches.append(current)
            current = []
        current.extend(thread)
    if current:
        batches.append(current)
    return batches


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


def _mail_subject_key(
    *,
    by_source: dict[str, _ExtractionRecord],
    source_ids: list[str],
) -> str | None:
    cited = [by_source[source_id].record for source_id in source_ids]
    if not cited or not all(_is_email_record(record) for record in cited):
        return None
    containers = {record.container_id for record in cited}
    boundaries = {record.thread_id for record in cited}
    if len(containers) != 1 or len(boundaries) != 1 or None in boundaries:
        return None
    container_id = next(iter(containers))
    thread_id = next(iter(boundaries))
    assert thread_id is not None
    prefix = f"{container_id}:"
    if not thread_id.startswith(prefix):
        return None
    conversation_key = thread_id.removeprefix(prefix)
    return f"mail:{stable_hash([container_id, conversation_key])[:20]}"


def _is_email_record(record: Record) -> bool:
    return record.subtype == "email" or ":mail:" in record.container_id
