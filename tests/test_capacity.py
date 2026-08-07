from __future__ import annotations

from matterhorn.capacity import (
    ActivationWeights,
    CapacitySettings,
    LossWeights,
    matter_activation,
    resolve_capacity,
)


def test_capacity_registry_resolution_precedence() -> None:
    config = {
        "capacity": {"layer_depth": 2},
        "themes": {
            "theme_min_cluster": 4,
            "theme_min_backlog": 7,
            "theme_conversation_fanout": 9,
            "human_edge_weight": 11,
        },
        "signals": {"hot_min_authors": 5, "hot_min_messages": 8},
        "wall": {
            "deck_width": 6,
            "activation_weights": {"blocked": 40, "unseen": 30},
        },
        "eval": {"loss_weights": {"missing": 2, "mis_structured": 6}},
    }
    environment = {
        "MATTERHORN_THEME_MIN_CLUSTER": "5",
        "MATTERHORN_HOT_MIN_AUTHORS": "6",
        "MATTERHORN_DECK_WIDTH": "7",
        "MATTERHORN_ACTIVATION_WEIGHTS": '{"blocked": 50, "recency": 3}',
        "MATTERHORN_LOSS_WEIGHT_MISSING": "4",
    }

    resolved = resolve_capacity(
        config=config,
        environment=environment,
        explicit={
            "theme_min_cluster": 8,
            "activation_weights": {"blocked": 60},
            "loss_weights": {"missing": 5},
        },
    )

    assert resolved == CapacitySettings(
        layer_depth=2,
        theme_min_cluster=8,
        theme_min_backlog=7,
        conversation_fanout=9,
        hot_min_authors=6,
        hot_min_messages=8,
        human_edge_weight=11,
        activation_weights=ActivationWeights(
            blocked=60,
            unseen=30,
            recency=3,
            hotness=0,
            pinned=0,
        ),
        loss_weights=LossWeights(
            missing=5,
            spurious=1,
            mis_attached=1,
            mis_typed=1,
            mis_structured=6,
        ),
        deck_width=7,
    )


def test_default_activation_matches_pre_s4_deck_order() -> None:
    rows = [
        {
            "subject_key": "recent",
            "bubbled_blockers": [],
            "unseen": False,
            "latest_activity": "2026-08-07T12:00:00Z",
        },
        {
            "subject_key": "unseen",
            "bubbled_blockers": [],
            "unseen": True,
            "latest_activity": "2026-08-01T12:00:00Z",
        },
        {
            "subject_key": "blocked-one",
            "bubbled_blockers": [{}],
            "unseen": False,
            "latest_activity": "2026-07-01T12:00:00Z",
        },
        {
            "subject_key": "blocked-two",
            "bubbled_blockers": [{}, {}],
            "unseen": False,
            "latest_activity": "2026-06-01T12:00:00Z",
        },
    ]

    legacy = sorted(
        rows,
        key=lambda row: (
            len(row["bubbled_blockers"]),
            int(row["unseen"]),
            row["latest_activity"],
        ),
        reverse=True,
    )
    activated = sorted(rows, key=matter_activation, reverse=True)

    assert [row["subject_key"] for row in activated] == [
        row["subject_key"] for row in legacy
    ]
