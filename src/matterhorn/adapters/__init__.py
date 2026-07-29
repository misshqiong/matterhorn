"""Write-side and deterministic ecosystem adapters."""

from matterhorn.adapters.email_mbox import (
    EmailMappingResult,
    map_email_file,
    map_email_message,
    map_eml,
    map_mbox,
)
from matterhorn.adapters.messages import (
    ChatMessage,
    MessageCardExtractor,
    MessageExtractionReport,
    RecordCardExtractor,
)
from matterhorn.adapters.openviking import map_openviking_digest
from matterhorn.adapters.reme import map_reme_digest
from matterhorn.adapters.slack import (
    SlackHistoryResult,
    map_slack_event,
    map_slack_history,
    map_slack_message,
)

__all__ = [
    "ChatMessage",
    "EmailMappingResult",
    "MessageCardExtractor",
    "MessageExtractionReport",
    "RecordCardExtractor",
    "SlackHistoryResult",
    "map_email_file",
    "map_email_message",
    "map_eml",
    "map_mbox",
    "map_openviking_digest",
    "map_reme_digest",
    "map_slack_event",
    "map_slack_history",
    "map_slack_message",
]
