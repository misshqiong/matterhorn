import json
import subprocess
import sys
from importlib import import_module

import yaml


def _command(*args: str) -> list[str]:
    return [sys.executable, "-m", "matterhorn.cli", *args]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command(*args),
        check=True,
        capture_output=True,
        text=True,
    )


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
    assert "Usage:" in _run("--help").stdout
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
    help_text = _run("dream", "--help").stdout
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
    assert "SUMMARY passed=37 failed=0 total=37" in completed.stdout


def test_conformance_cli_documents_backend_selection() -> None:
    help_text = _run("conformance", "run", "--help").stdout
    assert "--backend" in help_text
    assert "--dsn" in help_text
    assert "MATTERHORN_TEST_POSTGRES_DSN" in help_text
    assert "0 when all cases pass" in help_text
    assert "1 when any valid case fails" in help_text
    assert "2 when" in help_text


def test_conformance_cli_invalid_suite_exits_two(tmp_path) -> None:
    completed = subprocess.run(
        _command("conformance", "run", "--suite", str(tmp_path / "missing")),
        check=False,
        capture_output=True,
        text=True,
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
        text=True,
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
        text=True,
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
