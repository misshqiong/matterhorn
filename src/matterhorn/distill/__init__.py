"""Write-side semantic distillation.

This package is intentionally absent from every read-side import graph.
"""

from matterhorn.distill.gate import (
    GateReason,
    GateRejection,
    GateReport,
    SemanticCandidate,
    validate_response,
)
from matterhorn.distill.gateway import (
    AnthropicGateway,
    LlmGateway,
    NullGateway,
    OpenAICompatibleGateway,
    ToolLoopGateway,
    ToolLoopResult,
)
from matterhorn.distill.prompt import PromptContract, build_prompt

__all__ = [
    "AnthropicGateway",
    "GateReason",
    "GateRejection",
    "GateReport",
    "LlmGateway",
    "NullGateway",
    "OpenAICompatibleGateway",
    "PromptContract",
    "SemanticCandidate",
    "ToolLoopGateway",
    "ToolLoopResult",
    "build_prompt",
    "validate_response",
]
