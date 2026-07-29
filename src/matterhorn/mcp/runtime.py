from __future__ import annotations

import os

from matterhorn.engine import Engine
from matterhorn.mcp.server import create_server
from matterhorn.service import MatterhornService


def run_stdio(
    *,
    db: str | None = None,
    schema: str | None = None,
) -> None:
    engine = Engine(
        db or os.environ.get("MATTERHORN_DB", "matterhorn.db"),
        schema or os.environ.get("MATTERHORN_SCHEMA", "org-matters/v1"),
    )
    create_server(MatterhornService(engine)).run(transport="stdio")
