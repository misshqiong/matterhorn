from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from matterhorn.contracts import ExportEnvelope
from matterhorn.contracts.schema import resolve_schema
from matterhorn.render import render_scope_html

FIXTURES = Path(__file__).parent / "fixtures" / "html"


class _HtmlInventory(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.fragment_hrefs: list[str] = []
        self.request_urls: list[tuple[str, str, str]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        href = values.get("href")
        if tag == "a" and href and href.startswith("#"):
            self.fragment_hrefs.append(href)
        for attribute in ("href", "src", "srcset"):
            value = values.get(attribute)
            if tag in {"link", "script", "img"} and value:
                self.request_urls.append((tag, attribute, value))


def fixture_html() -> str:
    snapshot = ExportEnvelope.model_validate_json(
        (FIXTURES / "ledger.json").read_text(encoding="utf-8")
    )
    profile = resolve_schema("org-matters/v1")
    first = render_scope_html(
        snapshot,
        profile,
        as_of="2026-06-01T12:01:00Z",
    )
    second = render_scope_html(
        snapshot,
        profile,
        as_of="2026-06-01T12:01:00Z",
    )
    assert first == second
    return first


def test_html_export_matches_golden_fixture() -> None:
    actual = fixture_html()
    expected = (FIXTURES / "ledger.sha256").read_text(encoding="utf-8").strip()

    assert hashlib.sha256(actual.encode("utf-8")).hexdigest() == expected
    assert "Open commitments" in actual
    assert "overdue" in actual
    assert "✏️ human correction" in actual
    assert "Source emails" in actual


def test_html_export_has_no_external_request_tags() -> None:
    inventory = _HtmlInventory()
    inventory.feed(fixture_html())

    external = [
        item
        for item in inventory.request_urls
        if urlsplit(item[2]).scheme or item[2].startswith("//")
    ]
    assert external == []


def test_every_in_page_evidence_link_resolves() -> None:
    inventory = _HtmlInventory()
    inventory.feed(fixture_html())

    assert inventory.fragment_hrefs
    assert all(href[1:] in inventory.ids for href in inventory.fragment_hrefs)


def test_html_contains_no_generation_timestamp() -> None:
    rendered = fixture_html()

    assert "generated_at" not in rendered
    assert "2026-06-01T12:01:00.000000Z" in rendered
    assert json.dumps({"scope": "email-demo"}) not in rendered


def test_source_link_resolution_is_pluggable_by_namespace() -> None:
    serialized = (FIXTURES / "ledger.json").read_text(encoding="utf-8")
    serialized = serialized.replace(
        "email:kickoff@project.example",
        "github:project:issue:7",
    )
    payload = json.loads(serialized)
    github_state = next(
        item
        for item in payload["source_states"]
        if item["source_id"] == "github:project:issue:7"
    )
    github_state["uri"] = "https://github.example/project/issues/7"
    snapshot = ExportEnvelope.model_validate(payload)
    profile = resolve_schema("org-matters/v1")

    external = render_scope_html(
        snapshot,
        profile,
        as_of="2026-06-01T12:01:00Z",
    )
    in_page = render_scope_html(
        snapshot,
        profile,
        as_of="2026-06-01T12:01:00Z",
        source_link_resolvers={
            "github": lambda _ref, anchor: (f"#{anchor}", False)
        },
    )

    assert (
        'href="https://github.example/project/issues/7" '
        'target="_blank" rel="noreferrer noopener"' in external
    )
    assert 'href="#source-github-' in in_page
    assert 'id="source-github-' in in_page
