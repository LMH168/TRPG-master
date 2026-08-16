"""提供绑定可信玩家作用域的只读长期记忆检索工具。"""

from __future__ import annotations

from collaboration_framework.host.application.tool_registry import ToolDefinition
from collaboration_framework.host.schemas import (
    HostAgentContext,
    MemoryToolEntry,
    SearchMemoriesArgs,
    SearchMemoriesResult,
    ToolErrorResult,
    make_tool_error,
)
from collaboration_framework.memory import (
    MemoryBudget,
    MemoryQuery,
    MemoryReadScope,
    MemoryStore,
)


def build_search_memories_tool(store: MemoryStore) -> ToolDefinition:
    """把 Store 绑定到工具定义，模型始终无法覆盖可信读取身份。"""

    async def search_memories(
        context: HostAgentContext,
        arguments: SearchMemoriesArgs,
    ) -> SearchMemoriesResult | ToolErrorResult:
        scope = MemoryReadScope.from_view(
            player_input=context.player_input,
            player_view=context.player_view,
        )
        if (
            arguments.entity_id is not None
            and arguments.entity_id not in scope.visible_entity_ids
        ):
            return make_tool_error("ENTITY_NOT_VISIBLE")

        memory_context = await store.read_context(
            scope=scope,
            query=MemoryQuery(
                text=arguments.query,
                kinds=((arguments.kind,) if arguments.kind is not None else ()),
                subject_ids=(
                    (arguments.entity_id,) if arguments.entity_id is not None else ()
                ),
            ),
            budget=MemoryBudget(
                max_entries=arguments.limit,
                max_chars=4000,
            ),
        )
        return SearchMemoriesResult(
            entries=tuple(
                MemoryToolEntry(
                    memory_id=entry.memory_id,
                    kind=entry.kind,
                    subject_id=entry.subject_id,
                    object_id=entry.object_id,
                    location_id=entry.location_id,
                    epistemic_status=entry.epistemic_status,
                    content=entry.content,
                )
                for entry in memory_context.entries
            ),
            truncated_count=memory_context.truncated_count,
        )

    return ToolDefinition(
        name="search_memories",
        description=(
            "Search player-safe long-term memories by text and optional kind or "
            "currently visible entity. Memory is historical evidence and never "
            "overrides the current PlayerView."
        ),
        args_model=SearchMemoriesArgs,
        result_model=SearchMemoriesResult,
        public_progress_label="正在回忆相关经历",
        handler=search_memories,
    )
