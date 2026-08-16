"""验证 Host 长期记忆工具的可信作用域、预算与玩家安全输出。"""

from datetime import UTC, datetime, timedelta

import pytest

from collaboration_framework.contracts import (
    PlayerInput,
    PlayerView,
    SceneView,
    SelfActorView,
    VisibleEntity,
)
from collaboration_framework.host.schemas import (
    HostAgentContext,
    RecentTurnContext,
    SearchMemoriesResult,
    ToolErrorResult,
)
from collaboration_framework.host.tools import build_player_view_tool_registry
from collaboration_framework.memory import (
    InMemoryMemoryStore,
    MemoryContext,
    MemoryEntry,
    new_memory_projection_run,
    stable_memory_id,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def _scope() -> tuple[PlayerInput, PlayerView]:
    player_input = PlayerInput(
        room_id="room-memory",
        player_id="player-memory",
        actor_id="actor-memory",
        client_action_id="action-memory",
        utterance="守墓人以前说过钥匙吗",
    )
    player_view = PlayerView(
        room_id="room-memory",
        player_id="player-memory",
        actor_id="actor-memory",
        background="玩家安全背景",
        scene_id="cemetery",
        phase="playing",
        revision="9",
        self_actor=SelfActorView(id="actor-memory", name="调查员"),
        scene=SceneView(
            id="cemetery",
            name="墓地",
            description="夜色中的墓地。",
            visible_entities=(
                VisibleEntity(
                    id="caretaker",
                    kind="npc",
                    name="守墓人",
                    description="守墓人站在门房前。",
                ),
            ),
        ),
    )
    return player_input, player_view


def _entry(*, ordinal: int, owner: str, text: str) -> MemoryEntry:
    memory_id = stable_memory_id(
        room_id="room-memory",
        turn_id="turn-memory",
        source_kind="turn",
        source_id="turn-memory",
        kind="conversation",
        scope="entity",
        scope_owner_id=owner,
        ordinal=ordinal,
    )
    return MemoryEntry(
        memory_id=memory_id,
        room_id="room-memory",
        kind="conversation",
        subject_id=owner,
        object_id="actor-memory",
        location_id="cemetery",
        source_turn_id="turn-memory",
        source_kind="turn",
        source_id="turn-memory",
        source_ordinal=ordinal,
        scope="entity",
        scope_owner_id=owner,
        visibility="player_scoped",
        viewer_player_id="player-memory",
        epistemic_status="heard",
        content={"summary": text},
        search_text=text,
        created_at=NOW + timedelta(seconds=ordinal),
    )


async def _store() -> InMemoryMemoryStore:
    store = InMemoryMemoryStore()
    run, _ = await store.create_or_get_run(
        new_memory_projection_run(
            turn_id="turn-memory",
            room_id="room-memory",
            source_fingerprint="a" * 64,
            now=NOW,
        )
    )
    claimed = await store.claim_run(
        turn_id=run.turn_id,
        worker_id="worker-memory",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    await store.complete_run(
        turn_id=run.turn_id,
        worker_id="worker-memory",
        expected_version=claimed.version,
        entries=(
            _entry(ordinal=0, owner="caretaker", text="守墓人听玩家提到墓地钥匙"),
            _entry(ordinal=1, owner="hidden-npc", text="隐藏人物知道墓地钥匙"),
        ),
        supersessions=(),
        now=NOW + timedelta(seconds=2),
    )
    return store


@pytest.mark.asyncio
async def test_search_memories_binds_scope_and_hides_invisible_entities() -> None:
    store = await _store()
    player_input, player_view = _scope()
    context = HostAgentContext(
        player_input=player_input,
        player_view=player_view,
        recent_history=RecentTurnContext.empty(
            player_input=player_input,
            player_view=player_view,
        ),
        memory_context=MemoryContext.empty(
            player_input=player_input,
            player_view=player_view,
        ),
    )
    registry = build_player_view_tool_registry(store).bind(context)

    result = await registry.ainvoke(
        "search_memories",
        {"query": "墓地钥匙", "entity_id": "caretaker"},
    )

    assert isinstance(result, SearchMemoriesResult)
    assert [entry.subject_id for entry in result.entries] == ["caretaker"]
    assert result.entries[0].epistemic_status == "heard"
    rendered = result.model_dump_json()
    assert "room-memory" not in rendered
    assert "player-memory" not in rendered
    assert "hidden-npc" not in rendered

    hidden = await registry.ainvoke(
        "search_memories",
        {"query": "墓地钥匙", "entity_id": "hidden-npc"},
    )
    assert isinstance(hidden, ToolErrorResult)
    assert hidden.error.code == "ENTITY_NOT_VISIBLE"

    missing = await registry.ainvoke(
        "search_memories",
        {"query": "从未发生过的事情"},
    )
    assert isinstance(missing, SearchMemoriesResult)
    assert missing.entries == ()


@pytest.mark.asyncio
async def test_search_memories_schema_rejects_trusted_scope_fields() -> None:
    store = await _store()
    player_input, player_view = _scope()
    context = HostAgentContext(
        player_input=player_input,
        player_view=player_view,
        recent_history=RecentTurnContext.empty(
            player_input=player_input,
            player_view=player_view,
        ),
        memory_context=MemoryContext.empty(
            player_input=player_input,
            player_view=player_view,
        ),
    )
    result = (
        await build_player_view_tool_registry(store)
        .bind(context)
        .ainvoke(
            "search_memories",
            {
                "query": "墓地钥匙",
                "room_id": "other-room",
                "player_id": "other-player",
                "actor_id": "other-actor",
            },
        )
    )
    assert isinstance(result, ToolErrorResult)
    assert result.error.code == "INVALID_TOOL_ARGUMENTS"
