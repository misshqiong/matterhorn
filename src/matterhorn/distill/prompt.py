from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from matterhorn.canonical import canonical_json
from matterhorn.contracts import EpisodeCard, ExtractionMode, SchemaProfile
from matterhorn.distill.traceability import source_aliases


@dataclass(frozen=True)
class PromptContract:
    system: str
    user: str
    response_schema: dict[str, Any]
    source_aliases: dict[str, str]


def candidate_response_schema(profile: SchemaProfile) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "subject_type",
                        "predicate",
                        "operation",
                        "object_value",
                        "valid_from",
                        "source_ids",
                        "confidence",
                    ],
                    "properties": {
                        "subject_key": {"type": ["string", "null"]},
                        "subject_type": {"type": "string"},
                        "parent_subject_key": {"type": ["string", "null"]},
                        "subject_title": {"type": ["string", "null"]},
                        "predicate": {"type": "string"},
                        "operation": {
                            "type": "string",
                            "enum": ["ASSERT", "RETRACT"],
                        },
                        "object_value": {},
                        "valid_from": {"type": "string", "format": "date-time"},
                        "source_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "uniqueItems": True,
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                },
            }
        },
    }


def build_prompt(
    profile: SchemaProfile,
    card: EpisodeCard,
    *,
    subject_key: str,
    subject_type: str,
) -> PromptContract:
    predicates = [
        {
            "name": predicate.name,
            "subject_type": predicate.subject,
            "object_type": predicate.object,
            "value_domain": predicate.value_domain,
            "field_domains": predicate.field_domains,
        }
        for predicate in profile.predicates
        if predicate.extraction == ExtractionMode.semantic
    ]
    subjects = [
        {
            "type": subject.type,
            "parent": subject.parent,
            "primary": subject.type == profile.primary_subject.type,
        }
        for subject in profile.subjects
    ]
    aliases = source_aliases(ref.source_id for ref in card.source_refs)
    card_payload = card.model_dump(mode="json")
    card_payload["source_refs"] = [
        {
            **ref,
            "source_id": alias,
        }
        for alias, ref in zip(
            aliases,
            card_payload["source_refs"],
            strict=True,
        )
    ]
    system = (
        "Extract only semantic assertions registered below. "
        "Each supplied source_ref uses a short source alias. Use only those "
        "aliases (m1, m2, ...) in source_ids; never cite a scope, card, or URI. "
        "Return JSON only. The top-level key MUST be \"candidates\" "
        "(exactly), holding an array of candidate objects, e.g. "
        '{"candidates":[{"subject_key":"sub_...","subject_type":"...",'
        '"predicate":"...","operation":"ASSERT","object_value":true,'
        '"valid_from":"2026-01-31T00:00:00Z","source_ids":["m1"],'
        '"confidence":0.9}]}. Use exactly these field names; any other '
        "envelope key or field shape is rejected. "
        "If nothing qualifies, return {\"candidates\":[]}. "
        "When targeting an existing subject, set subject_key. To create a "
        "declared non-primary child, set parent_subject_key and subject_title; "
        "the engine derives subject_key. When uncertain, emit no candidate.\n"
        f"registered_subjects={canonical_json(subjects)}\n"
        f"registered_semantic_predicates={canonical_json(predicates)}"
    )
    user = canonical_json(
        {
            "resolved_subject": {
                "subject_key": subject_key,
                "subject_type": subject_type,
            },
            "episode_card": card_payload,
        }
    )
    return PromptContract(
        system=system,
        user=user,
        response_schema=candidate_response_schema(profile),
        source_aliases=aliases,
    )
