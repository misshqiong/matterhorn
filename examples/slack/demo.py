from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from matterhorn import Engine
from matterhorn.adapters import (
    MessageCardExtractor,
    map_slack_event,
    map_slack_history,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "slack"
WORKSPACE = "matterhorn.slack.com"
USERS = {
    "U123": {"profile": {"display_name": "Ada"}},
    "U456": {"profile": {"display_name": "Bob"}},
}


class FixtureGateway:
    """Deterministic offline extraction fixture; no token or network."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = iter(responses)

    def complete(self, **_kwargs) -> str:
        return json.dumps(next(self._responses))


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def card_response(source_id: str, title: str = "Release") -> dict:
    return {
        "cards": [
            {
                "date": "2023-11-13",
                "title": title,
                "status": "open",
                "occurred_at": "2023-11-13T15:00:54.123456Z",
                "source_ids": [source_id],
            }
        ]
    }


def dump(label: str, value) -> None:
    print(f"{label}:")
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    )


def main() -> None:
    history = map_slack_history(
        load("conversations-history.json"),
        channel_id="C0123",
        workspace_domain=WORKSPACE,
        users=USERS,
    )
    root = history.records[0]
    initial_response = card_response(root.record_id)

    preview = MessageCardExtractor(
        FixtureGateway([initial_response]),
        "org-matters/v1",
    ).extract(scope_id="slack-demo", records=history.records)
    dump(
        "Slack payload -> Records",
        {
            "record_ids": [record.record_id for record in history.records],
            "permalinks": [record.uri for record in history.records],
            "adapter_dropped": history.dropped,
        },
    )
    dump(
        "Records -> extracted EpisodeCards",
        [card.model_dump(mode="json") for card in preview.cards],
    )

    with tempfile.TemporaryDirectory() as directory:
        engine = Engine(
            Path(directory) / "memory.db",
            "org-matters/v1",
            gateway=FixtureGateway(
                [
                    initial_response,
                    card_response(root.record_id),
                ]
            ),
            clock=[
                datetime(2026, 7, 29, 9, 0, tzinfo=UTC),
                datetime(2026, 7, 29, 9, 5, tzinfo=UTC),
            ],
        )
        initial_report = engine.add_records(
            history.records,
            scope_id="slack-demo",
            cursors={"C0123": history.next_cursor or "end"},
        )
        subject = engine.query.list_matters("slack-demo")[0]
        initial_answer = engine.query.current(
            "slack-demo",
            subject.subject_key,
            "status",
        )[0]
        dump(
            "normal deterministic ingest -> query_current",
            {
                "report": initial_report.model_dump(mode="json"),
                "answer": initial_answer.to_dict(),
            },
        )

        edited = map_slack_event(
            load("message-changed.json"),
            workspace_domain=WORKSPACE,
            users=USERS,
        )
        assert edited is not None
        edit_report = engine.add_records(
            [edited],
            scope_id="slack-demo",
            cursors={"C0123": "events:Ev123"},
        )
        assertions_after_edit = [
            assertion
            for assertion in engine.store.assertions("slack-demo")
            if assertion.predicate == "status"
        ]
        answer_after_edit = engine.query.current(
            "slack-demo",
            subject.subject_key,
            "status",
        )[0]
        dump(
            "edited message -> new observation; old assertion survives",
            {
                "report": edit_report.model_dump(mode="json"),
                "assertions": [
                    {
                        "assertion_id": item.assertion_id,
                        "observation_id": item.observation_id,
                        "recorded_at": item.recorded_at,
                        "value": item.object_value,
                    }
                    for item in assertions_after_edit
                ],
                "answer": answer_after_edit.to_dict(),
            },
        )

        deleted = map_slack_event(
            load("message-deleted.json"),
            workspace_domain=WORKSPACE,
            prior_record=edited,
        )
        assert deleted is not None
        delete_report = engine.add_records(
            [deleted],
            scope_id="slack-demo",
            cursors={"C0123": "events:Ev124"},
        )
        answer_after_delete = engine.query.current(
            "slack-demo",
            subject.subject_key,
            "status",
        )[0]
        dump(
            "deleted message -> assertion retained, evidence revoked",
            {
                "report": delete_report.model_dump(mode="json"),
                "assertion_count_before": len(assertions_after_edit),
                "assertion_count_after": len(
                    [
                        item
                        for item in engine.store.assertions("slack-demo")
                        if item.predicate == "status"
                    ]
                ),
                "answer": answer_after_delete.to_dict(),
            },
        )

    other = map_slack_history(
        load("same-ts-other-channel.json"),
        channel_id="C9999",
        workspace_domain=WORKSPACE,
        users=USERS,
    ).records[0]
    with tempfile.TemporaryDirectory() as directory:
        isolation = Engine(
            Path(directory) / "isolation.db",
            "org-matters/v1",
            gateway=FixtureGateway(
                [
                    card_response(root.record_id, "Same-ts first"),
                    card_response(other.record_id, "Same-ts second"),
                ]
            ),
        )
        isolation.add_records([root], scope_id="same-ts")
        isolation.add_records([other], scope_id="same-ts")
        subjects = isolation.query.list_matters("same-ts")
        dump(
            "cross-channel same ts is not shared evidence",
            {
                "native_ids": [root.native_id, other.native_id],
                "record_ids": [root.record_id, other.record_id],
                "shared_source_ids": sorted(
                    {root.record_id}.intersection({other.record_id})
                ),
                "matter_count": len(subjects),
                "matter_keys": [item.subject_key for item in subjects],
            },
        )


if __name__ == "__main__":
    main()
