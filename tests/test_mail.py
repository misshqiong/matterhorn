from __future__ import annotations

import asyncio
import imaplib
import json
import logging
import re as _re
import tomllib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from typer.testing import CliRunner


def _plain(text: str) -> str:
    """CLI output stripped of ANSI so assertions ignore terminal rendering."""

    return _re.sub(r"\x1b\[[0-9;]*m", "", text)

from matterhorn.api import create_app
from matterhorn.canonical import stable_hash
from matterhorn.cli.app import app as cli_app
from matterhorn.connectors.mail import (
    MAIL_PROVIDERS,
    MailAuthError,
    MailboxResetError,
    MailConfig,
    MailConnector,
    MailRuntime,
    MailRuntimeRegistry,
    MailSyncReport,
    load_mail_config,
    load_mail_configs,
    save_mail_config,
    save_mail_configs,
)
from matterhorn.contracts import EpisodeCard
from matterhorn.defaults import Engine


class EmptySemanticGateway:
    def complete(self, **kwargs):
        if kwargs["response_schema"].get("$id") == (
            "matterhorn-identity-adjudication/v1"
        ):
            return json.dumps(
                {
                    "decision": "new",
                    "subject_key": None,
                    "confidence": 1.0,
                    "evidence_source_ids": [],
                }
            )
        return json.dumps({"candidates": []})


class DeterministicMailExtractor:
    def extract(self, *, scope_id, records, context, batch_size, anchors):
        del context, batch_size, anchors
        return SimpleNamespace(
            cards=[
                EpisodeCard.model_validate(
                    {
                        "card_id": f"mail-card:{record.record_id}",
                        "scope_id": scope_id,
                        "date": record.sent_at.date(),
                        "title": record.content.splitlines()[0].removeprefix(
                            "Subject: "
                        ),
                        "status": "open",
                        "occurred_at": record.sent_at,
                        "source_refs": [record.to_source_ref()],
                        "thread_id": record.thread_id,
                    }
                )
                for record in records
            ],
            rejection_counts={},
        )


@dataclass
class FakeIMAP:
    uidvalidity: str
    messages: dict[int, bytes]
    auth_error: bool = False

    def __post_init__(self):
        self.searches: list[str] = []
        self.fetches: list[str] = []
        self.logged_out = False

    def login(self, user, password):
        del user
        if self.auth_error:
            raise imaplib.IMAP4.error(f"credential {password} was rejected")
        return "OK", [b"authenticated"]

    def select(self, folder, readonly=True):
        assert folder
        assert readonly is True
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name):
        assert name == "UIDVALIDITY"
        return "UIDVALIDITY", [self.uidvalidity.encode()]

    def uid(self, command, *args):
        if command.casefold() == "search":
            criteria = args[-1]
            self.searches.append(criteria)
            return "OK", [
                b" ".join(str(uid).encode() for uid in sorted(self.messages))
            ]
        if command.casefold() == "fetch":
            uid_set = str(args[0])
            self.fetches.append(uid_set)
            uids = [int(uid) for uid in uid_set.split(",")]
            response = []
            for uid in uids:
                response.extend(
                    [
                        (
                            f"{uid} (RFC822 {{{len(self.messages[uid])}}})".encode(),
                            self.messages[uid],
                        ),
                        b")",
                    ]
                )
            return "OK", response
        raise AssertionError(command)

    def logout(self):
        self.logged_out = True
        return "BYE", [b"logged out"]


def _engine(path) -> Engine:
    return Engine(
        path,
        gateway=EmptySemanticGateway(),
        extractor=DeterministicMailExtractor(),
        clock=lambda: datetime(2026, 7, 30, 8, tzinfo=UTC),
    )


def _eml(
    uid: int,
    *,
    automated: bool = False,
    subject: str | None = None,
    references: str | None = None,
) -> bytes:
    message = EmailMessage()
    message["Message-ID"] = f"<mail-{uid}@example.test>"
    message["Date"] = f"Thu, 30 Jul 2026 08:{uid % 60:02d}:00 +0000"
    message["From"] = "Dana Reyes <dana@example.test>"
    message["To"] = "team@example.test"
    message["Subject"] = subject or f"Mail matter {uid}"
    if references is not None:
        message["References"] = references
    if automated:
        message["Auto-Submitted"] = "auto-generated"
    message.set_content(f"Status for mail matter {uid} is open.")
    return message.as_bytes()


class ConversationMailGateway:
    def __init__(self):
        self.extraction_calls = 0

    def complete(self, *, system, user, response_schema):
        del system
        if "cards" not in response_schema.get("properties", {}):
            return json.dumps({"candidates": []})
        self.extraction_calls += 1
        records = json.loads(user)["records"]
        sent_at = records[-1]["record"]["sent_at"]
        return json.dumps(
            {
                "cards": [
                    {
                        "date": sent_at[:10],
                        "title": "Quarterly launch",
                        "progress": f"Conversation update {self.extraction_calls}",
                        "occurred_at": sent_at,
                        "source_ids": [
                            item["source_alias"] for item in records
                        ],
                    }
                ]
            }
        )


def _config(
    *,
    provider: str = "gmail",
    initial_window: int = 50,
) -> MailConfig:
    preset = MAIL_PROVIDERS[provider]
    return MailConfig(
        provider=provider,
        host=preset.host,
        port=preset.port,
        ssl=preset.ssl,
        user="dana@example.test",
        folder="INBOX",
        interval="off",
        initial_window=initial_window,
        scope="mail-scope",
    )


def _connector(
    engine,
    fake: FakeIMAP,
    *,
    provider: str = "gmail",
    initial_window: int = 50,
    secret="s3cret",
):
    return MailConnector(
        engine,
        _config(provider=provider, initial_window=initial_window),
        secret,
        imap_ssl_factory=lambda _host, _port: fake,
    )


def test_mocked_imap_advances_uid_watermark_filters_and_reports(tmp_path) -> None:
    engine = _engine(tmp_path / "mail.db")
    first_imap = FakeIMAP(
        "901",
        {
            5: _eml(5),
            6: _eml(6, automated=True),
        },
    )

    first = _connector(engine, first_imap).sync(scope_id="mail-scope")

    assert first.to_dict() == {
        "scope_id": "mail-scope",
        "account": "dana@example.test",
        "folder": "INBOX",
        "container_id": "imap:dana@example.test@imap.gmail.com/INBOX",
        "pulled": 1,
        "filtered": 1,
        "filtered_by_reason": {"auto-submitted": 1},
        "parse_errors": 0,
        "effective_window": 2,
        "cards_produced": 1,
        "new_assertions": 1,
        "new_matters": 1,
        "new_watermark": 6,
        "uidvalidity": "901",
        "previous_uidvalidity": None,
        "reset_detected": False,
        "backfill": False,
    }
    position = next(
        item
        for item in engine.sync_positions("mail-scope")
        if item.container_id == _config().container_id
    )
    assert position.uid_watermark == 6
    assert position.cursor == "901"

    second_imap = FakeIMAP(
        "901",
        {
            5: _eml(5),
            6: _eml(6, automated=True),
            7: _eml(7),
        },
    )
    second = _connector(engine, second_imap).sync(scope_id="mail-scope")

    assert second_imap.searches == ["7:*"]
    assert second.pulled == 1
    assert second.filtered == 0
    assert second.parse_errors == 0
    assert second.effective_window is None
    assert second.cards_produced == 1
    assert second.new_assertions == 1
    assert second.new_watermark == 7


def test_references_thread_accumulates_one_matter_across_two_syncs(
    tmp_path,
) -> None:
    gateway = ConversationMailGateway()
    engine = Engine(
        tmp_path / "conversation.db",
        gateway=gateway,
        clock=lambda: datetime(2026, 7, 30, 10, tzinfo=UTC),
    )
    thread_root = "<mail-1@example.test>"
    first_imap = FakeIMAP(
        "906",
        {
            1: _eml(
                1,
                subject="Quarterly launch",
            )
        },
    )

    first = _connector(engine, first_imap).sync(scope_id="mail-scope")
    second_imap = FakeIMAP(
        "906",
        {
            1: first_imap.messages[1],
            2: _eml(
                2,
                subject="回复: Quarterly launch",
                references=thread_root,
            ),
        },
    )
    second = _connector(engine, second_imap).sync(scope_id="mail-scope")

    expected_subject_key = "mail:" + stable_hash(
        [
            _config().container_id,
            "subject:quarterly launch",
        ]
    )[:20]
    matters = engine.query.list_matters("mail-scope")
    timeline = engine.query.timeline(
        "mail-scope",
        expected_subject_key,
        "progress",
    )
    assert first.new_matters == 1
    assert second.new_matters == 0
    assert gateway.extraction_calls == 2
    assert [matter.subject_key for matter in matters] == [expected_subject_key]
    assert [item.value for item in timeline] == [
        "Conversation update 1",
        "Conversation update 2",
    ]


def test_malformed_message_is_isolated_and_watermark_advances(tmp_path) -> None:
    engine = _engine(tmp_path / "parse-isolation.db")
    malformed = (
        b"Date: Thu, 30 Jul 2026 08:02:00 +0000\r\n"
        b"From: Dana Reyes <dana@example.test>\r\n"
        b"To: team@example.test\r\n"
        b"Subject: Missing traceable identity\r\n"
        b"\r\n"
        b"This message has no Message-ID.\r\n"
    )
    fake = FakeIMAP(
        "902",
        {
            1: _eml(1),
            2: malformed,
            3: _eml(3),
        },
    )

    report = _connector(engine, fake).sync(scope_id="mail-scope")

    assert report.pulled == 2
    assert report.filtered == 1
    assert report.parse_errors == 1
    assert report.new_watermark == 3
    assert report.cards_produced == 2
    assert fake.fetches == ["1,2,3"]


def test_first_sync_honors_initial_window_and_sets_watermark_to_mailbox_max(
    tmp_path,
) -> None:
    engine = _engine(tmp_path / "initial-window.db")
    fake = FakeIMAP(
        "903",
        {uid: _eml(uid) for uid in range(1, 6)},
    )

    report = _connector(engine, fake, initial_window=2).sync(
        scope_id="mail-scope"
    )

    assert fake.searches == ["ALL"]
    assert fake.fetches == ["4,5"]
    assert report.pulled == 2
    assert report.effective_window == 2
    assert report.new_watermark == 5


def test_imap_fetches_uids_in_batches(tmp_path) -> None:
    engine = _engine(tmp_path / "batched-fetch.db")
    fake = FakeIMAP(
        "904",
        {uid: _eml(uid) for uid in range(1, 26)},
    )

    report = _connector(engine, fake, initial_window=25).sync(
        scope_id="mail-scope"
    )

    assert report.pulled == 25
    assert fake.fetches == [
        ",".join(str(uid) for uid in range(1, 21)),
        ",".join(str(uid) for uid in range(21, 26)),
    ]


def test_backfill_ignores_initial_window_and_pulls_full_history(tmp_path) -> None:
    engine = _engine(tmp_path / "backfill.db")
    fake = FakeIMAP(
        "905",
        {uid: _eml(uid) for uid in range(1, 6)},
    )

    report = _connector(engine, fake, initial_window=2).sync(
        scope_id="mail-scope",
        backfill=True,
    )

    assert fake.searches == ["1:*"]
    assert fake.fetches == ["1,2,3,4,5"]
    assert report.pulled == 5
    assert report.effective_window is None
    assert report.new_watermark == 5


def test_uidvalidity_change_refuses_before_search_without_backfill(tmp_path) -> None:
    engine = _engine(tmp_path / "reset.db")
    _connector(engine, FakeIMAP("100", {10: _eml(10)})).sync(
        scope_id="mail-scope"
    )
    reset_imap = FakeIMAP("200", {1: _eml(1)})

    with pytest.raises(MailboxResetError) as caught:
        _connector(engine, reset_imap).sync(scope_id="mail-scope")

    assert reset_imap.searches == []
    assert caught.value.report.reset_detected is True
    assert caught.value.report.previous_uidvalidity == "100"
    assert caught.value.report.uidvalidity == "200"
    assert "--backfill" in str(caught.value)

    recovered = _connector(engine, reset_imap).sync(
        scope_id="mail-scope",
        backfill=True,
    )
    assert reset_imap.searches == ["1:*"]
    assert recovered.reset_detected is True
    assert recovered.new_watermark == 1
    position = next(
        item
        for item in engine.sync_positions("mail-scope")
        if item.container_id == _config().container_id
    )
    assert position.uid_watermark == 1
    assert position.cursor == "200"


def test_mailbox_reset_error_equal_to_password_is_redacted_with_context(
    monkeypatch,
    tmp_path,
) -> None:
    secret = "PASSWORD-IS-THE-WHOLE-ERROR"
    config_path = tmp_path / "redaction.toml"
    save_mail_config(config_path, _config())
    runtime = MailRuntime(
        _engine(tmp_path / "redaction.db"),
        config_path=config_path,
        environment={"MATTERHORN_MAIL_PASSWORD": secret},
    )
    report = MailSyncReport(
        scope_id="mail-scope",
        account="dana@example.test",
        folder="INBOX",
        container_id=_config().container_id,
        pulled=0,
        filtered=0,
        filtered_by_reason={},
        parse_errors=0,
        effective_window=None,
        cards_produced=0,
        new_assertions=0,
        new_matters=0,
        new_watermark=10,
        uidvalidity="200",
        previous_uidvalidity="100",
        reset_detected=True,
        backfill=False,
    )

    def fail_with_secret(_connector, *, scope_id, backfill=False):
        del scope_id, backfill
        raise MailboxResetError(report, message=secret)

    monkeypatch.setattr(MailConnector, "sync", fail_with_secret)

    with pytest.raises(MailboxResetError) as caught:
        runtime.sync(scope_id="mail-scope")

    expected = "IMAP sync failed: [REDACTED]"
    assert str(caught.value) == expected
    assert runtime.last_error == expected
    assert runtime.last_error != "[REDACTED]"
    assert runtime.last_report == report
    assert secret not in json.dumps(runtime.status())


def test_auth_failure_links_help_and_redacts_error_and_logs(
    caplog, tmp_path
) -> None:
    secret = "DO-NOT-LEAK-THIS"
    connector = _connector(
        _engine(tmp_path / "auth.db"),
        FakeIMAP("1", {}, auth_error=True),
        secret=secret,
    )

    with caplog.at_level(logging.WARNING), pytest.raises(MailAuthError) as caught:
        connector.sync(scope_id="mail-scope")

    combined = f"{caught.value}\n{caplog.text}"
    assert secret not in combined
    assert MAIL_PROVIDERS["gmail"].help_url in combined
    assert "authentication failed" in combined


def test_mail_config_toml_round_trip_never_contains_password(tmp_path) -> None:
    path = tmp_path / "matterhorn.toml"
    path.write_text('db = "fixture.db"\nscope = "root"\n', encoding="utf-8")
    runtime = MailRuntime(
        _engine(tmp_path / "config.db"),
        config_path=path,
        environment={},
    )

    runtime.configure(_config(), password="top-secret")

    text = path.read_text(encoding="utf-8")
    assert "top-secret" not in text
    assert "password" not in text.casefold()
    assert tomllib.loads(text)["mail"]["accounts"] == [
        {
            "provider": "gmail",
            "host": "imap.gmail.com",
            "port": 993,
            "ssl": True,
            "user": "dana@example.test",
            "folder": "INBOX",
            "interval": "off",
            "initial_window": 50,
            "scope": "mail-scope",
        }
    ]
    assert load_mail_config(path) == _config()


def test_mail_setup_cli_flags_write_no_secret_key(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli_app,
        [
            "mail",
            "setup",
            "--provider",
            "gmail",
            "--account",
            "fixture@example.test",
            "--folder",
            "INBOX",
            "--interval",
            "6h",
            "--scope",
            "fixture",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = tomllib.loads((tmp_path / "matterhorn.toml").read_text())
    assert payload["mail"]["accounts"] == [
        {
            "provider": "gmail",
            "host": "imap.gmail.com",
            "port": 993,
            "ssl": True,
            "user": "fixture@example.test",
            "folder": "INBOX",
            "interval": "6h",
            "initial_window": 50,
            "scope": "fixture",
        }
    ]
    assert "password" not in (tmp_path / "matterhorn.toml").read_text().casefold()


def test_multi_account_toml_round_trip_and_legacy_migration(tmp_path) -> None:
    path = tmp_path / "matterhorn.toml"
    path.write_text(
        """db = "fixture.db"
[mail]
provider = "gmail"
host = "imap.gmail.com"
port = 993
ssl = true
user = "dana@example.test"
folder = "INBOX"
interval = "off"
initial_window = 50
scope = "personal"
""",
        encoding="utf-8",
    )
    legacy = load_mail_configs(path)
    assert [item.account_id for item in legacy] == [
        "dana@example.test@imap.gmail.com/INBOX"
    ]
    work = MailConfig(
        provider="manual",
        host="imap.work.example",
        port=993,
        ssl=True,
        user="dana@work.example",
        folder="Matters",
        interval="1h",
        initial_window=25,
        scope="work",
        name="work-mail",
    )

    save_mail_configs(path, [*legacy, work])

    text = path.read_text(encoding="utf-8")
    payload = tomllib.loads(text)
    assert "[mail]" not in text
    assert text.count("[[mail.accounts]]") == 2
    assert [item["user"] for item in payload["mail"]["accounts"]] == [
        "dana@example.test",
        "dana@work.example",
    ]
    assert [item.account_id for item in load_mail_configs(path)] == [
        "dana@example.test@imap.gmail.com/INBOX",
        "work-mail",
    ]
    assert "password" not in text.casefold()


def test_mail_cli_appends_and_requires_selection_when_ambiguous(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    for name, account, scope in [
        ("personal", "dana@example.test", "personal"),
        ("work", "dana@work.example", "work"),
    ]:
        result = CliRunner().invoke(
            cli_app,
            [
                "mail",
                "setup",
                "--provider",
                "gmail",
                "--account",
                account,
                "--name",
                name,
                "--scope",
                scope,
            ],
        )
        assert result.exit_code == 0, result.output

    assert [item.account_id for item in load_mail_configs("matterhorn.toml")] == [
        "personal",
        "work",
    ]
    ambiguous = CliRunner().invoke(cli_app, ["mail", "sync"])
    assert ambiguous.exit_code == 2
    plain = _plain(ambiguous.output)
    assert "--account is required" in plain
    assert "personal" in plain
    assert "work" in plain


def test_registry_ticks_all_accounts_with_independent_passwords_and_positions(
    tmp_path,
) -> None:
    now = [datetime(2026, 7, 30, 8, tzinfo=UTC)]
    logins: dict[str, list[str]] = {"imap.one.example": [], "imap.two.example": []}

    class CredentialIMAP(FakeIMAP):
        def __init__(self, host: str, uid: int):
            super().__init__(str(uid + 100), {uid: _eml(uid)})
            self.host = host

        def login(self, user, password):
            del user
            logins[self.host].append(password)
            return "OK", [b"authenticated"]

    configs = [
        MailConfig(
            provider="manual",
            host="imap.one.example",
            port=993,
            ssl=True,
            user="dana@one.example",
            interval="15min",
            scope="personal",
            name="personal",
        ),
        MailConfig(
            provider="manual",
            host="imap.two.example",
            port=993,
            ssl=True,
            user="dana@two.example",
            interval="15min",
            scope="work",
            name="work",
        ),
    ]
    path = tmp_path / "matterhorn.toml"
    save_mail_configs(path, configs)

    def factory(host, _port):
        uid = 1 if host == "imap.one.example" else 7
        return CredentialIMAP(host, uid)

    engine = _engine(tmp_path / "registry.db")
    registry = MailRuntimeRegistry(
        engine,
        config_path=path,
        environment={},
        clock=lambda: now[0],
        imap_ssl_factory=factory,
    )
    registry.configure(configs[0], password="personal-secret")
    registry.configure(configs[1], password="work-secret")
    now[0] = datetime(2026, 7, 30, 8, 15, tzinfo=UTC)

    reports = registry.tick()

    assert [(item.scope_id, item.new_watermark) for item in reports] == [
        ("personal", 1),
        ("work", 7),
    ]
    assert logins == {
        "imap.one.example": ["personal-secret"],
        "imap.two.example": ["work-secret"],
    }
    assert engine.sync_positions("personal")[0].uid_watermark == 1
    assert engine.sync_positions("work")[0].uid_watermark == 7
    serialized = json.dumps(registry.accounts())
    assert "personal-secret" not in serialized
    assert "work-secret" not in serialized


def test_mail_reset_cli_requires_yes_and_deletes_only_sync_position(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "reset-cli.db"
    config_path = tmp_path / "matterhorn.toml"
    config_path.write_text(
        f'db = {json.dumps(str(db_path))}\nscope = "mail-scope"\n',
        encoding="utf-8",
    )
    save_mail_config(config_path, _config())
    engine = _engine(db_path)
    with engine.store.transaction():
        engine.store.update_mail_sync_position(
            "mail-scope",
            _config().container_id,
            uid_watermark=42,
            uidvalidity="900",
            fallback_watermark=datetime(1970, 1, 1, tzinfo=UTC),
        )

    refused = CliRunner().invoke(cli_app, ["mail", "reset"])
    assert refused.exit_code == 2
    refused_plain = _plain(refused.output)
    assert "--yes" in refused_plain
    assert "required" in refused_plain
    assert engine.sync_positions("mail-scope")[0].uid_watermark == 42

    accepted = CliRunner().invoke(cli_app, ["mail", "reset", "--yes"])
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.output) == {
        "scope_id": "mail-scope",
        "container_id": _config().container_id,
        "position_deleted": True,
        "next_sync": "initial_window",
    }
    assert engine.sync_positions("mail-scope") == []


def test_runtime_scheduler_and_environment_credential_state(tmp_path) -> None:
    now = [datetime(2026, 7, 30, 8, tzinfo=UTC)]
    config = replace(_config(), interval="15min")
    config_path = tmp_path / "scheduled.toml"
    save_mail_config(config_path, config)
    runtime = MailRuntime(
        _engine(tmp_path / "scheduled.db"),
        config_path=config_path,
        environment={"MATTERHORN_MAIL_PASSWORD": "environment-secret"},
        clock=lambda: now[0],
        imap_ssl_factory=lambda _host, _port: FakeIMAP("44", {2: _eml(2)}),
    )

    initial = runtime.status()
    assert initial["password_state"] == "loaded from environment"
    assert initial["next_run_at"] == "2026-07-30T08:15:00+00:00"
    assert "environment-secret" not in json.dumps(initial)
    now[0] = datetime(2026, 7, 30, 8, 14, tzinfo=UTC)
    assert runtime.tick() is None
    now[0] = datetime(2026, 7, 30, 8, 15, tzinfo=UTC)
    report = runtime.tick()
    assert report is not None
    assert report.pulled == 1
    assert runtime.status()["next_run_at"] == "2026-07-30T08:30:00+00:00"


def test_mail_reset_rest_requires_confirmation_and_console_has_button(
    tmp_path,
) -> None:
    async def scenario() -> None:
        fake = FakeIMAP("88", {1: _eml(1)})
        app = create_app(
            engine=_engine(tmp_path / "reset-rest.db"),
            mail_config_path=tmp_path / "reset-rest.toml",
            mail_imap_ssl_factory=lambda _host, _port: fake,
            console_enabled=True,
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            await client.post(
                "/v1/connectors/mail/config",
                json={
                    "provider": "gmail",
                    "account": "dana@example.test",
                    "scope": "mail-scope",
                    "password": "memory-only",
                },
            )
            synced = await client.post(
                "/v1/connectors/mail/sync",
                json={"scope_id": "mail-scope"},
            )
            assert synced.status_code == 200

            refused = await client.post(
                "/v1/connectors/mail/reset",
                json={"scope_id": "mail-scope"},
            )
            assert refused.status_code == 400
            assert "explicit confirmation" in refused.json()["error"]["message"]

            accepted = await client.post(
                "/v1/connectors/mail/reset",
                json={"scope_id": "mail-scope", "confirm": True},
            )
            assert accepted.status_code == 200
            assert accepted.json() == {
                "scope_id": "mail-scope",
                "container_id": _config().container_id,
                "position_deleted": True,
                "next_sync": "initial_window",
            }
            status = await client.get(
                "/v1/connectors/mail/status",
                params={"scope_id": "mail-scope"},
            )
            assert status.json()["uid_watermark"] is None
            assert status.json()["uidvalidity"] is None

            console = await client.get("/console")
            assert console.status_code == 200
            assert 'data-mail-action="reset"' in console.text
            assert "Re-pull recent" in console.text
            paths = (await client.get("/openapi.json")).json()["paths"]
            assert "/v1/connectors/mail/reset" in paths

    asyncio.run(scenario())


def test_status_endpoint_is_redacted_and_mail_rest_round_trip(tmp_path) -> None:
    async def scenario() -> None:
        secret = "console-only-secret"
        fake = FakeIMAP("77", {3: _eml(3)})
        config_path = tmp_path / "matterhorn.toml"
        app = create_app(
            engine=_engine(tmp_path / "rest-mail.db"),
            mail_config_path=config_path,
            mail_imap_ssl_factory=lambda _host, _port: fake,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            configured = await client.post(
                "/v1/connectors/mail/config",
                json={
                    "provider": "gmail",
                    "account": "dana@example.test",
                    "folder": "INBOX",
                    "interval": "15min",
                    "scope": "mail-scope",
                    "password": secret,
                },
            )
            assert configured.status_code == 200
            assert configured.json()["host"] == "imap.gmail.com"
            assert "password" not in configured.json()

            synced = await client.post(
                "/v1/connectors/mail/sync",
                json={"scope_id": "mail-scope"},
            )
            assert synced.status_code == 200
            assert synced.json()["pulled"] == 1
            status = await client.get(
                "/v1/connectors/mail/status",
                params={"scope_id": "mail-scope"},
            )
            assert status.status_code == 200
            payload = status.json()
            assert payload["password_state"] == "loaded in process memory"
            assert payload["uid_watermark"] == 3
            assert payload["last_report"]["new_matters"] == 1
            assert secret not in json.dumps(payload)
            assert not _contains_key(payload, "password")

            invalid = await client.post(
                "/v1/connectors/mail/config",
                json={
                    "provider": "gmail",
                    "port": 0,
                    "account": "dana@example.test",
                    "password": secret,
                },
            )
            assert invalid.status_code == 422
            assert secret not in invalid.text

            paths = (await client.get("/openapi.json")).json()["paths"]
            for path in [
                "/v1/connectors/mail/config",
                "/v1/connectors/mail/status",
                "/v1/connectors/mail/sync",
                "/v1/connectors/mail/reset",
                "/v1/scopes/{scope_id}/upload",
                "/v1/scopes/{scope_id}/quick-message",
            ]:
                assert path in paths

        config_text = config_path.read_text(encoding="utf-8")
        assert secret not in config_text
        assert "password" not in config_text.casefold()
        assert secret.encode() not in (tmp_path / "rest-mail.db").read_bytes()

    asyncio.run(scenario())


def test_mail_account_collection_rest_round_trip_and_delete_retains_watermark(
    tmp_path,
) -> None:
    async def scenario() -> None:
        secrets = {
            "imap.personal.example": "personal-secret",
            "imap.work.example": "work-secret",
        }
        observed: dict[str, str] = {}

        class AccountIMAP(FakeIMAP):
            def __init__(self, host: str, uid: int):
                super().__init__(f"9{uid}", {uid: _eml(uid)})
                self.host = host

            def login(self, user, password):
                del user
                observed[self.host] = password
                assert password == secrets[self.host]
                return "OK", [b"authenticated"]

        def factory(host, _port):
            return AccountIMAP(
                host,
                3 if host == "imap.personal.example" else 8,
            )

        engine = _engine(tmp_path / "collection.db")
        config_path = tmp_path / "matterhorn.toml"
        app = create_app(
            engine=engine,
            mail_config_path=config_path,
            mail_imap_ssl_factory=factory,
            console_enabled=True,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            for account_id, host, user, scope, password in [
                (
                    "personal",
                    "imap.personal.example",
                    "dana@personal.example",
                    "personal",
                    secrets["imap.personal.example"],
                ),
                (
                    "work",
                    "imap.work.example",
                    "dana@work.example",
                    "work",
                    secrets["imap.work.example"],
                ),
            ]:
                response = await client.post(
                    "/v1/connectors/mail/accounts",
                    json={
                        "account_id": account_id,
                        "provider": "manual",
                        "host": host,
                        "port": 993,
                        "ssl": True,
                        "account": user,
                        "folder": "INBOX",
                        "scope": scope,
                        "password": password,
                    },
                )
                assert response.status_code == 200
                assert response.json()["account_id"] == account_id
                assert "password" not in response.text.casefold()

            accounts = await client.get("/v1/connectors/mail/accounts")
            assert [item["config"]["account_id"] for item in accounts.json()] == [
                "personal",
                "work",
            ]
            assert "personal-secret" not in accounts.text
            assert "work-secret" not in accounts.text

            personal = await client.post(
                "/v1/connectors/mail/accounts/personal/sync",
                json={},
            )
            work = await client.post(
                "/v1/connectors/mail/accounts/work/sync",
                json={},
            )
            assert personal.json()["new_watermark"] == 3
            assert work.json()["new_watermark"] == 8
            assert observed == secrets

            removed = await client.delete(
                "/v1/connectors/mail/accounts/personal"
            )
            assert removed.json()["watermark_retained"] is True
            assert engine.sync_positions("personal")[0].uid_watermark == 3
            remaining = await client.get("/v1/connectors/mail/accounts")
            assert [item["config"]["account_id"] for item in remaining.json()] == [
                "work"
            ]

            paths = (await client.get("/openapi.json")).json()["paths"]
            for path in [
                "/v1/connectors/mail/accounts",
                "/v1/connectors/mail/accounts/{account_id}/sync",
                "/v1/connectors/mail/accounts/{account_id}/reset",
                "/v1/connectors/mail/accounts/{account_id}",
            ]:
                assert path in paths

            console = await client.get("/console")
            assert 'id="mail-account-list"' in console.text
            assert 'api("/v1/connectors/mail/accounts")' in console.text

        text = config_path.read_text(encoding="utf-8")
        assert "personal-secret" not in text
        assert "work-secret" not in text
        assert "password" not in text.casefold()

    asyncio.run(scenario())


def test_derived_mail_account_id_with_folder_slash_is_routable(tmp_path) -> None:
    async def scenario() -> None:
        app = create_app(
            engine=_engine(tmp_path / "derived-id.db"),
            mail_config_path=tmp_path / "matterhorn.toml",
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            configured = await client.post(
                "/v1/connectors/mail/accounts",
                json={
                    "provider": "manual",
                    "host": "imap.personal.example",
                    "port": 993,
                    "ssl": True,
                    "account": "dana@example.test",
                    "folder": "Archive/Matters",
                    "scope": "personal",
                },
            )
            account_id = configured.json()["account_id"]
            assert account_id == (
                "dana@example.test@imap.personal.example/Archive/Matters"
            )
            removed = await client.delete(
                f"/v1/connectors/mail/accounts/{quote(account_id, safe='')}"
            )
            assert removed.status_code == 200
            assert removed.json()["account_id"] == account_id

    asyncio.run(scenario())


def test_mail_sync_lock_releases_after_endpoint_error(tmp_path) -> None:
    class FailingFetchIMAP(FakeIMAP):
        def uid(self, command, *args):
            if command.casefold() == "fetch":
                self.fetches.append(str(args[0]))
                raise imaplib.IMAP4.abort("connection dropped")
            return super().uid(command, *args)

    async def scenario() -> None:
        attempts = [
            FailingFetchIMAP("78", {1: _eml(1)}),
            FakeIMAP("78", {1: _eml(1)}),
        ]
        factory_calls = 0

        def imap_factory(_host, _port):
            nonlocal factory_calls
            client = attempts[factory_calls]
            factory_calls += 1
            return client

        app = create_app(
            engine=_engine(tmp_path / "lock-release.db"),
            mail_config_path=tmp_path / "lock-release.toml",
            mail_imap_ssl_factory=imap_factory,
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            configured = await client.post(
                "/v1/connectors/mail/config",
                json={
                    "provider": "gmail",
                    "account": "dana@example.test",
                    "scope": "mail-scope",
                    "password": "memory-only",
                },
            )
            assert configured.status_code == 200

            first = await asyncio.wait_for(
                client.post("/v1/connectors/mail/sync", json={}),
                timeout=2,
            )
            second = await asyncio.wait_for(
                client.post("/v1/connectors/mail/sync", json={}),
                timeout=2,
            )

            assert first.status_code == 400
            assert "connection dropped" in first.json()["error"]["message"]
            assert second.status_code == 200
            assert second.json()["pulled"] == 1
            assert factory_calls == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        (
            "message.json",
            json.dumps(
                {
                    "messages": [
                        {
                            "id": "json-1",
                            "sender": {"id": "dana", "name": "Dana Reyes"},
                            "text": "JSON matter is open.",
                            "sent_at": "2026-07-30T09:00:00Z",
                        }
                    ]
                }
            ).encode(),
        ),
        (
            "message.yaml",
            (
                b"messages:\n"
                b"  - id: yaml-1\n"
                b"    sender: {id: dana, name: Dana Reyes}\n"
                b"    text: YAML matter is open.\n"
                b"    sent_at: 2026-07-30T09:01:00Z\n"
            ),
        ),
        ("message.eml", _eml(20)),
        (
            "mailbox.mbox",
            b"From dana@example.test Thu Jul 30 09:02:00 2026\n"
            + _eml(21)
            + b"\n",
        ),
    ],
)
def test_upload_dispatches_all_supported_formats(
    filename, content, tmp_path
) -> None:
    async def scenario() -> None:
        app = create_app(
            engine=_engine(tmp_path / f"{filename}.db"),
            mail_config_path=tmp_path / f"{filename}.toml",
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            response = await client.post(
                "/v1/scopes/upload-scope/upload",
                files={
                    "file": (
                        filename,
                        content,
                        (
                            "message/rfc822"
                            if filename.endswith(".eml")
                            else "application/octet-stream"
                        ),
                    )
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "completed"
            matters = await client.get("/v1/scopes/upload-scope/matters")
            assert matters.status_code == 200
            assert len(matters.json()) == 1

    asyncio.run(scenario())


def test_upload_garbage_and_quick_message_server_timestamp(tmp_path) -> None:
    async def scenario() -> None:
        engine = _engine(tmp_path / "writes.db")
        app = create_app(
            engine=engine,
            mail_config_path=tmp_path / "writes.toml",
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            garbage = await client.post(
                "/v1/scopes/write-scope/upload",
                files={"file": ("garbage.txt", b"not supported", "text/plain")},
            )
            assert garbage.status_code == 400
            assert ".mbox" in garbage.json()["error"]["message"]

            quick = await client.post(
                "/v1/scopes/write-scope/quick-message",
                json={"sender": "Dana Reyes", "text": "Quick matter is open."},
            )
            assert quick.status_code == 200, quick.text
            assert quick.json()["status"] == "completed"
            observation = engine.store.record_observations("write-scope")[0]
            assert observation.observed_at == "2026-07-30T08:00:00.000000Z"
            matters = await client.get("/v1/scopes/write-scope/matters")
            assert len(matters.json()) == 1

    asyncio.run(scenario())


def test_sync_and_upload_use_existing_write_rate_limit(tmp_path) -> None:
    async def scenario() -> None:
        app = create_app(
            engine=_engine(tmp_path / "rate.db"),
            mail_config_path=tmp_path / "rate.toml",
            mail_imap_ssl_factory=lambda _host, _port: FakeIMAP(
                "1",
                {1: _eml(1)},
            ),
            ingest_rate_limit=1,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            await client.post(
                "/v1/connectors/mail/config",
                json={
                    "provider": "gmail",
                    "account": "dana@example.test",
                    "scope": "rate-scope",
                    "password": "memory-only",
                },
            )
            first_sync = await client.post(
                "/v1/connectors/mail/sync",
                json={},
            )
            second_sync = await client.post(
                "/v1/connectors/mail/sync",
                json={},
            )
            assert first_sync.status_code == 200
            assert second_sync.status_code == 429

            files = {
                "file": (
                    "one.json",
                    json.dumps(
                        {
                            "messages": [
                                {
                                    "id": "rate-upload",
                                    "sender": {"id": "dana"},
                                    "text": "Rate upload is open.",
                                    "sent_at": "2026-07-30T09:00:00Z",
                                }
                            ]
                        }
                    ).encode(),
                    "application/json",
                )
            }
            first_upload = await client.post(
                "/v1/scopes/upload-rate/upload",
                files=files,
            )
            second_upload = await client.post(
                "/v1/scopes/upload-rate/upload",
                files=files,
            )
            assert first_upload.status_code == 200
            assert second_upload.status_code == 429

    asyncio.run(scenario())


def _contains_key(value: Any, target: str) -> bool:
    if isinstance(value, dict):
        return target in value or any(
            _contains_key(item, target) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_key(item, target) for item in value)
    return False
