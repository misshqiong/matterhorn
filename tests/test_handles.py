from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from matterhorn import Engine
from matterhorn.api import create_app
from matterhorn.contracts.schema import resolve_schema
from matterhorn.engine.handles import normalize_handle, scan_handles

NOW = datetime(2026, 8, 4, 14, tzinfo=UTC)


class SequenceGateway:
    def __init__(self, responses):
        self.responses = iter(responses)

    def complete(self, **_kwargs):
        return json.dumps(next(self.responses))


def _source(source_id: str, excerpt: str | None = None) -> dict:
    return {
        "source_id": source_id,
        "sent_at": "2026-08-04T13:55:00Z",
        "sender": "Dana Reyes",
        "excerpt": excerpt,
    }


def _card(
    subject_key: str = "fictional-target",
    *,
    title: str = "Fictional release",
    status: str = "open",
    source_ref: dict | None = None,
) -> dict:
    return {
        "card_id": f"card-{subject_key}-{status}",
        "scope_id": "fictional-team",
        "subject_key": subject_key,
        "date": "2026-08-04",
        "title": title,
        "status": status,
        "source_refs": [source_ref or _source(f"source-{subject_key}")],
    }


def test_shipped_patterns_normalize_structured_ids_and_reject_low_entropy() -> None:
    profile = resolve_schema("org-matters/v1")
    assert normalize_handle(profile, "pull_request", "PR #00456") == "456"
    assert normalize_handle(profile, "thread_id", "Thread-ID: 路线-ABC") == "路线-abc"
    assert scan_handles(
        profile,
        "Dana Reyes approved 7319 and an amount of $42.00.",
        [],
    ) == []
    assert scan_handles(
        profile,
        (
            "preorder 7319, APR #456, preinvoice INV-2026-001, "
            "XCA1234-Y, NotMessage-ID: <route@octo.example>, and "
            "NotThread-ID: route-1 are not handles."
        ),
        [],
    ) == []
    assert [
        (item.handle_type, item.normalized_value)
        for item in scan_handles(
            profile,
            "OCT-9021 tracks order 0007319 and flight CA4321.",
            [],
        )
    ] == [
        ("issue", "oct-9021"),
        ("order", "7319"),
        ("flight", "ca4321"),
    ]


def test_human_bind_rejects_a_bare_number(tmp_path) -> None:
    engine = Engine(tmp_path / "entropy.db", clock=lambda: NOW)
    engine._ingest_cards_sync([_card()])

    with pytest.raises(ValueError, match="structured-identifier pattern"):
        engine.bind_handle(
            "fictional-team",
            "fictional-target",
            "order",
            "7319",
            source_refs=[_source("bare-number")],
        )


def test_human_bind_accepts_marker_only_merge_request(tmp_path) -> None:
    engine = Engine(tmp_path / "marker-handle.db", clock=lambda: NOW)
    engine._ingest_cards_sync([_card()])

    handle = engine.bind_handle(
        "fictional-team",
        "fictional-target",
        "pull_request",
        "(!00456)",
        source_refs=[_source("marker-handle")],
    )

    assert handle.handle_value == "(!00456)"
    assert handle.normalized_value == "456"


def test_human_unbind_and_rebind_preserve_history(tmp_path) -> None:
    engine = Engine(tmp_path / "history.db", clock=lambda: NOW)
    engine._ingest_cards_sync([_card()])
    first = engine.bind_handle(
        "fictional-team",
        "fictional-target",
        "pull_request",
        "PR #00456",
        source_refs=[_source("bind-one")],
    )
    revoked = engine.unbind_handle(
        "fictional-team",
        "fictional-target",
        "pull_request",
        "456",
        source_refs=[_source("unbind-one")],
    )
    second = engine.bind_handle(
        "fictional-team",
        "fictional-target",
        "pull_request",
        "MR #00456",
        source_refs=[_source("bind-two")],
    )

    assert revoked.binding_id == first.binding_id
    assert revoked.revocation_origin.value == "human"
    assert second.binding_id != first.binding_id
    assert engine.handle_lookup("fictional-team", "456", "pull_request") == [
        second
    ]
    history = engine.store.subject_handle_bindings("fictional-team")
    assert len(history) == 2
    assert history[0].revoked_at == NOW
    assert history[1].revoked_at is None


def test_merge_handle_reads_are_canonical_and_unmerge_restores(tmp_path) -> None:
    engine = Engine(tmp_path / "merge-handles.db", clock=lambda: NOW)
    engine._ingest_cards_sync(
        [
            _card("canonical", title="Canonical fictional release"),
            _card("duplicate", title="Duplicate fictional release"),
        ]
    )
    canonical_handle = engine.bind_handle(
        "fictional-team",
        "canonical",
        "issue",
        "OCT-9102",
        source_refs=[_source("canonical-handle")],
    )
    engine.bind_handle(
        "fictional-team",
        "duplicate",
        "issue",
        "OCT-9101",
        source_refs=[_source("duplicate-handle")],
    )
    engine.merge_subjects(
        "fictional-team",
        "duplicate",
        "canonical",
        source_refs=[_source("merge-reason")],
        valid_from=NOW,
    )

    assert [item.normalized_value for item in engine.subject_handles(
        "fictional-team", "canonical"
    )] == ["oct-9101", "oct-9102"]
    assert engine.handle_lookup("fictional-team", "OCT-9101", "issue")[
        0
    ].subject_key == "canonical"

    engine.unmerge_subjects(
        "fictional-team",
        "duplicate",
        source_refs=[_source("unmerge-reason")],
        valid_from=NOW,
    )
    assert engine.subject_handles("fictional-team", "canonical") == [
        canonical_handle
    ]
    assert engine.handle_lookup("fictional-team", "OCT-9101", "issue")[
        0
    ].subject_key == "duplicate"


def test_backfill_is_offline_and_idempotent(tmp_path) -> None:
    engine = Engine(tmp_path / "backfill.db", clock=lambda: NOW)
    engine._ingest_cards_sync(
        [
            _card(
                title="Invoice INV-2026-007",
                source_ref=_source(
                    "backfill-source",
                    "OCT-9201 is the fictional invoice issue.",
                ),
            )
        ]
    )

    first = engine.backfill_handles("fictional-team")
    second = engine.backfill_handles("fictional-team")

    assert first.bound == 2
    assert first.skipped_conflict == 0
    assert second.bound == 0
    assert second.already_bound == 2
    assert {
        (item.handle_type, item.normalized_value)
        for item in engine.subject_handles("fictional-team", "fictional-target")
    } == {("invoice", "inv-2026-007"), ("issue", "oct-9201")}


def test_backfill_preserves_pre_merge_evidence_ownership(tmp_path) -> None:
    engine = Engine(tmp_path / "backfill-merge.db", clock=lambda: NOW)
    engine._ingest_cards_sync(
        [
            _card(
                "canonical",
                title="Canonical fictional release",
                source_ref=_source("canonical-evidence", "OCT-9251 is canonical."),
            ),
            _card(
                "duplicate",
                title="Duplicate fictional release",
                source_ref=_source("duplicate-evidence", "OCT-9252 is duplicate."),
            ),
        ]
    )
    engine.merge_subjects(
        "fictional-team",
        "duplicate",
        "canonical",
        source_refs=[_source("backfill-merge-reason")],
        valid_from=NOW,
    )

    report = engine.backfill_handles("fictional-team")

    assert report.bound == 2
    assert {
        (item.normalized_value, item.subject_key)
        for item in engine.store.subject_handle_bindings("fictional-team")
    } == {("oct-9251", "canonical"), ("oct-9252", "duplicate")}
    assert engine.handle_lookup("fictional-team", "OCT-9252", "issue")[
        0
    ].subject_key == "canonical"

    engine.unmerge_subjects(
        "fictional-team",
        "duplicate",
        source_refs=[_source("backfill-unmerge-reason")],
        valid_from=NOW,
    )
    assert engine.handle_lookup("fictional-team", "OCT-9252", "issue")[
        0
    ].subject_key == "duplicate"


def test_message_flush_exposes_handle_conflicts_in_task_gate(tmp_path) -> None:
    gateway = SequenceGateway(
        [
            {
                "cards": [
                    {
                        "date": "2026-08-04",
                        "title": "First fictional issue",
                        "progress": "First note.",
                        "source_ids": ["fictional-team:first:r1"],
                    }
                ]
            },
            {
                "cards": [
                    {
                        "date": "2026-08-04",
                        "title": "Second fictional issue",
                        "progress": "Second note.",
                        "source_ids": ["fictional-team:second:r2"],
                    }
                ]
            },
            {"candidates": []},
            {"candidates": []},
        ]
    )
    engine = Engine(tmp_path / "task-conflict.db", gateway=gateway, clock=lambda: NOW)
    receipt = engine.add(
        "fictional-team",
        [
            {
                "id": "r1",
                "sender": {"id": "dana", "name": "Dana Reyes"},
                "text": "OCT-9301 belongs to the first fictional issue.",
                "sent_at": "2026-08-04T13:00:00Z",
                "conversation_id": "first",
            },
            {
                "id": "r2",
                "sender": {"id": "ellis", "name": "Ellis Stone"},
                "text": "OCT-9301 was quoted for the second fictional issue.",
                "sent_at": "2026-08-04T13:01:00Z",
                "conversation_id": "second",
            },
        ],
    )
    engine.flush("fictional-team")

    task = engine.task(receipt.task_id)
    assert task.status.value == "completed"
    assert task.gate.handle_conflicts == 1
    assert engine.gate_statistics("fictional-team").handle_conflicts == 1


def test_rest_handle_bind_unbind_and_detail_are_additive(tmp_path) -> None:
    engine = Engine(tmp_path / "rest-handles.db", clock=lambda: NOW)
    engine._ingest_cards_sync([_card()])

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(engine=engine))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            path = "/v1/scopes/fictional-team/subjects/fictional-target/handles"
            bound = await client.post(
                path,
                json={
                    "handle_type": "issue",
                    "handle_value": "OCT-9401",
                    "source_refs": [_source("rest-bind")],
                },
            )
            assert bound.status_code == 200
            listed = await client.get(path)
            assert listed.json()[0]["normalized_value"] == "oct-9401"
            detail = await client.get(
                "/v1/scopes/fictional-team/matters/fictional-target"
            )
            assert detail.json()["handles"][0]["handle_value"] == "OCT-9401"

            unbound = await client.post(
                f"{path}/issue/oct-9401/unbind",
                json={"source_refs": [_source("rest-unbind")]},
            )
            assert unbound.status_code == 200
            assert unbound.json()["revocation_origin"] == "human"
            assert (await client.get(path)).json() == []

            message_id = await client.post(
                path,
                json={
                    "handle_type": "message_id",
                    "handle_value": "Message-ID: <route/63@octo.example>",
                    "source_refs": [_source("rest-message-bind")],
                },
            )
            assert message_id.status_code == 200
            unbound_message = await client.post(
                f"{path}/message_id/route%2F63%40octo.example/unbind",
                json={"source_refs": [_source("rest-message-unbind")]},
            )
            assert unbound_message.status_code == 200

    asyncio.run(scenario())


def test_activity_is_derived_from_completion_status(tmp_path) -> None:
    engine = Engine(tmp_path / "activity.db", clock=lambda: NOW)
    engine._ingest_cards_sync(
        [
            _card("open-target", status="open"),
            _card("closed-target", status="closed"),
        ]
    )

    assert engine.subject_is_active("fictional-team", "open-target") is True
    assert engine.subject_is_active("fictional-team", "closed-target") is False
