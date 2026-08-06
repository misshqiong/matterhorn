from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from matterhorn.canonical import canonical_json
from matterhorn.cli.app import _unified_loop_setting
from matterhorn.conformance import FixtureGateway
from matterhorn.contracts import EpisodeCard, Record, SourceRef
from matterhorn.defaults import Engine
from matterhorn.engine.unified_loop import UnifiedLoopSession


def _record(record_id: str = "octo-room:r1") -> Record:
    return Record.model_validate(
        {
            "record_id": record_id,
            "container_id": "octo-room",
            "sent_at": "2026-08-06T09:00:00Z",
            "author": {
                "id": "dana-reyes",
                "display_name": "Dana Reyes",
                "kind": "human",
            },
            "content": "Open the fictional compatibility audit.",
            "kind": "im",
        }
    )


def _gateway(turns: list[dict]) -> FixtureGateway:
    return FixtureGateway(
        extraction=[],
        adjudication=[],
        semantic=[],
        tool_loop=[{"turns": turns}],
    )


def test_closed_world_rejects_one_intent_without_aborting_valid_peer(tmp_path) -> None:
    gateway = _gateway(
        [
            {
                "tool_call": {
                    "name": "emit",
                    "arguments": {
                        "assertions": [
                            {
                                "subject": {"subject_key": "hidden-root"},
                                "predicate": "progress",
                                "operation": "ASSERT",
                                "object_value": "Must be rejected.",
                                "evidence_aliases": ["m1"],
                            },
                            {
                                "subject": {
                                    "new_subject": {
                                        "ref": "audit",
                                        "subject_type": "MATTER",
                                        "title": "Fictional compatibility audit",
                                    }
                                },
                                "predicate": "status",
                                "operation": "ASSERT",
                                "object_value": "open",
                                "evidence_aliases": ["m1"],
                            },
                        ]
                    },
                }
            },
            {"final_message": "done"},
        ]
    )
    engine = Engine(
        tmp_path / "closed.db",
        gateway=gateway,
        unified_loop=True,
        clock=lambda: datetime(2026, 8, 6, 10, tzinfo=UTC),
    )
    engine._ingest_cards_sync(
        [
            EpisodeCard(
                card_id="seed",
                scope_id="scope",
                subject_key="hidden-root",
                date=date(2026, 8, 6),
                title="Hidden fictional root",
                status="open",
                source_refs=[
                    SourceRef(
                        source_id="octo-seed:r0",
                        sent_at=datetime(2026, 8, 6, 8, tzinfo=UTC),
                        sender="Dana Reyes",
                    )
                ],
            )
        ],
        scope_id="scope",
    )

    report = engine.add_records([_record()], scope_id="scope")

    assert report.assertions_emitted == 1
    assert report.drop_reasons == {"CLOSED_WORLD_VIOLATION": 1}
    assertions = engine.store.assertions("scope")
    assert any(item.predicate == "status" and item.subject_key != "hidden-root" for item in assertions)
    assert not any(item.object_value == "Must be rejected." for item in assertions)
    created = next(
        subject
        for subject in engine.store.subjects("scope")
        if subject.subject_key != "hidden-root"
    )
    assert created.source_ids == frozenset({"octo-room:r1"})


def test_emit_batch_rolls_back_subject_and_assertions_on_store_failure(
    tmp_path, monkeypatch
) -> None:
    engine = Engine(
        tmp_path / "atomic.db",
        unified_loop=True,
        clock=lambda: datetime(2026, 8, 6, 10, tzinfo=UTC),
    )
    session = UnifiedLoopSession(
        engine=engine,
        scope_id="scope",
        records=[_record()],
        context=[],
    )
    original = engine.store.add_assertion
    calls = 0

    def fail_second(assertion):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fictional store failure")
        return original(assertion)

    monkeypatch.setattr(engine.store, "add_assertion", fail_second)
    arguments = {
        "assertions": [
            {
                "subject": {
                    "new_subject": {
                        "ref": "audit",
                        "subject_type": "MATTER",
                        "title": "Fictional atomic audit",
                    }
                },
                "predicate": "status",
                "operation": "ASSERT",
                "object_value": "open",
                "evidence_aliases": ["m1"],
            },
            {
                "subject": {
                    "new_subject": {
                        "ref": "audit",
                        "subject_type": "MATTER",
                        "title": "Fictional atomic audit",
                    }
                },
                "predicate": "progress",
                "operation": "ASSERT",
                "object_value": "Started.",
                "evidence_aliases": ["m1"],
            },
        ]
    }

    with pytest.raises(RuntimeError, match="fictional store failure"):
        session.handle_tool("emit", arguments)

    assert engine.store.subjects("scope") == []
    assert engine.store.assertions("scope") == []
    assert session.state.assertion_ids == []
    assert session.state.assertion_aliases == {}


class _LegacyGateway:
    def complete(self, *, system, user, response_schema):
        del system
        properties = response_schema.get("properties", {})
        if "cards" in properties:
            alias = json.loads(user)["records"][0]["source_alias"]
            return json.dumps(
                {
                    "cards": [
                        {
                            "date": "2026-08-06",
                            "title": "Fictional equivalence audit",
                            "status": "open",
                            "source_ids": [alias],
                        }
                    ]
                }
            )
        if "candidates" in properties:
            return '{"candidates":[]}'
        raise AssertionError(response_schema)


def test_explicit_false_is_byte_equivalent_to_default_legacy_path(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("MATTERHORN_UNIFIED_LOOP", raising=False)
    clock = lambda: datetime(2026, 8, 6, 10, tzinfo=UTC)
    default = Engine(tmp_path / "default.db", gateway=_LegacyGateway(), clock=clock)
    explicit = Engine(
        tmp_path / "explicit.db",
        gateway=_LegacyGateway(),
        clock=clock,
        unified_loop=False,
    )

    default_report = default.add_records([_record()], scope_id="scope")
    explicit_report = explicit.add_records([_record()], scope_id="scope")

    assert canonical_json(default_report.model_dump(mode="json")) == canonical_json(
        explicit_report.model_dump(mode="json")
    )
    assert canonical_json(default.export("scope").model_dump(mode="json")) == canonical_json(
        explicit.export("scope").model_dump(mode="json")
    )


def test_distill_config_defaults_off_and_environment_overrides(monkeypatch) -> None:
    monkeypatch.delenv("MATTERHORN_UNIFIED_LOOP", raising=False)
    assert _unified_loop_setting({}) is False
    assert _unified_loop_setting({"distill": {"unified_loop": True}}) is True

    monkeypatch.setenv("MATTERHORN_UNIFIED_LOOP", "off")
    assert _unified_loop_setting({"distill": {"unified_loop": True}}) is False

    monkeypatch.setenv("MATTERHORN_UNIFIED_LOOP", "maybe")
    with pytest.raises(Exception, match="unified_loop"):
        _unified_loop_setting({})


def test_prompt_injects_only_source_kind_alignment_sample(tmp_path) -> None:
    engine = Engine(tmp_path / "samples.db", unified_loop=True)
    session = UnifiedLoopSession(
        engine=engine,
        scope_id="scope",
        records=[_record()],
        context=[],
    )

    system = session._prompt()["system"]

    assert "im-subgoal-fork" in system
    assert "mail-matter-update" not in system
    assert "agent-noise" not in system


def test_scripted_gateway_stops_before_seventeenth_tool_call() -> None:
    gateway = _gateway(
        [
            {
                "tool_call": {
                    "name": "search_candidates",
                    "arguments": {"text": f"fictional query {index}"},
                }
            }
            for index in range(17)
        ]
    )
    handled: list[str] = []

    result = gateway.tool_loop(
        system="system",
        user="user",
        tools=[],
        handler=lambda name, _arguments: handled.append(name) or {},
    )

    assert result.exhausted
    assert result.tool_calls == 16
    assert result.emissions == 0
    assert handled == ["search_candidates"] * 16
