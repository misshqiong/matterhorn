from __future__ import annotations

from typing import Any


def console_group_patterns(config: dict[str, Any]) -> dict[str, list[str]]:
    console = config.get("console", {})
    if console is None:
        return {}
    if not isinstance(console, dict):
        raise TypeError("[console] MUST be a TOML table")
    groups = console.get("groups", {})
    if groups is None:
        return {}
    if not isinstance(groups, dict):
        raise TypeError("[console.groups] MUST be a TOML table")

    result: dict[str, list[str]] = {}
    for group, raw_patterns in groups.items():
        if not isinstance(group, str) or not group:
            raise TypeError("console group names MUST be non-empty strings")
        if not isinstance(raw_patterns, list) or not all(
            isinstance(pattern, str) and pattern for pattern in raw_patterns
        ):
            raise TypeError(
                f"console group {group!r} MUST contain an array of scope patterns"
            )
        for pattern in raw_patterns:
            if "*" in pattern[:-1] or pattern.count("*") > 1:
                raise ValueError(
                    f"console scope pattern {pattern!r} may use '*' only once, as a suffix"
                )
        result[group] = list(raw_patterns)
    return result


def resolve_console_groups(
    patterns: dict[str, list[str]], scopes: list[str]
) -> tuple[dict[str, list[str]], dict[str, str]]:
    ordered_scopes = sorted(scopes, key=lambda value: value.encode("utf-8"))
    assigned: dict[str, str] = {}
    for scope in ordered_scopes:
        for group, group_patterns in patterns.items():
            if any(_scope_matches(scope, pattern) for pattern in group_patterns):
                assigned[scope] = group
                break
        else:
            assigned[scope] = "other"

    resolved = {
        group: [scope for scope in ordered_scopes if assigned[scope] == group]
        for group in patterns
        if group != "other"
    }
    other = [scope for scope in ordered_scopes if assigned[scope] == "other"]
    if "other" in patterns or other:
        resolved["other"] = other
    return resolved, assigned


def _scope_matches(scope: str, pattern: str) -> bool:
    if pattern.endswith("*"):
        return scope.startswith(pattern[:-1])
    return scope == pattern
