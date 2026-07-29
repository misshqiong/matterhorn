"""Write-side and deterministic ecosystem adapters."""

from matterhorn.adapters.messages import (
    ChatMessage,
    MessageCardExtractor,
    MessageExtractionReport,
)
from matterhorn.adapters.openviking import map_openviking_digest
from matterhorn.adapters.reme import map_reme_digest

__all__ = [
    "ChatMessage",
    "MessageCardExtractor",
    "MessageExtractionReport",
    "map_openviking_digest",
    "map_reme_digest",
]
