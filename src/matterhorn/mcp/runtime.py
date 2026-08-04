from __future__ import annotations

import os

from matterhorn.engine import Engine
from matterhorn.engine.engine import (
    DEFAULT_STAGING_RETENTION_DAYS,
    validate_staging_retention_days,
)
from matterhorn.gateway_config import configured_gateway
from matterhorn.mcp.server import create_server
from matterhorn.service import MatterhornService


def run_stdio(
    *,
    db: str | None = None,
    schema: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    fixture_path: str | None = None,
    timeout: float | None = None,
) -> None:
    engine = Engine(
        db or os.environ.get("MATTERHORN_DB", "matterhorn.db"),
        schema or os.environ.get("MATTERHORN_SCHEMA", "org-matters/v1"),
        gateway=configured_gateway(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            fixture_path=fixture_path,
            timeout=timeout,
        ),
        staging_retention_days=validate_staging_retention_days(
            os.environ.get(
                "MATTERHORN_STAGING_RETENTION_DAYS",
                str(DEFAULT_STAGING_RETENTION_DAYS),
            )
        ),
    )
    create_server(MatterhornService(engine)).run(transport="stdio")
