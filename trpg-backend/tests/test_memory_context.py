"""验证生产回合读取长期 MemoryContext 时的作用域绑定与安全降级。"""

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from collaboration_framework.contracts import (
    PlayerInput,
    PlayerView,
    SceneView,
    SelfActorView,
    VisibleEntity,
)
from collaboration_framework.memory import (
    MemoryBudget,
    MemoryContext,
    MemoryEntry,
    MemoryReadScope,
    stable_memory_id,
)

from app.adapters import SqlAlchemyMemoryStore
from app.core.action_plan_turn import (
    ActionPlanTurnApplication,
    action_plan_turn_application,
)

NOW = datetime(2026, 8, 16, 13, 0, tzinfo=UTC)


def _scope() -> tuple[PlayerInput, PlayerView]:
    player_input = PlayerInput(
        room_id="room-context",
        player_id="player-context",
        actor_id="actor-context",
        client_action_id="action-context",
        utterance="继续询问守墓人",
    )
    player_view = PlayerView(
        room_id="room-context",
        player_id="player-context",
        actor_id="actor-context",
        background="玩家安全背景",
        scene_id="cemetery",
        phase="playing",
        revision="12",
        self_actor=SelfActorView(id="actor-context", name="调查员"),
        scene=SceneView(
            id="cemetery",
            name="墓地",
            description="墓地门房就在前方。",
            visible_entities=(
                VisibleEntity(
                    id="caretaker",
                    kind="npc",
                    name="守墓人",
                    description="守墓人仍在门房前。",
                ),
            ),
        ),
    )
    return player_input, player_view


def _memory() -> MemoryEntry:
    return MemoryEntry(
        memory_id=stable_memory_id(
            room_id="room-context",
            turn_id="turn-old",
            source_kind="turn",
            source_id="turn-old",
            kind="conversation",
            scope="entity",
            scope_owner_id="caretaker",
            ordinal=0,
        ),
        room_id="room-context",
        kind="conversation",
        subject_id="caretaker",
        object_id="actor-context",
        location_id="cemetery",
        source_turn_id="turn-old",
        source_kind="turn",
        source_id="turn-old",
        source_ordinal=0,
        scope="entity",
        scope_owner_id="caretaker",
        visibility="player_scoped",
        viewer_player_id="player-context",
        epistemic_status="heard",
        content={"summary": "玩家曾询问银钥匙"},
        search_text="玩家曾询问银钥匙",
        created_at=NOW,
    )


class _MemoryStore:
    def __init__(self, context: MemoryContext) -> None:
        self.context = context
        self.calls: list[tuple[MemoryReadScope, MemoryBudget]] = []

    async def read_context(self, *, scope, query, budget):
        del query
        self.calls.append((scope, budget))
        return self.context


@pytest.mark.asyncio
async def test_action_plan_application_reads_revision_bound_memory_context() -> None:
    player_input, player_view = _scope()
    expected = MemoryContext(
        room_id=player_view.room_id,
        viewer_player_id=player_view.player_id,
        viewer_actor_id=player_view.actor_id,
        as_of_revision=player_view.revision,
        entries=(_memory(),),
    )
    store = _MemoryStore(expected)
    application = object.__new__(ActionPlanTurnApplication)
    application._memory_store = cast(Any, store)
    application._memory_budget = MemoryBudget(max_entries=8, max_chars=4000)

    result = await application._read_memory_context(
        player_input=player_input,
        player_view=player_view,
    )

    assert result == expected
    assert store.calls[0][0] == MemoryReadScope.from_view(
        player_input=player_input,
        player_view=player_view,
    )
    assert store.calls[0][1] == MemoryBudget(max_entries=8, max_chars=4000)


@pytest.mark.asyncio
async def test_invalid_memory_scope_degrades_without_exposing_entries() -> None:
    player_input, player_view = _scope()
    invalid = MemoryContext(
        room_id=player_view.room_id,
        viewer_player_id="other-player",
        viewer_actor_id=player_view.actor_id,
        as_of_revision=player_view.revision,
    )
    application = object.__new__(ActionPlanTurnApplication)
    application._memory_store = cast(Any, _MemoryStore(invalid))
    application._memory_budget = MemoryBudget()

    result = await application._read_memory_context(
        player_input=player_input,
        player_view=player_view,
    )

    assert result.entries == ()
    assert result.viewer_player_id == player_view.player_id


def test_production_application_uses_sql_memory_store() -> None:
    assert isinstance(action_plan_turn_application._memory_store, SqlAlchemyMemoryStore)
