from __future__ import annotations

import os
import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from matterhorn import Engine
from matterhorn.adapters.email_mbox import map_mbox
from matterhorn.gateway_config import FixtureFileGateway
from matterhorn.html import render_scope_html

ROOT = Path(__file__).resolve().parents[1]
EMAIL_EXAMPLE = ROOT / "examples" / "email"


def test_demo_mbox_maps_two_threads_and_filters_bulk() -> None:
    mapped = map_mbox(EMAIL_EXAMPLE / "demo.mbox")

    assert len(mapped.records) == 17
    assert mapped.dropped == {"AUTOMATED": 1}
    assert not any("relay-bulk" in item.record_id for item in mapped.records)
    assert Counter(item.thread_id for item in mapped.records) == {
        "email:relay-01@lumenfinch.example": 14,
        "email:accept-01@lumenfinch.example": 3,
    }


def test_fixture_chain_renders_all_story_beats_and_resolved_anchors(
    tmp_path: Path,
) -> None:
    mapped = map_mbox(EMAIL_EXAMPLE / "demo.mbox")
    start = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
    engine = Engine(
        tmp_path / "email-demo.db",
        gateway=FixtureFileGateway(
            EMAIL_EXAMPLE / "fixture-gateway.json"
        ),
        clock=[start + timedelta(seconds=index) for index in range(20)],
    )

    report = engine.add_records(mapped.records, scope_id="email-demo")
    dream = engine.dream("email-demo")
    rendered = render_scope_html(engine.export("email-demo"), engine.profile)

    assert report.cards_accepted == 15
    assert report.cards_dropped == 0
    assert dream.processed == 15
    assert "Due set to 2026-06-01" in rendered
    assert (
        "Due changed from 2026-06-01 to 2026-06-10" in rendered
    )
    assert (
        "Due changed from 2026-06-10 to 2026-06-20" in rendered
    )
    assert "Adopt option A" in rendered
    assert "explicitly switch to option B" in rendered
    assert "Owner released: mira.venn@lumenfinch.example" in rendered
    assert "Owner assigned: theo.rill@pebblearc.example" in rendered
    assert "2026-06-15 · overdue" in rendered
    assert rendered.count('class="source" id="source-email-') == 17

    hrefs = _attribute_values(rendered, 'href="#')
    ids = set(_attribute_values(rendered, 'id="'))
    assert hrefs
    assert all(href in ids for href in hrefs)


def test_run_script_without_credentials_fails_before_creating_output(
    tmp_path: Path,
) -> None:
    environment = {
        "PATH": os.environ["PATH"],
        "MATTERHORN_MH_BIN": "true",
        "MATTERHORN_EMAIL_DEMO_DIR": str(tmp_path / "must-not-exist"),
    }

    completed = subprocess.run(
        [str(EMAIL_EXAMPLE / "run.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 2
    assert "set MATTERHORN_MODEL" in completed.stderr
    assert not (tmp_path / "must-not-exist").exists()


def _attribute_values(document: str, prefix: str) -> list[str]:
    result: list[str] = []
    for part in document.split(prefix)[1:]:
        result.append(part.split('"', 1)[0])
    return result
