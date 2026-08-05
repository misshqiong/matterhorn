from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from typer.testing import CliRunner

from matterhorn.api import create_app
from matterhorn.cli.app import app
from matterhorn.connectors.mail import MailConfig, MailRuntime, MailSyncReport
from matterhorn.contracts import Record, Signal
from matterhorn.defaults import Engine
from matterhorn.store import SQLiteStore

NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)


def _message(
    native_id: str,
    text: str,
    *,
    sender_id: str = "ellis",
    minute: int = 0,
) -> dict[str, object]:
    return {
        "id": native_id,
        "sender": {"id": sender_id, "name": "Ellis Stone"},
        "text": text,
        "sent_at": NOW + timedelta(minutes=minute),
        "conversation_id": "octo-room",
    }


def _card(
    subject_key: str,
    *,
    status: str | None = None,
    progress: str | None = None,
    next_step: str | None = None,
    blocker: str | None = None,
    source_id: str | None = None,
) -> dict[str, object]:
    return {
        "card_id": f"card-{subject_key}-{progress or status or 'plain'}",
        "scope_id": "octo-team",
        "subject_key": subject_key,
        "date": date(2026, 8, 5),
        "title": f"Fictional {subject_key}",
        "status": status,
        "progress": progress,
        "next_step": next_step,
        "blocker": blocker,
        "source_refs": [
            {
                "source_id": source_id or f"{subject_key}:r1",
                "sent_at": NOW,
                "sender": "Dana Reyes",
            }
        ],
    }


def test_detectors_fire_in_fixed_order_and_enforce_both_guards(tmp_path) -> None:
    engine = Engine(
        tmp_path / "detectors.db",
        clock=lambda: NOW,
        identity_handles=["Dana", "258"],
    )
    messages = [
        _message("mention", "@Dana please review the fictional note."),
        _message(
            "machine",
            "Security failure in the fictional sandbox.",
            sender_id="no-reply",
            minute=1,
        ),
        _message("human-alert", "Security alert from a human.", minute=2),
        _message("machine-normal", "All checks passed.", sender_id="notification", minute=3),
        _message("bare-digit", "A bare 258 must not match.", minute=4),
        _message("guarded-digit", "Please route :258 now.", minute=5),
    ]

    engine.add("octo-team", messages)

    assert {
        (item.record_id.rsplit(":", 1)[-1], item.kind, item.matched_text)
        for item in engine.signals()
    } == {
        ("mention", "mention_of_self", "Dana"),
        ("machine", "machine_alert", "Security"),
        ("guarded-digit", "mention_of_self", "258"),
    }
    assert engine.store.subjects("octo-team") == []
    assert engine.store.assertions("octo-team") == []


def test_identity_unset_disables_mentions(tmp_path) -> None:
    engine = Engine(tmp_path / "unset.db", clock=lambda: NOW)
    engine.add("octo-team", [_message("unset", "@Dana review this.")])
    assert engine.signals() == []


def test_signal_replay_is_idempotent_and_ack_is_terminal(tmp_path) -> None:
    engine = Engine(
        tmp_path / "terminal.db",
        clock=lambda: NOW,
        identity_handles=["Dana"],
    )
    message = _message("terminal", "Dana should acknowledge this.")
    engine.add("octo-team", [message])
    first_ack = NOW + timedelta(minutes=1)
    engine.acknowledge_signal(
        "octo-team",
        "octo-team:octo-room:terminal",
        "mention_of_self",
        acked_at=first_ack,
    )
    engine.add("octo-team", [message])
    second = engine.acknowledge_signal(
        "octo-team",
        "octo-team:octo-room:terminal",
        "mention_of_self",
        acked_at=NOW + timedelta(minutes=2),
    )

    assert len(engine.signals()) == 1
    assert second.acked_at == first_ack
    engine.replay("octo-team")
    assert engine.signals()[0].acked_at == first_ack


def test_critical_matcher_digit_guard_longest_tie_and_closed_subject(tmp_path) -> None:
    clock_values = iter(NOW + timedelta(minutes=index) for index in range(20))
    engine = Engine(
        tmp_path / "critical.db",
        clock=lambda: next(clock_values),
        identity_handles=["Dana"],
    )
    engine._ingest_cards_sync(
        [
            _card("alpha", status="closed"),
            _card("beta"),
            _card("zeta"),
        ]
    )
    refs = [{"source_id": "handle-proof", "sent_at": NOW, "sender": "Dana Reyes"}]
    engine.bind_handle("octo-team", "alpha", "pull_request", "PR #1000", source_refs=refs)
    engine.bind_handle("octo-team", "beta", "order", "order 1000", source_refs=refs)
    engine.bind_handle("octo-team", "zeta", "issue", "OCT-1000", source_refs=refs)

    engine.add(
        "octo-team",
        [
            _message("bare", "Dana noted bare 1000."),
            _message("tie", "Dana reopened #1000.", minute=1),
            _message("long", "Dana compared #1000 with OCT-1000.", minute=2),
        ],
    )
    linked = {item.record_id.rsplit(":", 1)[-1]: item.subject_key for item in engine.signals()}
    assert linked == {"bare": None, "tie": "alpha", "long": "zeta"}


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_store_signal_watermark_hotness_and_mail_report_parity(
    backend: str, tmp_path
) -> None:
    scope = "octo-store-signals"
    if backend == "sqlite":
        store = SQLiteStore(tmp_path / "store-signals.db")
    else:
        dsn = os.environ.get("MATTERHORN_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("MATTERHORN_TEST_POSTGRES_DSN is unset; PostgreSQL signal store skipped")
        from matterhorn.store.postgres import PostgresStore

        store = PostgresStore(dsn)
    try:
        store.clear_scope(scope)
        signal = Signal(
            scope_id=scope,
            record_id="hot-room:signal",
            kind="mention_of_self",
            detected_at=NOW,
            matched_text="Dana",
        )
        assert store.add_signal(signal) is True
        assert store.add_signal(signal) is False
        acked = store.acknowledge_signal(
            scope, signal.record_id, signal.kind, acked_at=NOW + timedelta(minutes=1)
        )
        assert acked is not None
        again = store.acknowledge_signal(
            scope, signal.record_id, signal.kind, acked_at=NOW + timedelta(minutes=2)
        )
        assert again is not None and again.acked_at == NOW + timedelta(minutes=1)

        assert store.set_read_watermark(scope, "matter", last_seen_at=NOW) == NOW
        assert store.set_read_watermark(
            scope, "matter", last_seen_at=NOW - timedelta(minutes=1)
        ) == NOW

        records = [
            Record(
                record_id=f"hot-room:r{index}",
                container_id="hot-room",
                sent_at=NOW + timedelta(minutes=index),
                author={"id": f"author-{index % 3}", "kind": "human"},
                content=f"Fictional hotness message {index}.",
                reactions=[{"name": "check", "count": 1}],
            )
            for index in range(5)
        ]
        records.extend(
            Record(
                record_id=f"cold-room:r{index}",
                container_id="cold-room",
                sent_at=NOW + timedelta(minutes=(index if index < 4 else 30)),
                author={"id": f"author-{index % 3}", "kind": "human"},
                content=f"Fictional cold-edge message {index}.",
            )
            for index in range(5)
        )
        store.stage_records(scope, records, staged_at=NOW)
        hotness = store.conversation_hotness(
            [scope],
            window_start=NOW,
            window_end=NOW + timedelta(minutes=60),
            min_authors=3,
            min_messages=5,
        )
        assert [
            (
                item.container_id,
                item.message_count,
                item.distinct_authors,
                item.reaction_total,
                item.hot,
            )
            for item in hotness
        ] == [
            ("cold-room", 5, 3, 0, False),
            ("hot-room", 5, 3, 5, True),
        ]
        report = {"filtered": 2, "filtered_by_reason": {"LIST_MAIL": 2}}
        store.save_mail_runtime_report(
            "fictional-mailbox", scope, report, updated_at=NOW
        )
        assert store.mail_runtime_report("fictional-mailbox") == report
        store.clear_scope(scope)
        assert store.signals(scope) == []
        assert store.read_watermarks(scope) == {}
        assert store.mail_runtime_report("fictional-mailbox") is None
    finally:
        store.close()


def test_brief_orders_unseen_activity_and_uses_monotonic_watermark(tmp_path) -> None:
    clock_values = iter(
        [
            datetime(2026, 8, 5, 9, 10, tzinfo=UTC),
            datetime(2026, 8, 5, 9, 20, tzinfo=UTC),
            datetime(2026, 8, 5, 9, 30, tzinfo=UTC),
        ]
    )
    engine = Engine(
        tmp_path / "brief.db",
        clock=lambda: next(clock_values),
        identity_handles=["Dana"],
    )
    engine._ingest_cards_sync(
        [
            _card("blocked", status="blocked", progress="Waiting", blocker="certificate"),
            _card("completed", status="completed", progress="Finished"),
            _card("active", status="open", progress="Drafted", next_step="Dana reviews it"),
        ]
    )
    watermark = datetime(2026, 8, 5, 9, 15, tzinfo=UTC)
    engine.set_seen("octo-team", "blocked", last_seen_at=watermark)
    engine.set_seen(
        "octo-team", "blocked", last_seen_at=watermark - timedelta(minutes=5)
    )

    brief = engine.brief(
        datetime(2026, 8, 5, 9, tzinfo=UTC),
        datetime(2026, 8, 5, 10, tzinfo=UTC),
        console_groups={"team": ["octo-*"]},
    )

    group = brief["groups"][0]
    assert group["counts"] == {"touched": 3, "completed": 1, "blocked": 1}
    assert [item["subject_key"] for item in group["matters"]] == [
        "active",
        "completed",
        "blocked",
    ]
    assert group["matters"][-1]["unseen"] == 0
    assert brief["needs_me"][-1]["subject_key"] == "active"


def test_rest_brief_seen_signals_ack_and_console_markup(tmp_path) -> None:
    async def scenario() -> None:
        engine = Engine(
            tmp_path / "rest-signals.db",
            clock=lambda: NOW,
            identity_handles=["Dana"],
        )
        engine._ingest_cards_sync([_card("rest-matter", status="open")])
        engine.add("octo-team", [_message("rest-signal", "@Dana review OCT-404.")])
        application = create_app(
            engine=engine,
            console_enabled=True,
            console_groups={"team": ["octo-*"]},
        )
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://matterhorn.test"
        ) as client:
            brief = await client.get(
                "/v1/console/brief",
                params={
                    "window_start": "2026-08-05T00:00:00Z",
                    "window_end": "2026-08-06T00:00:00Z",
                },
            )
            assert brief.status_code == 200
            assert brief.json()["groups"][0]["name"] == "team"
            signals = await client.get("/v1/signals", params={"status": "open"})
            assert signals.status_code == 200 and len(signals.json()) == 1
            signal = signals.json()[0]
            ack = await client.post(
                "/v1/signals/ack",
                json={
                    "scope_id": signal["scope_id"],
                    "record_id": signal["record_id"],
                    "kind": signal["kind"],
                    "acked_at": "2026-08-05T12:01:00Z",
                },
            )
            assert ack.status_code == 200 and ack.json()["status"] == "acked"
            seen = await client.post(
                "/v1/matters/rest-matter/seen",
                json={"scope_id": "octo-team", "last_seen_at": "2026-08-05T12:02:00Z"},
            )
            assert seen.status_code == 200
            matters = await client.get("/v1/matters", params={"scope": "octo-team"})
            assert matters.json()[0]["unseen"] is False
            page = await client.get("/console")
            assert "需要我" in page.text
            assert "pollBrief" in page.text
            assert "/seen" in page.text

    asyncio.run(scenario())


def test_cli_loads_signal_config_and_prints_brief(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "matterhorn.toml").write_text(
        """db = "brief-cli.db"
scope = "octo-team"
[identity]
handles = ["Dana"]
[signals]
machine_senders = ["octobot"]
alert_keywords = ["urgent"]
hot_min_authors = 2
hot_min_messages = 4
[console.groups]
team = ["octo-*"]
""",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["brief"])
    assert result.exit_code == 0
    assert "需要我" in result.stdout
    assert "Groups" in result.stdout


def test_mail_filter_audit_report_reloads_after_restart(tmp_path) -> None:
    engine = Engine(tmp_path / "mail-report.db", clock=lambda: NOW)
    config = MailConfig(
        provider="manual",
        host="imap.octo-org.example",
        port=993,
        ssl=True,
        user="dana@octo-org.example",
        scope="octo-mail",
        name="fictional-mailbox",
    )
    runtime = MailRuntime(
        engine,
        config_path=tmp_path / "matterhorn.toml",
        config=config,
        environment={},
        persist_config=False,
    )
    report = MailSyncReport(
        scope_id="octo-mail",
        account="dana@octo-org.example",
        folder="INBOX",
        container_id=config.container_id,
        pulled=3,
        filtered=2,
        filtered_by_reason={"LIST_MAIL": 2},
        parse_errors=0,
        effective_window=5,
        cards_produced=1,
        new_assertions=2,
        new_matters=1,
        new_watermark=42,
        uidvalidity="7",
        previous_uidvalidity=None,
        reset_detected=False,
        backfill=False,
    )
    runtime.last_run_at = NOW
    runtime._persist_report(report)

    restarted = MailRuntime(
        engine,
        config_path=tmp_path / "matterhorn.toml",
        config=config,
        environment={},
        persist_config=False,
    )
    assert restarted.status()["last_report"]["filtered_by_reason"] == {
        "LIST_MAIL": 2
    }
