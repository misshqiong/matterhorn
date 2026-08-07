from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from matterhorn.api import create_app
from matterhorn.canonical import canonical_json, derive_assertion_id, object_key
from matterhorn.contracts import Assertion, Operation, Origin, SourceRef
from matterhorn.defaults import Engine
from matterhorn.engine.theme_converge import snapshot_theme_state
from matterhorn.store import SQLiteStore

NOW = datetime(2026, 8, 7, 9, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "postgres"])
def election_engine(request, tmp_path):
    scope_id = f"octo-election-{request.param}"
    if request.param == "sqlite":
        store = SQLiteStore(tmp_path / "election.db")
    else:
        dsn = os.environ.get("MATTERHORN_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("MATTERHORN_TEST_POSTGRES_DSN is unset")
        from matterhorn.store.postgres import PostgresStore

        store = PostgresStore(dsn)
    store.clear_scope(scope_id)
    engine = Engine(
        store,
        clock=lambda: NOW + timedelta(hours=1),
        human_edge_weight=10,
    )
    try:
        yield engine, scope_id
    finally:
        store.clear_scope(scope_id)
        store.close()


def _source(source_id: str, *, minute: int) -> SourceRef:
    return SourceRef(
        source_id=source_id,
        sent_at=NOW + timedelta(minutes=minute),
        sender="Dana Reyes",
        excerpt=f"Fictional octo-org evidence {source_id}.",
    )


def _seed(engine: Engine, scope_id: str, keys: tuple[str, ...]) -> None:
    engine._ingest_cards_sync(
        [
            {
                "card_id": f"card-{key}",
                "scope_id": scope_id,
                "subject_key": key,
                "date": "2026-08-07",
                "occurred_at": NOW + timedelta(minutes=index),
                "title": f"Fictional {key}",
                "status": "open",
                "source_refs": [
                    _source(f"octo-org:seed:{key}", minute=index).model_dump(
                        mode="json"
                    )
                ],
            }
            for index, key in enumerate(keys)
        ]
    )


def _model_edge(
    engine: Engine,
    scope_id: str,
    subject_key: str,
    target: str,
    *,
    minute: int,
) -> Assertion:
    source_refs = [_source(f"octo-org:model:{subject_key}:{minute}", minute=minute)]
    valid_from = NOW + timedelta(minutes=minute)
    assertion = Assertion(
        assertion_id=derive_assertion_id(
            scope_id,
            subject_key,
            "part_of",
            Operation.ASSERT,
            object_key(target),
            valid_from,
            source_refs,
        ),
        scope_id=scope_id,
        subject_key=subject_key,
        subject_type="MATTER",
        predicate="part_of",
        operation=Operation.ASSERT,
        object_value=target,
        object_key=object_key(target),
        valid_from=valid_from,
        recorded_at=valid_from + timedelta(seconds=30),
        source_refs=source_refs,
        origin=Origin.model,
    )
    rejection = engine._structure_rejection(assertion)
    if rejection is not None:
        raise ValueError(rejection.value)
    assert not engine._model_assertion_is_unchanged(
        assertion,
        engine.store.assertions(scope_id),
    )
    with engine.store.transaction():
        for source_ref in source_refs:
            engine.store.observe_source(scope_id, source_ref)
        assert engine._add_assertion(assertion)
        engine._rebuild(scope_id)
    return assertion


def _human_correction_payload(scope_id: str, subject_key: str, target: str) -> dict:
    return {
        "subject_key": subject_key,
        "subject_type": "MATTER",
        "predicate": "part_of",
        "operation": "ASSERT",
        "object_value": target,
        "valid_from": (NOW + timedelta(minutes=10)).isoformat(),
        "source_refs": [
            _source("octo-org:human-placement", minute=10).model_dump(mode="json")
        ],
    }


def test_rest_human_and_model_admissions_flip_every_read_and_enqueue_notice(
    election_engine,
) -> None:
    engine, scope_id = election_engine
    _seed(engine, scope_id, ("child", "human-parent", "model-parent"))

    async def correct_over_rest() -> None:
        transport = httpx.ASGITransport(app=create_app(engine=engine))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            response = await client.post(
                f"/v1/scopes/{scope_id}/corrections",
                json=_human_correction_payload(scope_id, "child", "human-parent"),
            )
            assert response.status_code == 200

    asyncio.run(correct_over_rest())
    for minute in range(11, 20):
        _model_edge(engine, scope_id, "child", "model-parent", minute=minute)

    assert engine.matter_graph(scope_id, "child").root_subject_key == "human-parent"
    assert engine.query.current(scope_id, "child", "part_of")[0].value == (
        "human-parent"
    )

    _model_edge(engine, scope_id, "child", "model-parent", minute=20)

    graph = engine.matter_graph(scope_id, "child")
    assert graph.root_subject_key == "model-parent"
    assert graph.parent_chain[0].subject_key == "model-parent"
    assert engine.query.current(scope_id, "child", "part_of")[0].value == (
        "model-parent"
    )
    assert "child" not in {
        item.subject_key for item in snapshot_theme_state(engine, scope_id).candidates
    }
    roots = {item.subject_key for item in engine.matters(scope_id)}
    assert roots == {"human-parent", "model-parent"}

    notices = engine.review_items(scope_id)
    assert len(notices) == 1
    assert notices[0].reasons == ["ELECTION_OVERRODE_HUMAN"]
    assert notices[0].candidates_json == [
        {
            "action": "election_notice",
            "subject_key": "child",
            "old_human_target": "human-parent",
            "new_elected_target": "model-parent",
            "weights": {
                "human-parent": {"human": 10, "model": 0, "total": 10},
                "model-parent": {"human": 0, "model": 10, "total": 10},
            },
        }
    ]

    before = canonical_json(graph.to_dict())
    engine.replay(scope_id)
    assert canonical_json(engine.matter_graph(scope_id, "child").to_dict()) == before
    assert len(engine.review_items(scope_id)) == 1


def test_cycle_gate_rejects_only_the_assertion_that_would_elect_a_cycle(
    election_engine,
) -> None:
    engine, scope_id = election_engine
    _seed(engine, scope_id, ("child", "root", "safe-parent"))
    engine.correct(
        {
            "scope_id": scope_id,
            **_human_correction_payload(scope_id, "child", "root"),
        }
    )
    engine.correct(
        {
            "scope_id": scope_id,
            **_human_correction_payload(scope_id, "root", "safe-parent"),
        }
    )

    for minute in range(11, 20):
        _model_edge(engine, scope_id, "root", "child", minute=minute)

    with pytest.raises(ValueError, match="STRUCTURE_CYCLE"):
        _model_edge(engine, scope_id, "root", "child", minute=20)
    assert engine.matter_graph(scope_id, "root").parent_chain[0].subject_key == (
        "safe-parent"
    )
