import json
import os
import subprocess
import sys
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any

import yaml
from typer.core import TyperGroup
from typer.main import get_command

from matterhorn.cli.app import app
from matterhorn.contracts import Record
from matterhorn.store import SQLiteStore


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
    assert replayed["events_emitted"] == 0


def test_events_export_import_cli_round_trip(tmp_path) -> None:
    source_db = tmp_path / "source.db"
    target_db = tmp_path / "target.db"
    export_file = tmp_path / "scope.json"
    markdown_file = tmp_path / "MATTERS.md"
    html_file = tmp_path / "matters.html"
    card_file = tmp_path / "card.yaml"
    card_file.write_text(
        yaml.safe_dump(
            {
                "card_id": "cli-output-1",
                "scope_id": "team",
                "subject_key": "release",
                "date": "2026-07-29",
                "title": "Release",
                "status": "done",
                "source_refs": [
                    {
                        "source_id": "m1",
                        "sent_at": "2026-07-29T08:00:00Z",
                        "sender": "u1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _run("ingest", str(card_file), "--db", str(source_db))
    events = json.loads(
        _run("events", "team", "--db", str(source_db)).stdout
    )
    assert {item["event_type"] for item in events} == {
        "matter_created",
        "status_changed",
        "matter_completed",
    }
    exported = _run(
        "export",
        "team",
        "--out",
        str(export_file),
        "--db",
        str(source_db),
    )
    assert "Exported team" in exported.stdout
    rendered = _run(
        "export",
        "team",
        "--format",
        "markdown",
        "--out",
        str(markdown_file),
        "--db",
        str(source_db),
    )
    assert "Exported team as markdown" in rendered.stdout
    markdown = markdown_file.read_text(encoding="utf-8")
    assert "# Development ledger" in markdown
    assert "## Release" in markdown
    assert "generated from assertions; reproduce:" in markdown
    html_rendered = _run(
        "export",
        "team",
        "--format",
        "html",
        "--as-of",
        "2026-07-30T00:00:00Z",
        "--out",
        str(html_file),
        "--db",
        str(source_db),
    )
    assert "Exported team as html" in html_rendered.stdout
    html = html_file.read_text(encoding="utf-8")
    assert "<style>" in html
    assert "<script>" in html
    assert "Deterministically rendered from 1 assertions" in html
    imported = json.loads(
        _run("import", str(export_file), "--db", str(target_db)).stdout
    )
    assert imported["assertions"] == 1
    source_matters = json.loads(
        _run("matters", "team", "--db", str(source_db)).stdout
    )
    target_matters = json.loads(
        _run("matters", "team", "--db", str(target_db)).stdout
    )
    assert target_matters == source_matters
    replayed = json.loads(
        _run("replay", "team", "--db", str(target_db)).stdout
    )
    assert replayed["events_emitted"] == 0

    unavailable_file = tmp_path / "unavailable-profile.json"
    unavailable = json.loads(export_file.read_text(encoding="utf-8"))
    unavailable["schema_profile"]["id"] = "missing-profile/v999"
    unavailable_file.write_text(json.dumps(unavailable), encoding="utf-8")
    refused = subprocess.run(
        _command(
            "import",
            str(unavailable_file),
            "--db",
            str(tmp_path / "refused.db"),
        ),
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert refused.returncode == 2
    for fragment in [
        "schema",
        "profile",
        "available",
        "locally",
        "missing-profile/v999",
    ]:
        assert fragment in refused.stderr
    assert "Traceback" not in refused.stderr


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


def test_merge_and_unmerge_cli_use_human_provenance(tmp_path) -> None:
    db = tmp_path / "merge-cli.db"
    cards = tmp_path / "merge-cards.yaml"
    cards.write_text(
        yaml.safe_dump(
            {
                "cards": [
                    {
                        "card_id": key,
                        "scope_id": "demo",
                        "subject_key": key,
                        "date": "2026-07-29",
                        "title": title,
                        "status": "open",
                        "source_refs": [
                            {
                                "source_id": f"model-{key}",
                                "sent_at": "2026-07-29T09:00:00Z",
                                "sender": "bot",
                            }
                        ],
                    }
                    for key, title in [
                        ("source", "Release QA"),
                        ("target", "Release"),
                    ]
                ]
            }
        ),
        encoding="utf-8",
    )
    _run("ingest", str(cards), "--db", str(db))
    merged = json.loads(
        _run(
            "merge",
            "demo",
            "source",
            "target",
            "--reason",
            "same real-world release",
            "--sender",
            "Ada",
            "--db",
            str(db),
        ).stdout
    )
    matters = json.loads(_run("matters", "demo", "--db", str(db)).stdout)
    assert merged["event_type"] == "subject_merged"
    assert merged["source_ids"][0].startswith("console:")
    assert matters[0]["aliases"] == ["Release QA"]

    unmerged = json.loads(
        _run(
            "unmerge",
            "demo",
            "source",
            "--reason",
            "separate work after all",
            "--sender",
            "Ada",
            "--db",
            str(db),
        ).stdout
    )
    restored = json.loads(_run("matters", "demo", "--db", str(db)).stdout)
    assert unmerged["event_type"] == "subject_unmerged"
    assert {item["subject_key"] for item in restored} == {"source", "target"}


def test_handles_backfill_cli_is_idempotent(tmp_path) -> None:
    db = tmp_path / "handles-backfill.db"
    cards = tmp_path / "handles-card.yaml"
    cards.write_text(
        yaml.safe_dump(
            {
                "card_id": "handle-card",
                "scope_id": "demo",
                "subject_key": "fictional-invoice",
                "date": "2026-08-04",
                "title": "Invoice INV-2026-009",
                "status": "open",
                "source_refs": [
                    {
                        "source_id": "fictional-invoice-source",
                        "sent_at": "2026-08-04T09:00:00Z",
                        "sender": "Dana Reyes",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _run("ingest", str(cards), "--db", str(db))

    first = _run("handles", "backfill", "demo", "--db", str(db)).stdout
    second = _run("handles", "backfill", "demo", "--db", str(db)).stdout

    assert "bound                 1" in first
    assert "bound                 0" in second
    assert "already-bound         1" in second


def test_staging_purge_cli_prints_deleted_count(tmp_path) -> None:
    db = tmp_path / "staging.db"
    store = SQLiteStore(db)
    sent_at = datetime.fromisoformat("2000-01-01T09:00:00+00:00")
    with store.transaction():
        store.stage_records(
            "fictional-team",
            [
                Record(
                    record_id="room:old",
                    native_id="old",
                    container_id="room",
                    sent_at=sent_at,
                    author={"id": "ada", "kind": "human"},
                    content="Fictional old staging row.",
                )
            ],
            staged_at=sent_at,
        )
    store.close()

    result = json.loads(
        _run("staging", "purge", "fictional-team", "--db", str(db)).stdout
    )

    assert result == {"scope_id": "fictional-team", "deleted": 1}


def test_cli_rejects_invalid_staging_retention_environment(tmp_path) -> None:
    environment = dict(os.environ)
    environment["MATTERHORN_STAGING_RETENTION_DAYS"] = "not-a-number"
    completed = subprocess.run(
        _command(
            "staging",
            "purge",
            "fictional-team",
            "--db",
            str(tmp_path / "x.db"),
        ),
        check=False,
        capture_output=True,
        encoding="utf-8",
        env=environment,
    )

    assert completed.returncode == 2
    assert "MATTERHORN_STAGING_RETENTION_DAYS" in completed.stderr


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
        "MATTERHORN_TIMEOUT",
    ]:
        assert name in help_text


def test_conformance_cli_runs_packaged_golden_suite() -> None:
    completed = _run("conformance", "run")
    assert "PASS basic-current" in completed.stdout
    assert "SUMMARY passed=67 failed=0 total=67" in completed.stdout


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


def test_eval_cli_contract_documents_measurement_flags() -> None:
    command = _cli_metadata("eval", "run")

    assert "--dataset" in _parameter(command, "dataset").opts
    assert "--case" in _parameter(command, "case_id").opts
    assert "--provider" in _parameter(command, "provider").opts
    assert "--responses" in _parameter(command, "responses").opts
    assert "--json" in _parameter(command, "json_path").opts
    assert "--seed-note" in _parameter(command, "seed_note").opts
    assert "metric values never determine exit status" in " ".join(
        (command.help or "").split()
    )


def test_eval_cli_runs_fixture_case_and_writes_parseable_json(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("MATTERHORN_PROVIDER", raising=False)
    report_path = tmp_path / "report.json"

    completed = _run(
        "eval",
        "run",
        "--case",
        "simple-single-matter",
        "--json",
        str(report_path),
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert "case_id | matters_expected | matters_produced" in completed.stdout
    assert "simple-single-matter | 1 | 1 | 1 | 0 | 0/1 (0.000)" in completed.stdout
    assert report["provider"] == "fixture-file"
    assert report["cases"][0]["metrics"]["over_split"]["count"] == 0
    assert report["aggregate"]["metrics"]["zero_model_route_rate"] is None


def test_eval_cli_metric_failures_still_exit_zero(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MATTERHORN_PROVIDER", raising=False)
    completed = subprocess.run(
        _command(
            "eval",
            "run",
            "--case",
            "interleaved-three-matters",
            "--json",
            str(tmp_path / "report.json"),
        ),
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0
    assert (
        "interleaved-three-matters | 3 | 1 | 1 | 1 | "
        "0/3 (0.000) | 1/1 (1.000)"
    ) in completed.stdout


def test_eval_cli_unusable_dataset_exits_two(tmp_path) -> None:
    completed = subprocess.run(
        _command("eval", "run", "--dataset", str(tmp_path / "missing")),
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert completed.returncode == 2
    assert "ERROR eval dataset directory not found" in completed.stderr
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


def test_init_config_and_five_minute_commands_work_offline(tmp_path) -> None:
    init = subprocess.run(
        _command("init"),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert init.returncode == 0, init.stderr
    assert (tmp_path / "matterhorn.toml").is_file()
    assert (tmp_path / "matterhorn.db").is_file()
    assert (tmp_path / "demo-messages.yaml").is_file()

    added = subprocess.run(
        _command("add", "demo-messages.yaml"),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert added.returncode == 0, added.stderr
    receipt = json.loads(added.stdout)
    assert receipt["accepted"] == 1

    flushed = subprocess.run(
        _command("flush", "demo"),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert flushed.returncode == 0, flushed.stderr
    assert json.loads(flushed.stdout)["tasks_processed"] == 1

    listed = subprocess.run(
        _command("matters", "demo"),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert listed.returncode == 0, listed.stderr
    matters = json.loads(listed.stdout)
    assert matters[0]["title"] == "Payment refactor"
    assert matters[0]["owners"] == ["u1"]

    task = subprocess.run(
        _command("task", receipt["task_id"]),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert task.returncode == 0, task.stderr
    assert json.loads(task.stdout)["gate"] == {
        "accepted": 1,
        "rejected": {},
    }

    repeated = subprocess.run(
        _command("init"),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert repeated.returncode == 0, repeated.stderr
