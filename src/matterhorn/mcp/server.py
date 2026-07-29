from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as error:  # pragma: no cover - dependency failure path
    raise ImportError(
        "Matterhorn MCP support requires the official MCP SDK; "
        "install it with `pip install 'matterhorn[mcp]'`."
    ) from error

from matterhorn.contracts import Correction, EpisodeCard, Message, Record
from matterhorn.contracts.models import StrictModel
from matterhorn.service import MatterhornService


class ToolError(StrictModel):
    code: str
    message: str


class ToolResponse(StrictModel):
    ok: bool
    data: Any = None
    error: ToolError | None = None


def _safe(call: Callable[[], Any]) -> ToolResponse:
    try:
        return ToolResponse(ok=True, data=call())
    # The transport contract converts every service failure to a typed response.
    except Exception as error:  # noqa: BLE001
        return ToolResponse(
            ok=False,
            error=ToolError(code=type(error).__name__, message=str(error)),
        )


def create_server(service: MatterhornService) -> FastMCP:
    server = FastMCP(
        "matterhorn",
        instructions=(
            "Use add_messages as the default write door, then list_matters and "
            "query tools for deterministic evidence-backed answers. add_cards and "
            "add_records are advanced inputs. Use correct when a human identifies "
            "an error."
        ),
    )

    @server.tool(name="add_messages")
    def add_messages(
        messages: list[Message],
        scope_id: str,
        wait: bool = False,
    ) -> ToolResponse:
        """Default door: queue minimal messages and return a persistent task receipt."""
        return _safe(
            lambda: service.add_messages(
                messages=messages,
                scope_id=scope_id,
                wait=wait,
            )
        )

    @server.tool(name="add_cards")
    def add_cards(
        cards: list[EpisodeCard],
        scope_id: str,
        wait: bool = False,
    ) -> ToolResponse:
        """Advanced: queue validated EpisodeCards when you already have an L1 extractor."""
        return _safe(
            lambda: service.add_cards(
                cards=cards,
                scope_id=scope_id,
                wait=wait,
            )
        )

    @server.tool(name="add_records")
    def add_records(
        records: list[Record],
        scope_id: str,
        cursors: dict[str, str] | None = None,
        backfill: bool = False,
    ) -> ToolResponse:
        """Advanced/internal: ingest provider-normalized traceable Records."""
        return _safe(
            lambda: service.add_records(
                records=records,
                scope_id=scope_id,
                cursors=cursors,
                backfill=backfill,
            )
        )

    @server.tool(name="query_current")
    def query_current(
        scope_id: str, subject_key: str, predicate: str
    ) -> ToolResponse:
        """Use when the agent needs the value that is currently true."""
        return _safe(
            lambda: service.query_current(
                scope_id=scope_id, subject_key=subject_key, predicate=predicate
            )
        )

    @server.tool(name="query_timeline")
    def query_timeline(
        scope_id: str, subject_key: str, predicate: str
    ) -> ToolResponse:
        """Use to explain how a value changed and which evidence supported each interval."""
        return _safe(
            lambda: service.query_timeline(
                scope_id=scope_id, subject_key=subject_key, predicate=predicate
            )
        )

    @server.tool(name="query_at")
    def query_at(
        scope_id: str,
        subject_key: str,
        predicate: str,
        instant: datetime,
    ) -> ToolResponse:
        """Use to reconstruct what was true at a specific effective-time instant."""
        return _safe(
            lambda: service.query_at(
                scope_id=scope_id,
                subject_key=subject_key,
                predicate=predicate,
                instant=instant,
            )
        )

    @server.tool(name="query_by_person")
    def query_by_person(scope_id: str, person_id: str) -> ToolResponse:
        """Use to find current subjects related to a known person identifier."""
        return _safe(
            lambda: service.query_by_person(scope_id=scope_id, person_id=person_id)
        )

    @server.tool(name="list_matters")
    def list_matters(scope_id: str) -> ToolResponse:
        """Default read door: list deterministic projected matters in a scope."""
        return _safe(lambda: service.list_matters(scope_id=scope_id))

    @server.tool(name="correct")
    def correct(correction: Correction) -> ToolResponse:
        """Use immediately when a human says stored memory is wrong or outdated."""
        return _safe(lambda: service.correct(correction=correction))

    return server
