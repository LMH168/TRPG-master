"""Player-safe Host Agent tool definitions."""

from collaboration_framework.host.application.tool_registry import ToolRegistry
from collaboration_framework.memory import MemoryStore

from .memories import build_search_memories_tool
from .visible_entities import (
    GET_VISIBLE_ENTITY_TOOL,
    SEARCH_VISIBLE_ENTITIES_TOOL,
    get_visible_entity,
    normalize_search_text,
    search_visible_entities,
)


def build_player_view_tool_registry(
    memory_store: MemoryStore | None = None,
) -> ToolRegistry:
    """Build the immutable first-party read-only tool set."""

    definitions = [
        SEARCH_VISIBLE_ENTITIES_TOOL,
        GET_VISIBLE_ENTITY_TOOL,
    ]
    if memory_store is not None:
        definitions.append(build_search_memories_tool(memory_store))
    return ToolRegistry(definitions)


__all__ = [
    "GET_VISIBLE_ENTITY_TOOL",
    "SEARCH_VISIBLE_ENTITIES_TOOL",
    "build_player_view_tool_registry",
    "build_search_memories_tool",
    "get_visible_entity",
    "normalize_search_text",
    "search_visible_entities",
]
