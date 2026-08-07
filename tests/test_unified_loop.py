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


def _declare_turns(
    title: str,
    *,
    predicate: str = "status",
    value: str = "open",
) -> list[dict]:
    return [
        {
            "tool_call": {
                "name": "emit",
                "arguments": {
                    "assertions": [
                        {
                            "subject": {
                                "new_subject": {
                                    "ref": "topic",
                                    "subject_type": "MATTER",
                                    "title": title,
                                }
                            },
                            "predicate": predicate,
                            "operation": "ASSERT",
                            "object_value": value,
                            "evidence_aliases": ["m1"],
                        }
                    ]
                },
            }
        },
        {"final_message": "done"},
    ]


def _titled_record(record_id: str, *, container: str = "octo-room") -> Record:
    return Record.model_validate(
        {
            "record_id": record_id,
            "container_id": container,
            "sent_at": "2026-08-06T09:00:00Z",
            "author": {"id": "dana-reyes", "display_name": "Dana Reyes", "kind": "human"},
            "content": "Fictional discussion of the compatibility audit.",
            "kind": "im",
        }
    )


def test_same_title_in_two_windows_of_one_container_is_one_subject(tmp_path) -> None:
    title = "Fictional compatibility audit"
    gateway = FixtureGateway(
        extraction=[],
        adjudication=[],
        semantic=[],
        tool_loop=[
            {"turns": _declare_turns(title)},
            {
                "turns": _declare_turns(
                    title,
                    predicate="progress",
                    value="The audit moved to its second window.",
                )
            },
        ],
    )
    engine = Engine(
        tmp_path / "mint.db",
        gateway=gateway,
        unified_loop=True,
        clock=lambda: datetime(2026, 8, 6, 10, tzinfo=UTC),
    )

    engine.add_records([_titled_record("octo-room:r1")], scope_id="scope")
    engine.add_records([_titled_record("octo-room:r2")], scope_id="scope")

    matters = [item for item in engine.store.subjects("scope") if item.title == title]
    assert len(matters) == 1
    # The second window becomes evidence on the same subject, not a twin.
    assert matters[0].source_ids == frozenset({"octo-room:r1", "octo-room:r2"})


def test_same_title_in_two_containers_stays_two_subjects(tmp_path) -> None:
    title = "Fictional compatibility audit"
    gateway = FixtureGateway(
        extraction=[],
        adjudication=[],
        semantic=[],
        tool_loop=[{"turns": _declare_turns(title)}, {"turns": _declare_turns(title)}],
    )
    engine = Engine(
        tmp_path / "mint-containers.db",
        gateway=gateway,
        unified_loop=True,
        clock=lambda: datetime(2026, 8, 6, 10, tzinfo=UTC),
    )

    engine.add_records([_titled_record("octo-room:r1")], scope_id="scope")
    engine.add_records(
        [_titled_record("other-room:r1", container="other-room")], scope_id="scope"
    )

    assert len([item for item in engine.store.subjects("scope") if item.title == title]) == 2


def test_a_merged_away_declared_subject_is_not_re_minted(tmp_path) -> None:
    """A stable declared key can now name a subject a human merged away."""

    title = "Fictional compatibility audit"
    gateway = FixtureGateway(
        extraction=[],
        adjudication=[],
        semantic=[],
        tool_loop=[
            {"turns": _declare_turns(title)},
            {
                "turns": _declare_turns(
                    title,
                    predicate="progress",
                    value="The audit continued after the merge.",
                )
            },
        ],
    )
    engine = Engine(
        tmp_path / "mint-merged.db",
        gateway=gateway,
        unified_loop=True,
        clock=lambda: datetime(2026, 8, 6, 10, tzinfo=UTC),
    )
    engine.add_records([_titled_record("octo-room:r1")], scope_id="scope")
    minted = next(
        item for item in engine.store.subjects("scope") if item.title == title
    )
    engine._ingest_cards_sync(
        [
            EpisodeCard(
                card_id="survivor",
                scope_id="scope",
                subject_key="survivor",
                date=date(2026, 8, 6),
                title="Fictional surviving audit",
                status="open",
                source_refs=[
                    SourceRef(
                        source_id="octo-seed:survivor",
                        sent_at=datetime(2026, 8, 6, 8, tzinfo=UTC),
                        sender="Dana Reyes",
                    )
                ],
            )
        ],
        scope_id="scope",
    )
    engine.merge_subjects(
        "scope",
        minted.subject_key,
        "survivor",
        source_refs=[
            SourceRef(
                source_id="review:merge-away",
                sent_at=datetime(2026, 8, 6, 9, tzinfo=UTC),
                sender="Dana Reyes",
            )
        ],
        valid_from=datetime(2026, 8, 6, 9, tzinfo=UTC),
    )

    engine.add_records([_titled_record("octo-room:r2")], scope_id="scope")

    # The second window must write to the survivor, not resurrect the
    # merged-away key.
    progress = engine.query.current("scope", "survivor", "progress")
    assert [item.value for item in progress] == [
        "The audit continued after the merge."
    ]


def test_degenerate_titles_never_become_identity_anchors(tmp_path) -> None:
    gateway = FixtureGateway(
        extraction=[],
        adjudication=[],
        semantic=[],
        tool_loop=[{"turns": _declare_turns("总结")}, {"turns": _declare_turns("总结")}],
    )
    engine = Engine(
        tmp_path / "mint-degenerate.db",
        gateway=gateway,
        unified_loop=True,
        clock=lambda: datetime(2026, 8, 6, 10, tzinfo=UTC),
    )

    engine.add_records([_titled_record("octo-room:r1")], scope_id="scope")
    engine.add_records([_titled_record("octo-room:r2")], scope_id="scope")

    # Fusing on "总结" would build one immortal accreting subject.
    assert len([item for item in engine.store.subjects("scope") if item.title == "总结"]) == 2


def test_duplicates_command_reports_pairs_but_never_degenerate_titles(tmp_path) -> None:
    from typer.testing import CliRunner

    from matterhorn.cli.app import app

    db = tmp_path / "dupes.db"
    engine = Engine(db, clock=lambda: datetime(2026, 8, 6, 10, tzinfo=UTC))

    def seed(key: str, title: str) -> None:
        engine._ingest_cards_sync(
            [
                EpisodeCard(
                    card_id=f"card-{key}",
                    scope_id="scope",
                    subject_key=key,
                    date=date(2026, 8, 6),
                    title=title,
                    status="open",
                    source_refs=[
                        SourceRef(
                            source_id=f"octo-room:{key}",
                            sent_at=datetime(2026, 8, 6, 8, tzinfo=UTC),
                            sender="Dana Reyes",
                        )
                    ],
                )
            ],
            scope_id="scope",
        )

    seed("dup-1", "Fictional compatibility audit")
    seed("dup-2", "Fictional compatibility audit")
    seed("noise-1", "总结")
    seed("noise-2", "总结")

    result = CliRunner().invoke(app, ["duplicates", "scope", "--db", str(db)])

    assert result.exit_code == 1
    groups = json.loads(result.stdout)
    assert [item["normalized_title"] for item in groups] == [
        "fictional compatibility audit"
    ]
    assert groups[0]["keep"] == "dup-1"
    assert groups[0]["merge_away"] == ["dup-2"]
    # The command only reports; merging stays a human decision.
    assert engine.canonical_subject_key("scope", "dup-2") == "dup-2"


def test_mutual_parent_rejection_enqueues_one_merge_suggestion(tmp_path) -> None:
    gateway = _gateway(
        [
            {
                "tool_call": {
                    "name": "read_neighborhood",
                    "arguments": {"subject_keys": ["alpha", "beta"]},
                }
            },
            {
                "tool_call": {
                    "name": "emit",
                    "arguments": {
                        "assertions": [
                            {
                                "subject": {"subject_key": "alpha"},
                                "predicate": "part_of",
                                "operation": "ASSERT",
                                "object_value": "beta",
                                "evidence_aliases": ["m1"],
                            }
                        ]
                    },
                }
            },
            {"final_message": "done"},
        ]
    )
    engine = Engine(
        tmp_path / "mutual.db",
        gateway=gateway,
        unified_loop=True,
        clock=lambda: datetime(2026, 8, 6, 10, tzinfo=UTC),
    )
    for key in ("alpha", "beta"):
        engine._ingest_cards_sync(
            [
                EpisodeCard(
                    card_id=f"seed-{key}",
                    scope_id="scope",
                    subject_key=key,
                    date=date(2026, 8, 6),
                    title=f"Fictional {key} initiative",
                    status="open",
                    source_refs=[
                        SourceRef(
                            source_id=f"octo-seed:{key}",
                            sent_at=datetime(2026, 8, 6, 8, tzinfo=UTC),
                            sender="Dana Reyes",
                        )
                    ],
                )
            ],
            scope_id="scope",
        )
    engine.correct(
        {
            "scope_id": "scope",
            "subject_key": "beta",
            "subject_type": "MATTER",
            "predicate": "part_of",
            "operation": "ASSERT",
            "object_value": "alpha",
            "valid_from": datetime(2026, 8, 6, 8, 30, tzinfo=UTC),
            "source_refs": [
                {
                    "source_id": "review:beta-alpha",
                    "sent_at": datetime(2026, 8, 6, 8, 30, tzinfo=UTC),
                    "sender": "Dana Reyes",
                }
            ],
        }
    )

    report = engine.add_records([_record()], scope_id="scope")

    assert report.drop_reasons.get("STRUCTURE_CYCLE") == 1
    suggestions = [
        item
        for item in engine.review_items("scope")
        if "MERGE_SUGGESTION" in item.reasons
    ]
    assert len(suggestions) == 1
    candidate = suggestions[0].candidates_json[0]
    assert candidate["action"] == "merge"
    assert candidate["subject_key"] == "alpha"
    assert candidate["parent_subject_key"] == "beta"

    # The suggestion resolves through the merge door and dissolves the pair.
    resolved = engine.resolve_review(
        "scope",
        suggestions[0].review_id,
        action="merge",
        subject_key=candidate["subject_key"],
        parent_subject_key=candidate["parent_subject_key"],
        source_refs=[
            {
                "source_id": "review:merge-resolution",
                "sent_at": datetime(2026, 8, 6, 11, tzinfo=UTC),
                "sender": "Dana Reyes",
            }
        ],
    )
    assert resolved.resolved_at is not None
    assert engine.canonical_subject_key("scope", "alpha") == "beta"
    assert engine.structure_cycles("scope") == []


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
