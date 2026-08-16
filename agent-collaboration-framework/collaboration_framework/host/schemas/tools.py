"""Framework-independent schemas for player-safe Host Agent tools."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import Field, JsonValue

from collaboration_framework.contracts import (
    CheckpointOption,
    ContractModel,
    NarrativeDetailView,
    ObservableStateView,
)
from collaboration_framework.memory import MemoryEpistemicStatus, MemoryKind

ToolErrorCode: TypeAlias = Literal[
    "TOOL_NOT_FOUND",
    "INVALID_TOOL_ARGUMENTS",
    "ENTITY_NOT_VISIBLE",
    "INVALID_TOOL_RESULT",
    "TOOL_INTERNAL_ERROR",
    "TOOL_TIMEOUT",
]


class SearchVisibleEntitiesArgs(ContractModel):
    query: str = Field(min_length=1, max_length=200)
    kind: Literal["npc", "object", "location"] | None = None
    limit: int = Field(default=5, ge=1, le=5)


class VisibleEntitySummary(ContractModel):
    id: str = Field(min_length=1)
    kind: Literal["npc", "object", "location"]
    name: str = Field(min_length=1)


class SearchVisibleEntitiesResult(ContractModel):
    matches: tuple[VisibleEntitySummary, ...] = ()


class GetVisibleEntityArgs(ContractModel):
    entity_id: str = Field(min_length=1)


class GetVisibleEntityResult(ContractModel):
    id: str = Field(min_length=1)
    kind: Literal["npc", "object", "location"]
    name: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    description: str
    narrative_details: tuple[NarrativeDetailView, ...] = ()
    observable_state: tuple[ObservableStateView, ...] = ()
    checkpoint_options: tuple[CheckpointOption, ...] = ()


class SearchMemoriesArgs(ContractModel):
    """模型可提供的长期记忆筛选项，不包含任何可信身份字段。"""

    query: str = Field(min_length=1, max_length=500)
    kind: MemoryKind | None = None
    entity_id: str | None = Field(default=None, min_length=1)
    limit: int = Field(default=8, ge=1, le=8)


class MemoryToolEntry(ContractModel):
    """供 Host 使用的最小玩家安全记忆摘要。"""

    memory_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: MemoryKind
    subject_id: str = Field(min_length=1)
    object_id: str | None = Field(default=None, min_length=1)
    location_id: str | None = Field(default=None, min_length=1)
    epistemic_status: MemoryEpistemicStatus
    content: dict[str, JsonValue] = Field(default_factory=dict)


class SearchMemoriesResult(ContractModel):
    entries: tuple[MemoryToolEntry, ...] = ()
    truncated_count: int = Field(default=0, ge=0)


class ToolError(ContractModel):
    code: ToolErrorCode
    message: str = Field(min_length=1)


class ToolErrorResult(ContractModel):
    error: ToolError


_ERROR_MESSAGES: dict[ToolErrorCode, str] = {
    "TOOL_NOT_FOUND": "The requested tool is not available.",
    "INVALID_TOOL_ARGUMENTS": "The tool arguments are invalid.",
    "ENTITY_NOT_VISIBLE": (
        "The requested entity is not available in the current player view."
    ),
    "INVALID_TOOL_RESULT": "The tool returned an invalid result.",
    "TOOL_INTERNAL_ERROR": "The tool could not complete the request.",
    "TOOL_TIMEOUT": "The tool timed out before completing the request.",
}


def make_tool_error(code: ToolErrorCode) -> ToolErrorResult:
    """Build a stable error without accepting provider or exception text."""

    return ToolErrorResult(
        error=ToolError(
            code=code,
            message=_ERROR_MESSAGES[code],
        )
    )
