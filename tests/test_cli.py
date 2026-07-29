import json
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

import yaml
from typer.core import TyperGroup
from typer.main import get_command

from matterhorn.cli.app import app


def _command(*args: str) -> list[str]:
    return [sys.executable, "-m", "matterhorn.cli", *args]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command(*args),
        check=True,
        capture_output=True,
        encoding="utf-8",
    )


def _cli_metadata(*path: str) -> Any:
    command = get_command(app)
    for name in path:
        assert isinstance(command, TyperGroup)
        command = command.commands[name]
    return command


def _parameter(command: Any, name: str) -> Any:
    return next(parameter for parameter in command.params if parameter.name == name)


def test_cli_smoke_end_to_end(tmp_path) -> None:
    db = tmp_path / "cli.db"
    card_file = tmp_path / "card.yaml"
    card_file.write_text(
        yaml.safe_dump(
            {
                "card_id": "cli-1",
                "scope_id": "demo",
                "subject_key": "launch",
                "date": "2026-01-01",
                "title": "Launch",
                "status": "open",
                "source_refs": [
                    {
                        "source_id": "m1",
                        "sent_at": "2026-01-01T08:00:00Z",
                        "sender": "u1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert _run("--help").returncode == 0
    ingested = json.loads(_run("ingest", str(card_file), "--db", str(db)).stdout)
    assert ingested["assertions_emitted"] == 1
    current = json.loads(
        _run(
            "query",
            "current",
            "demo",
            "launch",
            "status",
            "--db",
            str(db),
        ).stdout
    )
    assert current[0]["value"] == "open"
    historical = json.loads(
        _run(
            "query",
            "at",
            "demo",
            "launch",
            "status",
            "2026-01-01T00:00:00Z",
            "--db",
            str(db),
        ).stdout
    )
    assert historical[0]["value"] == "open"
    replayed = json.loads(_run("replay", "demo", "--db", str(db)).stdout)
    assert replayed["status"] == "rebuilt"


def test_extract_cli_wires_records_to_cards_ingest_and_sync_status(
    monkeypatch, tmp_path, capsys
) -> None:
    cli_app = import_module("matterhorn.cli.app")

    class Gateway:
        def complete(self, **_kwargs):
            return json.dumps(
                {
                    "cards": [
                        {
                            "date": "2026-07-29",
                            "title": "Release",
                            "status": "open",
                            "source_ids": ["C1:1.000001"],
                        }
                    ]
                }
            )

    monkeypatch.setattr(cli_app, "_write_gateway", lambda *_args: Gateway())
    db = tmp_path / "extract.db"
    input_file = tmp_path / "records.json"
    input_file.write_text(
        json.dumps(
            {
                "scope_id": "team",
                "records": [
                    {
                        "record_id": "C1:1.000001",
                        "native_id": "1.000001",
                        "container_id": "C1",
                        "sent_at": "2026-07-29T09:00:00Z",
                        "author": {"id": "U1", "kind": "human"},
                        "content": "Release is open.",
                        "uri": "https://example.slack.com/archives/C1/p1000001",
                        "kind": "message",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    cli_app.extract(
        input_file=input_file,
        scope_id=None,
        adapter="records",
        container_id=None,
        workspace_domain=None,
        cursor=["C1=next-page"],
        backfill=False,
        db=str(db),
        schema="org-matters/v1",
        schema_dir=None,
        provider=None,
        base_url=None,
        api_key=None,
        model=None,
    )
    report = json.loads(capsys.readouterr().out)
    assert report["records_processed"] == 1
    assert report["cards_accepted"] == 1
    assert report["assertions_emitted"] == 1
    assert report["sync_positions"][0]["cursor"] == "next-page"

    cli_app.sync_status(
        "team",
        db=str(db),
        schema="org-matters/v1",
        schema_dir=None,
    )
    positions = json.loads(capsys.readouterr().out)
    assert positions[0]["container_id"] == "C1"


def test_extract_cli_wires_reme_and_openviking_adapters(tmp_path, capsys) -> None:
    cli_app = import_module("matterhorn.cli.app")
    fixture_root = Path(__file__).parent / "fixtures"
    for adapter, fixture in [
        ("reme", fixture_root / "reme" / "daily-release.json"),
        (
            "openviking",
            fixture_root / "openviking" / "release-overview.json",
        ),
    ]:
        cli_app.extract(
            input_file=fixture,
            scope_id=None,
            adapter=adapter,
            container_id=None,
            workspace_domain=None,
            cursor=None,
            backfill=False,
            db=str(tmp_path / f"{adapter}.db"),
            schema="org-matters/v1",
            schema_dir=None,
            provider=None,
            base_url=None,
            api_key=None,
            model=None,
        )
        report = json.loads(capsys.readouterr().out)
        assert report["adapter"] == adapter
        assert report["scope_id"] == "team-a"
        assert report["cards_accepted"] == 1
        assert report["cards_dropped"] == 0
        assert report["assertions_emitted"] >= 1


def test_correct_cli_direct_flags_change_query_answer(tmp_path) -> None:
    db = tmp_path / "direct-correction.db"
    card_file = tmp_path / "direct-card.yaml"
    card_file.write_text(
        yaml.safe_dump(
            {
                "card_id": "direct-correction-card",
                "scope_id": "demo",
                "subject_key": "release",
                "date": "2026-07-29",
                "occurred_at": "2026-07-29T09:00:00Z",
                "title": "Release",
                "status": "blocked",
                "source_refs": [
                    {
                        "source_id": "model-1",
                        "sent_at": "2026-07-29T09:00:00Z",
                        "sender": "bot",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _run("ingest", str(card_file), "--db", str(db))
    before = json.loads(
        _run(
            "query", "current", "demo", "release", "status", "--db", str(db)
        ).stdout
    )
    corrected = json.loads(
        _run(
            "correct",
            "--scope-id",
            "demo",
            "--subject-key",
            "release",
            "--subject-type",
            "MATTER",
            "--predicate",
            "status",
            "--object-value",
            "open",
            "--valid-from",
            "2026-07-29T09:00:00Z",
            "--source-ref",
            json.dumps(
                {
                    "source_id": "human-1",
                    "sent_at": "2026-07-29T09:05:00Z",
                    "sender": "ada",
                }
            ),
            "--db",
            str(db),
        ).stdout
    )
    after = json.loads(
        _run(
            "query", "current", "demo", "release", "status", "--db", str(db)
        ).stdout
    )
    assert before[0]["value"] == "blocked"
    assert corrected["origin"] == "human"
    assert corrected["source_refs"][0]["source_id"] == "human-1"
    assert after[0]["value"] == "open"
    assert after[0]["origin"] == "human"


def test_correct_cli_accepts_yaml_file(tmp_path) -> None:
    db = tmp_path / "file-correction.db"
    card_file = tmp_path / "file-card.yaml"
    correction_file = tmp_path / "correction.yaml"
    card_file.write_text(
        yaml.safe_dump(
            {
                "card_id": "file-correction-card",
                "scope_id": "demo",
                "subject_key": "release",
                "date": "2026-07-29",
                "title": "Release",
                "status": "blocked",
                "source_refs": [
                    {
                        "source_id": "model-1",
                        "sent_at": "2026-07-29T09:00:00Z",
                        "sender": "bot",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    correction_file.write_text(
        yaml.safe_dump(
            {
                "correction": {
                    "scope_id": "demo",
                    "subject_key": "release",
                    "subject_type": "MATTER",
                    "predicate": "status",
                    "object_value": "closed",
                    "valid_from": "2026-07-30T00:00:00Z",
                    "source_refs": [
                        {
                            "source_id": "human-file-1",
                            "sent_at": "2026-07-30T00:05:00Z",
                            "sender": "ada",
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    _run("ingest", str(card_file), "--db", str(db))
    corrected = json.loads(
        _run("correct", str(correction_file), "--db", str(db)).stdout
    )
    after = json.loads(
        _run(
            "query", "current", "demo", "release", "status", "--db", str(db)
        ).stdout
    )
    assert corrected["origin"] == "human"
    assert after[0]["value"] == "closed"
    assert after[0]["source_ids"] == ["human-file-1"]


def test_dream_help_documents_environment_credentials() -> None:
    command = _cli_metadata("dream")
    help_text = " ".join(
        parameter.help or "" for parameter in command.params
    )
    for name in [
        "MATTERHORN_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MATTERHORN_BASE_URL",
    ]:
        assert name in help_text


def test_conformance_cli_runs_packaged_golden_suite() -> None:
    completed = _run("conformance", "run")
    assert "PASS basic-current" in completed.stdout
    assert "SUMMARY passed=40 failed=0 total=40" in completed.stdout


def test_conformance_cli_documents_backend_selection() -> None:
    command = _cli_metadata("conformance", "run")
    backend = _parameter(command, "backend")
    dsn = _parameter(command, "dsn")
    exit_status_help = " ".join((command.help or "").split())

    assert "--backend" in backend.opts
    assert backend.default == "sqlite"
    assert "--dsn" in dsn.opts
    assert "MATTERHORN_TEST_POSTGRES_DSN" in (dsn.help or "")
    assert "0 when all cases pass" in exit_status_help
    assert "1 when any valid case fails" in exit_status_help
    assert "2 when" in exit_status_help


def test_conformance_cli_invalid_suite_exits_two(tmp_path) -> None:
    completed = subprocess.run(
        _command("conformance", "run", "--suite", str(tmp_path / "missing")),
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert completed.returncode == 2
    assert "ERROR conformance suite directory not found" in completed.stderr


def test_conformance_cli_case_failure_exits_one(tmp_path) -> None:
    from matterhorn.conformance import default_suite

    case_path = default_suite() / "01-basic-current.yaml"
    case = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    case["case_id"] = "seeded-failure"
    case["title"] = "Seeded expectation failure"
    case["expect"]["queries"][0]["result"][0]["value"] = "closed"
    (tmp_path / "broken.yaml").write_text(
        yaml.safe_dump(case),
        encoding="utf-8",
    )
    completed = subprocess.run(
        _command("conformance", "run", "--suite", str(tmp_path)),
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert completed.returncode == 1
    assert "FAIL seeded-failure" in completed.stdout
    assert "SUMMARY passed=0 failed=1 total=1" in completed.stdout


def test_conformance_cli_malformed_case_exits_two(tmp_path) -> None:
    (tmp_path / "malformed.yaml").write_text(
        "case_id: [unterminated",
        encoding="utf-8",
    )
    completed = subprocess.run(
        _command("conformance", "run", "--suite", str(tmp_path)),
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert completed.returncode == 2
    assert "ERROR malformed conformance case" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_dream_environment_defaults_and_explicit_overrides(monkeypatch, tmp_path) -> None:
    cli_app = import_module("matterhorn.cli.app")
    captured = []

    class Report:
        def model_dump(self, **_kwargs):
            return {"status": "ok"}

    class FakeEngine:
        def __init__(self, gateway):
            self.gateway = gateway

        def dream(self, _scope_id, limit=None):
            captured.append((self.gateway, limit))
            return Report()

    def fake_engine(_db, _schema, _schema_dir, *, gateway=None):
        return FakeEngine(gateway)

    monkeypatch.setattr(cli_app, "_engine", fake_engine)
    monkeypatch.setenv("MATTERHORN_API_KEY", "matterhorn-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("MATTERHORN_BASE_URL", "https://env.example/v1")

    cli_app.dream(
        "s",
        limit=None,
        db=tmp_path / "env.db",
        schema="org-matters/v1",
        schema_dir=None,
        provider="openai-compatible",
        base_url=None,
        api_key=None,
        model="model-a",
    )
    gateway, _ = captured[-1]
    assert gateway.api_key == "matterhorn-secret"
    assert gateway.base_url == "https://env.example/v1"

    cli_app.dream(
        "s",
        limit=None,
        db=tmp_path / "override.db",
        schema="org-matters/v1",
        schema_dir=None,
        provider="openai-compatible",
        base_url="https://override.example/v1",
        api_key="override-secret",
        model="model-b",
    )
    gateway, _ = captured[-1]
    assert gateway.api_key == "override-secret"
    assert gateway.base_url == "https://override.example/v1"

    monkeypatch.delenv("MATTERHORN_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    cli_app.dream(
        "s",
        limit=None,
        db=tmp_path / "fallback.db",
        schema="org-matters/v1",
        schema_dir=None,
        provider="openai-compatible",
        base_url=None,
        api_key=None,
        model="model-c",
    )
    gateway, _ = captured[-1]
    assert gateway.api_key == "provider-secret"

    cli_app.dream(
        "s",
        limit=None,
        db=tmp_path / "anthropic.db",
        schema="org-matters/v1",
        schema_dir=None,
        provider="anthropic",
        base_url=None,
        api_key=None,
        model="claude-test",
    )
    gateway, _ = captured[-1]
    assert gateway.api_key == "anthropic-secret"
    assert gateway.base_url == "https://env.example/v1"
