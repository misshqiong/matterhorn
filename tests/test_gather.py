from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from matterhorn.api import create_app
from matterhorn.canonical import canonical_json, derive_assertion_id
from matterhorn.contracts import Assertion, EpisodeCard, Operation, Origin, SourceRef
from matterhorn.defaults import Engine
from matterhorn.engine.gather import encode_subject_ref
from matterhorn.store import SQLiteStore

NOW = datetime(2026, 8, 7, 9, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "postgres"])
def gather_engine(request, tmp_path):
    prefix = f"octo-gather-{request.param}"
    if request.param == "sqlite":
        store = SQLiteStore(tmp_path / "gather.db")
    else:
        dsn = os.environ.get("MATTERHORN_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("MATTERHORN_TEST_POSTGRES_DSN is unset")
        from matterhorn.store.postgres import PostgresStore

        store = PostgresStore(dsn)
    scopes = [f"{prefix}-portfolio", f"{prefix}-a", f"{prefix}-b"]
    for scope_id in scopes:
        store.clear_scope(scope_id)
    engine = Engine(store, clock=lambda: NOW + timedelta(hours=1))
    try:
        yield engine, scopes
    finally:
        for scope_id in scopes:
            store.clear_scope(scope_id)
        store.close()


def _source(source_id: str, minute: int = 0) -> SourceRef:
    return SourceRef(
        source_id=source_id,
        sent_at=NOW + timedelta(minutes=minute),
        sender="Dana Reyes",
        excerpt=f"Fictional octo-org evidence {source_id}.",
    )


def _card(
    scope_id: str,
    subject_key: str,
    *,
    layer: int = 1,
    status: str = "open",
    blocker: str | None = None,
    minute: int = 0,
) -> dict:
    return {
        "card_id": f"card-{scope_id}-{subject_key}",
        "scope_id": scope_id,
        "subject_key": subject_key,
        "layer": layer,
        "date": "2026-08-07",
        "occurred_at": NOW + timedelta(minutes=minute),
        "title": f"Fictional {subject_key}",
        "status": status,
        "blocker": blocker,
        "source_refs": [
            _source(f"octo-org:seed:{scope_id}:{subject_key}", minute).model_dump(
                mode="json"
            )
        ],
    }


def _correct_gather(
    engine: Engine,
    source_scope: str,
    source_key: str,
    target_scope: str,
    target_key: str,
    *,
    object_key: str | None = None,
    minute: int = 10,
) -> Assertion:
    return engine.correct(
        {
            "scope_id": source_scope,
            "subject_key": source_key,
            "subject_type": "MATTER",
            "predicate": "gathers",
            "operation": "ASSERT",
            "object_value": encode_subject_ref(target_scope, target_key),
            "object_key": object_key,
            "valid_from": NOW + timedelta(minutes=minute),
            "source_refs": [
                _source(
                    f"octo-org:gather:{source_key}:{target_scope}:{target_key}:{minute}",
                    minute,
                ).model_dump(mode="json")
            ],
        }
    )


def _model_gather(
    engine: Engine,
    source_scope: str,
    source_key: str,
    target_scope: str,
    target_key: str,
    *,
    slot: str,
    minute: int,
) -> Assertion:
    target = encode_subject_ref(target_scope, target_key)
    refs = [_source(f"octo-org:model:gather:{minute}", minute)]
    valid_from = NOW + timedelta(minutes=minute)
    assertion = Assertion(
        assertion_id=derive_assertion_id(
            source_scope,
            source_key,
            "gathers",
            Operation.ASSERT,
            slot,
            valid_from,
            refs,
        ),
        scope_id=source_scope,
        subject_key=source_key,
        subject_type="MATTER",
        predicate="gathers",
        operation=Operation.ASSERT,
        object_value=target,
        object_key=slot,
        valid_from=valid_from,
        recorded_at=valid_from + timedelta(seconds=30),
        source_refs=refs,
        origin=Origin.model,
    )
    rejection = engine._structure_rejection(assertion)
    if rejection is not None:
        raise ValueError(rejection.value)
    assert not engine._model_assertion_is_unchanged(
        assertion, engine.store.assertions(source_scope)
    )
    with engine.store.transaction():
        for ref in refs:
            engine.store.observe_source(source_scope, ref)
        assert engine._add_assertion(assertion)
        engine._rebuild(source_scope)
    return assertion


def test_gather_gates_resolve_cross_scope_targets_and_preserve_local_tree_rules(
    gather_engine,
) -> None:
    engine, (portfolio_scope, scope_a, scope_b) = gather_engine
    engine._ingest_cards_sync(
        [
            _card(portfolio_scope, "portfolio", layer=2),
            _card(portfolio_scope, "portfolio-alias", layer=2),
            _card(portfolio_scope, "local-member"),
            _card(scope_a, "remote-member"),
            _card(scope_b, "other-portfolio", layer=2),
        ]
    )

    admitted = _correct_gather(
        engine,
        portfolio_scope,
        "portfolio",
        scope_a,
        "remote-member",
    )
    assert admitted.object_value == encode_subject_ref(scope_a, "remote-member")
    current = engine.query.current(portfolio_scope, "portfolio", "gathers")
    assert current[0].source_ids == [
        f"octo-org:gather:portfolio:{scope_a}:remote-member:10"
    ]

    with pytest.raises(ValueError, match="GATHER_LAYER_VIOLATION"):
        _correct_gather(
            engine,
            portfolio_scope,
            "local-member",
            scope_a,
            "remote-member",
            minute=11,
        )
    with pytest.raises(ValueError, match="GATHER_LAYER_VIOLATION"):
        _correct_gather(
            engine,
            portfolio_scope,
            "portfolio",
            scope_b,
            "other-portfolio",
            minute=12,
        )
    with pytest.raises(ValueError, match="GATHER_SELF_REFERENCE"):
        _correct_gather(
            engine,
            portfolio_scope,
            "portfolio",
            portfolio_scope,
            "portfolio",
            minute=13,
        )
    with pytest.raises(ValueError, match="GATHER_UNKNOWN_TARGET"):
        _correct_gather(
            engine,
            portfolio_scope,
            "portfolio",
            scope_a,
            "missing",
            minute=14,
        )
    with pytest.raises(ValueError, match="GATHER_INVALID_TARGET"):
        engine.correct(
            {
                "scope_id": portfolio_scope,
                "subject_key": "portfolio",
                "subject_type": "MATTER",
                "predicate": "gathers",
                "object_value": "not-a-canonical-reference",
                "valid_from": NOW + timedelta(minutes=15),
                "source_refs": [
                    _source("octo-org:gather:invalid", 15).model_dump(
                        mode="json"
                    )
                ],
            }
        )
    engine.merge_subjects(
        portfolio_scope,
        "portfolio-alias",
        "portfolio",
        source_refs=[_source("octo-org:merge:portfolio-alias", 16)],
        valid_from=NOW + timedelta(minutes=16),
    )
    with pytest.raises(ValueError, match="GATHER_SELF_REFERENCE"):
        _correct_gather(
            engine,
            portfolio_scope,
            "portfolio",
            portfolio_scope,
            "portfolio-alias",
            minute=17,
        )
    with pytest.raises(ValueError, match="STRUCTURE_CROSS_SCOPE"):
        engine.correct(
            {
                "scope_id": portfolio_scope,
                "subject_key": "local-member",
                "subject_type": "MATTER",
                "predicate": "part_of",
                "object_value": "remote-member",
                "valid_from": NOW + timedelta(minutes=18),
                "source_refs": [
                    _source("octo-org:part-of-cross-scope", 18).model_dump(
                        mode="json"
                    )
                ],
            }
        )


def test_weighted_gather_slot_flips_and_enqueues_human_notice(
    gather_engine,
) -> None:
    engine, (portfolio_scope, scope_a, scope_b) = gather_engine
    engine._ingest_cards_sync(
        [
            _card(portfolio_scope, "portfolio", layer=2),
            _card(scope_a, "human-member"),
            _card(scope_b, "model-member"),
        ]
    )
    slot = "delivery-membership-slot"
    human_target = encode_subject_ref(scope_a, "human-member")
    model_target = encode_subject_ref(scope_b, "model-member")
    _correct_gather(
        engine,
        portfolio_scope,
        "portfolio",
        scope_a,
        "human-member",
        object_key=slot,
    )
    for minute in range(11, 20):
        _model_gather(
            engine,
            portfolio_scope,
            "portfolio",
            scope_b,
            "model-member",
            slot=slot,
            minute=minute,
        )
    assert [
        item.value
        for item in engine.query.current(portfolio_scope, "portfolio", "gathers")
    ] == [human_target]

    _model_gather(
        engine,
        portfolio_scope,
        "portfolio",
        scope_b,
        "model-member",
        slot=slot,
        minute=20,
    )
    assert [
        item.value
        for item in engine.query.current(portfolio_scope, "portfolio", "gathers")
    ] == [model_target]
    notices = engine.review_items(portfolio_scope)
    assert len(notices) == 1
    assert notices[0].reasons == ["ELECTION_OVERRODE_HUMAN"]
    assert notices[0].candidates_json[0]["old_human_target"] == human_target
    assert notices[0].candidates_json[0]["new_elected_target"] == model_target


def test_gather_view_is_deterministic_and_rolls_up_two_scope_graphs(
    gather_engine,
) -> None:
    engine, (portfolio_scope, scope_a, scope_b) = gather_engine
    engine._ingest_cards_sync(
        [
            _card(portfolio_scope, "portfolio", layer=2),
            _card(portfolio_scope, "portfolio-parent", layer=2),
            _card(scope_b, "bravo-root", status="blocked", blocker="Dana review"),
            _card(scope_a, "alpha-root"),
            _card(scope_a, "alpha-child", status="done", minute=1),
        ]
    )
    engine.correct(
        {
            "scope_id": scope_a,
            "subject_key": "alpha-child",
            "subject_type": "MATTER",
            "predicate": "part_of",
            "object_value": "alpha-root",
            "valid_from": NOW + timedelta(minutes=2),
            "source_refs": [
                _source("octo-org:alpha-tree", 2).model_dump(mode="json")
            ],
        }
    )
    _correct_gather(
        engine,
        portfolio_scope,
        "portfolio",
        scope_b,
        "bravo-root",
        minute=4,
    )
    _correct_gather(
        engine,
        portfolio_scope,
        "portfolio",
        scope_a,
        "alpha-root",
        minute=3,
    )

    view = engine.gather_view(portfolio_scope, "portfolio")
    assert [item.scope_id for item in view.members_by_scope] == [scope_a, scope_b]
    assert view.members_by_scope[0].members[0].rollup.descendants_total == 1
    assert view.aggregate.total == 3
    assert view.aggregate.completed == 1
    assert view.aggregate.blocked == 1
    assert view.aggregate.bubbled_blockers == [
        {
            "scope_id": scope_b,
            "subject_key": "bravo-root",
            "reference": f"{scope_b}:bravo-root",
            "blocker": ["Dana review"],
        }
    ]
    assert {item.subject_key for item in engine.matters(scope_a)} == {
        "alpha-root"
    }

    engine.correct(
        {
            "scope_id": portfolio_scope,
            "subject_key": "portfolio",
            "subject_type": "MATTER",
            "predicate": "part_of",
            "object_value": "portfolio-parent",
            "valid_from": NOW + timedelta(minutes=5),
            "source_refs": [
                _source("octo-org:portfolio-tree", 5).model_dump(mode="json")
            ],
        }
    )
    assert "portfolio" in {
        item.subject_key for item in engine.matters(portfolio_scope)
    }

    before = canonical_json(view.to_dict())
    engine.replay(portfolio_scope)
    engine.replay(scope_a)
    engine.replay(scope_b)
    assert canonical_json(
        engine.gather_view(portfolio_scope, "portfolio").to_dict()
    ) == before


def test_gather_rest_creation_correction_and_projection_shapes(tmp_path) -> None:
    engine = Engine(
        SQLiteStore(tmp_path / "gather-rest.db"),
        clock=lambda: NOW + timedelta(hours=1),
    )
    target_scope = "octo-gather-rest-target"
    portfolio_scope = "octo-gather-rest-portfolio"
    engine._ingest_cards_sync([_card(target_scope, "member", status="done")])

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=create_app(engine=engine))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            created = await client.post(
                f"/v1/scopes/{portfolio_scope}/cards",
                json={
                    "cards": [
                        EpisodeCard.model_validate(
                            _card(portfolio_scope, "portfolio", layer=2)
                        ).model_dump(mode="json")
                    ],
                    "wait": True,
                },
            )
            assert created.status_code == 200
            corrected = await client.post(
                f"/v1/scopes/{portfolio_scope}/corrections",
                json={
                    "subject_key": "portfolio",
                    "subject_type": "MATTER",
                    "predicate": "gathers",
                    "object_value": encode_subject_ref(target_scope, "member"),
                    "valid_from": NOW.isoformat(),
                    "source_refs": [
                        _source("octo-org:rest:gather").model_dump(mode="json")
                    ],
                },
            )
            assert corrected.status_code == 200
            response = await client.get(
                f"/v1/scopes/{portfolio_scope}/matters/portfolio/gather"
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["subject"]["layer"] == 2
            assert payload["members_by_scope"][0]["scope_id"] == target_scope
            assert payload["aggregate"] == {
                "total": 1,
                "completed": 1,
                "blocked": 0,
                "bubbled_blockers": [],
                "latest_activity": "2026-08-07T10:00:00Z",
            }

    try:
        import asyncio

        asyncio.run(exercise())
        subject = engine.store.subjects(portfolio_scope)[0]
        assert subject.layer == 2
    finally:
        engine.store.close()


def test_console_template_contains_gather_project_controls(tmp_path) -> None:
    engine = Engine(SQLiteStore(tmp_path / "gather-console.db"))
    try:
        html = TestClient(
            create_app(engine=engine, console_enabled=True)
        ).get("/console").text
        assert "Gathered membership" in html
        assert "Gather into project" in html
        assert 'predicate: "gathers"' in html
        assert "gather_members_by_scope" in html
    finally:
        engine.store.close()
