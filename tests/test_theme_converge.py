from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from matterhorn.cli.app import app
from matterhorn.contracts import TaskStatus
from matterhorn.defaults import Engine
from matterhorn.distill import ToolLoopResult
from matterhorn.engine.theme_converge import (
    AffinityKind,
    ThemeCandidate,
    ThemeNamingSession,
    cluster_themes,
    configured_theme_settings,
)
from matterhorn.store import SQLiteStore

NOW = datetime(2026, 8, 6, 9, tzinfo=UTC)
SCOPE = "octo-themes"


class ThemeGateway:
    def __init__(self, proposals: list[dict[str, object]]) -> None:
        self.proposals = list(proposals)
        self.calls = 0

    def complete(self, **_kwargs) -> str:
        raise AssertionError("theme convergence must use the tool loop")

    def tool_loop(
        self,
        *,
        system,
        user,
        tools,
        handler,
        max_tool_calls=16,
        max_emissions=4,
    ) -> ToolLoopResult:
        del system, user, tools, max_tool_calls, max_emissions
        proposal = self.proposals[self.calls]
        self.calls += 1
        handler("emit", proposal)
        return ToolLoopResult("done", tool_calls=1, emissions=1)


def _source(subject_key: str, *, container: str | None = None) -> dict[str, object]:
    selected = container or f"room-{subject_key}"
    return {
        "source_id": f"{selected}:{subject_key}",
        "sent_at": NOW,
        "sender": "Dana Reyes",
        "excerpt": f"Fictional evidence for {subject_key}.",
    }


def _seed(
    engine: Engine,
    rows: list[tuple[str, str, str | None]],
    *,
    scope_id: str = SCOPE,
) -> None:
    cards = []
    for key, title, container in rows:
        source = _source(key, container=container)
        cards.append(
            {
                "card_id": f"card-{key}",
                "scope_id": scope_id,
                "subject_key": key,
                "date": "2026-08-06",
                "title": title,
                "status": "open",
                "source_refs": [source],
            }
        )
    engine._ingest_cards_sync(cards, scope_id=scope_id)
    with engine.store.transaction():
        for key, _, container in rows:
            selected = container or f"room-{key}"
            engine.store.mark_record_observation(
                scope_id,
                f"{selected}:{key}",
                f"observation-{key}",
                selected,
                NOW,
            )


def _candidate(
    key: str,
    *,
    handles: tuple[str, ...] = (),
    conversations: tuple[str, ...] = (),
    title_tokens: tuple[str, ...] = (),
) -> ThemeCandidate:
    return ThemeCandidate(
        subject_key=key,
        subject_type="MATTER",
        title=key,
        title_tokens=frozenset(title_tokens),
        handle_values=frozenset(handles),
        conversations=frozenset(conversations),
        source_refs=(),
    )


def test_connected_components_are_deterministic_for_all_affinity_kinds() -> None:
    candidates = [
        _candidate("z", handles=("42",)),
        _candidate("a", handles=("42",), conversations=("room",)),
        _candidate("m", conversations=("room",), title_tokens=("octo", "launch")),
        _candidate(
            "n",
            handles=("42",),
            conversations=("room",),
            title_tokens=("octo", "launch"),
        ),
        _candidate("discard", title_tokens=("lonely",)),
    ]

    forward = cluster_themes(candidates, min_cluster=3)
    reverse = cluster_themes(list(reversed(candidates)), min_cluster=3)

    # "a"–"m" share only a conversation: a venue never bonds, so membership
    # comes from the handle and title edges; evidence still surfaces as a
    # confidence kind on the bonded pairs that also share the room.
    assert [item.member_keys for item in forward] == [("a", "m", "n", "z")]
    assert forward == reverse
    assert forward[0].affinity_kinds == {
        AffinityKind.handle,
        AffinityKind.evidence,
        AffinityKind.title,
    }


def test_min_cluster_discards_small_components() -> None:
    candidates = [
        _candidate("a", title_tokens=("octo", "release")),
        _candidate("b", title_tokens=("octo", "release")),
    ]
    assert cluster_themes(candidates, min_cluster=3) == ()


def test_conversation_alone_never_bonds() -> None:
    candidates = [
        _candidate(key, conversations=("busy-room",))
        for key in ("a", "b", "c")
    ]

    assert cluster_themes(candidates, conversation_fanout=3) == ()


def test_promiscuous_conversation_loses_confidence_kind_above_fanout() -> None:
    candidates = [
        _candidate(key, conversations=("busy-room",), title_tokens=("octo", "gate"))
        for key in ("a", "b", "c")
    ]

    bonded = cluster_themes(candidates, conversation_fanout=3)
    assert len(bonded) == 1
    assert AffinityKind.evidence in bonded[0].affinity_kinds
    discounted = cluster_themes(candidates, conversation_fanout=2)
    assert len(discounted) == 1
    assert AffinityKind.evidence not in discounted[0].affinity_kinds


def test_theme_configuration_env_overrides_defaults(monkeypatch) -> None:
    monkeypatch.setenv("MATTERHORN_THEME_CONVERGE", "auto")
    monkeypatch.setenv("MATTERHORN_THEME_MIN_CLUSTER", "4")
    monkeypatch.setenv("MATTERHORN_THEME_MIN_BACKLOG", "9")
    monkeypatch.setenv("MATTERHORN_THEME_INTERVAL_HOURS", "2.5")
    monkeypatch.setenv("MATTERHORN_THEME_CONVERSATION_FANOUT", "12")
    monkeypatch.setenv("MATTERHORN_HUMAN_EDGE_WEIGHT", "14")

    settings = configured_theme_settings()

    assert settings.mode == "auto"
    assert settings.min_cluster == 4
    assert settings.min_backlog == 9
    assert settings.interval_hours == 2.5
    assert settings.conversation_fanout == 12
    assert settings.human_edge_weight == 14


def test_theme_configuration_rejects_invalid_mode_and_thresholds(monkeypatch) -> None:
    monkeypatch.setenv("MATTERHORN_THEME_CONVERGE", "sometimes")
    with pytest.raises(ValueError, match="off, review, or auto"):
        configured_theme_settings()
    monkeypatch.setenv("MATTERHORN_THEME_CONVERGE", "review")
    monkeypatch.setenv("MATTERHORN_THEME_MIN_CLUSTER", "1")
    with pytest.raises(ValueError, match="integer >= 2"):
        configured_theme_settings()
    monkeypatch.setenv("MATTERHORN_THEME_MIN_CLUSTER", "3")
    monkeypatch.setenv("MATTERHORN_HUMAN_EDGE_WEIGHT", "1")
    with pytest.raises(ValueError, match="human_edge_weight MUST be an integer >= 2"):
        configured_theme_settings()


def test_naming_session_rejects_subjects_outside_cluster() -> None:
    cluster = cluster_themes(
        [
            _candidate("a", title_tokens=("octo", "release")),
            _candidate("b", title_tokens=("octo", "release")),
            _candidate("c", title_tokens=("octo", "release")),
        ]
    )[0]
    gateway = ThemeGateway(
        [
            {
                "title": "Octo release theme",
                "member_subject_keys": ["a", "outside"],
                "existing_root_subsumes": None,
            }
        ]
    )

    result = ThemeNamingSession(scope_id=SCOPE, cluster=cluster).run(gateway)

    assert result.proposal is None
    assert result.rejection_counts == {"CLOSED_WORLD_VIOLATION": 1}


def test_auto_confidence_gate_falls_back_to_review_and_rerun_is_noop(tmp_path) -> None:
    gateway = ThemeGateway(
        [
            {
                "title": "Octo launch theme",
                "member_subject_keys": ["a", "b", "c"],
                "existing_root_subsumes": None,
            }
        ]
    )
    engine = Engine(
        tmp_path / "review.db",
        gateway=gateway,
        clock=lambda: NOW + timedelta(hours=1),
        theme_converge="auto",
    )
    _seed(
        engine,
        [
            ("a", "Octo launch alpha", None),
            ("b", "Octo launch beta", None),
            ("c", "Octo launch gamma", None),
        ],
    )

    first = engine.themes(SCOPE)
    second = engine.themes(SCOPE)

    assert first.edges_applied == 0
    assert first.reviews_enqueued == 3
    assert first.roots_created == 1
    assert second.clusters == ()
    assert second.reviews_enqueued == 0
    assert gateway.calls == 1


def test_auto_applies_five_member_one_kind_cluster_as_model_edges(tmp_path) -> None:
    members = ["a", "b", "c", "d", "e"]
    gateway = ThemeGateway(
        [
            {
                "title": "Octo release theme",
                "member_subject_keys": members,
                "existing_root_subsumes": None,
            }
        ]
    )
    engine = Engine(
        tmp_path / "auto.db",
        gateway=gateway,
        clock=lambda: NOW + timedelta(hours=1),
        theme_converge="auto",
    )
    _seed(
        engine,
        [(key, f"Octo release {key}", None) for key in members],
    )

    report = engine.themes(SCOPE)

    assert report.edges_applied == 5
    assert report.reviews_enqueued == 0
    assert report.roots_created == 1
    parents = {
        engine.query.current(SCOPE, key, "part_of")[0].value for key in members
    }
    assert parents == {report.proposals[0].parent_subject_key}
    assert all(
        engine.query.current(SCOPE, key, "part_of")[0].origin == "model"
        for key in members
    )
    _seed(engine, [("human-parent", "Fictional human parent", None)])
    engine.correct(
        {
            "scope_id": SCOPE,
            "subject_key": "a",
            "subject_type": "MATTER",
            "predicate": "part_of",
            "object_value": "human-parent",
            "valid_from": NOW + timedelta(hours=2),
            "source_refs": [_source("human-reparent")],
        }
    )
    corrected = engine.query.current(SCOPE, "a", "part_of")[0]
    assert corrected.value == "human-parent"
    assert corrected.origin == "human"
    assert engine.themes(SCOPE).clusters == ()


def test_review_mode_always_queues_even_with_two_affinity_kinds(tmp_path) -> None:
    gateway = ThemeGateway(
        [
            {
                "title": "Dana octo theme",
                "member_subject_keys": ["a", "b", "c"],
                "existing_root_subsumes": None,
            }
        ]
    )
    engine = Engine(
        tmp_path / "always-review.db",
        gateway=gateway,
        clock=lambda: NOW + timedelta(hours=1),
        theme_converge="review",
    )
    _seed(
        engine,
        [
            ("a", "Dana octo alpha", "shared-room"),
            ("b", "Dana octo beta", "shared-room"),
            ("c", "Dana octo gamma", "shared-room"),
        ],
    )

    report = engine.themes(SCOPE)

    assert report.clusters[0].confident is True
    assert report.edges_applied == 0
    assert report.reviews_enqueued == 3
    review = engine.review_items(SCOPE)[0]
    candidate = review.candidates_json[0]
    resolved = engine.resolve_review(
        SCOPE,
        review.review_id,
        action="attach_subgoal",
        parent_subject_key=candidate["parent_subject_key"],
        source_refs=[_source("theme-review-resolution")],
    )
    assertion = engine.query.current(SCOPE, candidate["subject_key"], "part_of")[0]
    assert resolved.resolution_json["action"] == "attach_subgoal"
    assert assertion.value == candidate["parent_subject_key"]
    assert assertion.origin == "human"


def test_existing_parent_root_is_reused_and_cycle_edge_is_rejected(tmp_path) -> None:
    gateway = ThemeGateway(
        [
            {
                "title": "Ignored proposed title",
                "member_subject_keys": ["a", "b", "c"],
                "existing_root_subsumes": True,
            }
        ]
    )
    engine = Engine(
        tmp_path / "existing.db",
        gateway=gateway,
        clock=lambda: NOW + timedelta(hours=1),
        theme_converge="auto",
    )
    _seed(
        engine,
        [
            ("a", "Octo release parent", "shared-room"),
            ("b", "Octo release beta", "shared-room"),
            ("c", "Octo release gamma", "shared-room"),
            ("child", "Fictional child", None),
        ],
    )
    engine.correct(
        {
            "scope_id": SCOPE,
            "subject_key": "child",
            "subject_type": "MATTER",
            "predicate": "part_of",
            "object_value": "a",
            "valid_from": NOW + timedelta(minutes=1),
            "source_refs": [_source("child-edge")],
        }
    )
    engine.correct(
        {
            "scope_id": SCOPE,
            "subject_key": "a",
            "subject_type": "MATTER",
            "predicate": "spawned_from",
            "object_value": "b",
            "valid_from": NOW + timedelta(minutes=2),
            "source_refs": [_source("spawn-edge")],
        }
    )

    report = engine.themes(SCOPE)

    assert report.proposals[0].parent_subject_key == "a"
    assert report.roots_created == 0
    assert report.edges_applied == 1
    assert report.rejection_counts == {"STRUCTURE_CYCLE": 1}
    assert engine.query.current(SCOPE, "c", "part_of")[0].value == "a"


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_schedule_throttle_persists_on_both_backends(tmp_path, backend) -> None:
    if backend == "sqlite":
        store = SQLiteStore(tmp_path / "schedule.db")
    else:
        dsn = os.environ.get("MATTERHORN_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("MATTERHORN_TEST_POSTGRES_DSN is unset")
        from matterhorn.store.postgres import PostgresStore

        store = PostgresStore(dsn)
        store.clear_scope(SCOPE)
    gateway = ThemeGateway(
        [
            {
                "title": "Octo schedule theme",
                "member_subject_keys": ["a", "b", "c"],
                "existing_root_subsumes": None,
            }
        ]
    )
    engine = Engine(
        store,
        gateway=gateway,
        clock=lambda: NOW,
        theme_converge="review",
        theme_min_backlog=3,
    )
    _seed(
        engine,
        [(key, f"Octo schedule {key}", None) for key in ("a", "b", "c")],
    )

    first = engine.flush(SCOPE)
    second = engine.flush(SCOPE)
    state = store.theme_schedule_state(SCOPE)

    assert first.remaining == 1
    assert second.tasks_processed == 1
    assert state is not None
    assert state.last_enqueued_at == NOW
    assert state.last_run_at == NOW
    assert len(
        [row for row in store.tasks(SCOPE) if row.kind == "themes"]
    ) == 1
    assert store.tasks(SCOPE)[-1].result.status == TaskStatus.completed
    store.close()


def test_cli_dry_run_prints_clusters_and_does_not_write(tmp_path, monkeypatch) -> None:
    gateway = ThemeGateway(
        [
            {
                "title": "Octo CLI theme",
                "member_subject_keys": ["a", "b", "c"],
                "existing_root_subsumes": None,
            }
        ]
    )
    db = tmp_path / "cli.db"
    engine = Engine(
        db,
        gateway=gateway,
        clock=lambda: NOW,
        theme_converge="review",
    )
    _seed(
        engine,
        [(key, f"Octo CLI {key}", None) for key in ("a", "b", "c")],
    )
    engine.store.close()
    monkeypatch.setattr("matterhorn.cli.app._write_gateway", lambda *_args: gateway)

    completed = CliRunner().invoke(
        app,
        ["themes", "run", SCOPE, "--dry-run", "--db", str(db)],
    )

    assert completed.exit_code == 0, completed.output
    payload = json.loads(completed.output)
    assert payload["dry_run"] is True
    assert payload["clusters"][0]["member_subject_keys"] == ["a", "b", "c"]
    assert payload["proposals"][0]["disposition"] == "dry-run-review"
    check = Engine(db, theme_converge="review")
    assert check.review_items(SCOPE) == []
    assert all(check.query.current(SCOPE, key, "part_of") == [] for key in ("a", "b", "c"))


def test_affinity_compares_normalized_handle_values_independent_of_type() -> None:
    clusters = cluster_themes(
        [
            _candidate("a", handles=("42",)),
            _candidate("b", handles=("42",)),
            _candidate("c", handles=("42",)),
        ]
    )

    assert len(clusters) == 1
    assert clusters[0].affinity_kinds == {AffinityKind.handle}
    assert clusters[0].member_keys == ("a", "b", "c")


def test_weak_edges_attach_loners_but_never_merge_strong_components() -> None:
    candidates = [
        _candidate("s1", title_tokens=("dana", "launch", "alpha")),
        _candidate("s2", title_tokens=("dana", "launch", "beta")),
        _candidate("t1", title_tokens=("octo", "lunar", "alpha")),
        _candidate("t2", title_tokens=("octo", "lunar", "beta")),
        _candidate("w1", title_tokens=("hook", "配置")),
        _candidate("w2", title_tokens=("hook", "验证")),
        _candidate("w3", title_tokens=("hook", "日志")),
    ]

    clusters = cluster_themes(candidates, min_cluster=2)
    members = sorted(item.member_keys for item in clusters)
    # "alpha"/"beta" weak edges must not merge the two strong clusters;
    # the hook loners chain together through weak edges alone.
    assert members == [("s1", "s2"), ("t1", "t2"), ("w1", "w2", "w3")]
