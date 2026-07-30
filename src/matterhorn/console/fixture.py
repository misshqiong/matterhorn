"""Fixture-gateway fallback used only by the built-in Console sample."""

from __future__ import annotations

from importlib import resources
from typing import Any

from matterhorn.gateway_config import FixtureFileGateway

SAMPLE_MARKER = "octo-org Console launch"


class ConsoleSampleGateway:
    """Route the shipped fictional sample to a packaged fixture transcript."""

    def __init__(self, delegate: Any):
        self.delegate = delegate

    def complete(
        self, *, system: str, user: str, response_schema: dict
    ) -> str:
        if SAMPLE_MARKER not in user:
            return self.delegate.complete(
                system=system,
                user=user,
                response_schema=response_schema,
            )
        fixture = resources.files("matterhorn").joinpath(
            "fixtures/console-demo-gateway.json"
        )
        with resources.as_file(fixture) as path:
            return FixtureFileGateway(path).complete(
                system=system,
                user=user,
                response_schema=response_schema,
            )
