from __future__ import annotations

from pathlib import Path


def test_ledger_workflow_is_rebuildable_and_fails_closed() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ledger.yml"
    ).read_text(encoding="utf-8")

    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "fetch-depth: 0" in workflow
    assert "permissions:\n  contents: write\n" in workflow
    assert "rm -f ledger/dev.db" in workflow
    assert "mh import ledger/assertions.json --db ledger/dev.db" in workflow
    assert "--batch-size 8" in workflow
    assert "--out ledger/assertions.json" in workflow
    assert "--format markdown" in workflow
    assert "--out MATTERS.md" in workflow
    assert 'git commit -m "ledger: nightly update"' in workflow
    assert "git status --porcelain -- ledger/ MATTERS.md" in workflow
    assert "fixture" not in workflow.lower()

    for name in [
        "MATTERHORN_PROVIDER",
        "MATTERHORN_BASE_URL",
        "MATTERHORN_MODEL",
        "MATTERHORN_API_KEY",
        "MATTERHORN_TIMEOUT",
    ]:
        assert f"secrets.{name}" in workflow
    assert "Required GitHub secret ${name} is not configured" in workflow
