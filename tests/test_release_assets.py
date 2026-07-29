from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_is_slim_and_non_root() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "python:3.11-slim" in text
    assert "USER matterhorn" in text
    assert 'CMD ["mh", "serve"' in text


def test_compose_assets_are_valid_and_wire_postgres() -> None:
    for path in [
        ROOT / "compose.postgres.yml",
        ROOT / "examples/service/compose.yml",
    ]:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "postgres" in payload["services"]
        assert "api" in payload["services"] or "conformance" in payload["services"]
        serialized = str(payload)
        assert "postgresql://matterhorn:matterhorn@postgres:5432/matterhorn" in serialized


def test_ci_has_python_matrix_and_required_postgres_conformance_job() -> None:
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for version in ["3.11", "3.12", "3.13"]:
        assert version in text
    assert "postgres-conformance:" in text
    assert "MATTERHORN_TEST_POSTGRES_DSN" in text
    assert "tests/test_conformance.py" in text


def test_required_release_documents_and_examples_exist() -> None:
    required = [
        "CHANGELOG.md",
        "SECURITY.md",
        "docs/getting-started.md",
        "docs/core-concepts.md",
        "docs/schema-authoring.md",
        "docs/mcp-claude-code.md",
        "docs/corrections.md",
        "docs/slack.md",
        "docs/positioning.md",
        "examples/claude-code/README.md",
        "examples/embedded/README.md",
        "examples/service/README.md",
        "examples/correction/README.md",
        "examples/slack/README.md",
        "examples/slack/demo.py",
        "examples/demo-messages.yaml",
    ]
    assert all((ROOT / item).is_file() for item in required)
