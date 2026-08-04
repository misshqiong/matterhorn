from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from matterhorn.contracts import HandlePattern, SchemaProfile, SourceRef


@dataclass(frozen=True)
class HandleMatch:
    handle_type: str
    handle_value: str
    normalized_value: str
    source_refs: list[SourceRef]


def handle_pattern(profile: SchemaProfile, handle_type: str) -> HandlePattern:
    for pattern in profile.handle_patterns:
        if pattern.handle_type == handle_type:
            return pattern
    raise ValueError(f"unregistered handle type: {handle_type}")


def normalize_handle(
    profile: SchemaProfile,
    handle_type: str,
    value: str,
) -> str:
    definition = handle_pattern(profile, handle_type)
    normalized = _strip_surrounding(value.strip())
    for prefix in definition.normalization.strip_prefixes:
        if _has_ascii_prefix(normalized, prefix):
            normalized = normalized[len(prefix) :].lstrip()
            normalized = _strip_surrounding(normalized)
            break
    normalized = re.sub(r"^[#!]+\s*", "", normalized)
    normalized = _ascii_lower(normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if definition.normalization.strip_leading_zeros:
        normalized = re.sub(
            r"(?<!\w)[0-9]+(?!\w)",
            lambda match: str(int(match.group(0))),
            normalized,
        )
    if not normalized:
        raise ValueError("normalized handle value MUST be non-empty")
    return normalized


def matches_handle_pattern(
    profile: SchemaProfile,
    handle_type: str,
    value: str,
) -> bool:
    definition = handle_pattern(profile, handle_type)
    candidate = value.strip()
    return any(
        not _strip_surrounding(candidate[: match.start()])
        and not _strip_surrounding(candidate[match.end() :])
        for match in re.finditer(definition.pattern, candidate)
    )


def scan_handles(
    profile: SchemaProfile,
    text: str,
    source_refs: list[SourceRef],
) -> list[HandleMatch]:
    matches: list[HandleMatch] = []
    for definition in profile.handle_patterns:
        for match in re.finditer(definition.pattern, text):
            surface = match.group(0)
            matches.append(
                HandleMatch(
                    handle_type=definition.handle_type,
                    handle_value=surface,
                    normalized_value=normalize_handle(
                        profile,
                        definition.handle_type,
                        surface,
                    ),
                    source_refs=source_refs,
                )
            )
    return matches


def _strip_surrounding(value: str) -> str:
    result = value.strip()
    while result:
        if _is_punctuation_or_symbol(result[0]):
            result = result[1:].strip()
            continue
        if _is_punctuation_or_symbol(result[-1]):
            result = result[:-1].strip()
            continue
        break
    return result


def _is_punctuation_or_symbol(value: str) -> bool:
    return unicodedata.category(value).startswith(("P", "S"))


def _ascii_lower(value: str) -> str:
    return "".join(
        chr(ord(char) + 32) if "A" <= char <= "Z" else char
        for char in value
    )


def _has_ascii_prefix(value: str, prefix: str) -> bool:
    if _ascii_lower(value[: len(prefix)]) != _ascii_lower(prefix):
        return False
    if len(value) == len(prefix):
        return True
    following = value[len(prefix)]
    return following.isspace() or _is_punctuation_or_symbol(following)
