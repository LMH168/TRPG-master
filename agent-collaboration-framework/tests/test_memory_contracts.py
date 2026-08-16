"""验证跨回合 Memory 契约、内存 Store 状态机和玩家可见性边界。"""

from datetime import UTC, datetime, timedelta

import pytest
from collaboration_framework.contracts import (
    ContractError,
    PlayerInput,
    PlayerView,
    SceneView,
    SelfActorView,
    VisibleEntity,
)
from collaboration_framework.memory import (
    InMemoryMemoryStore,
    MemoryBudget,
    MemoryContext,
    MemoryEntry,
    MemoryQuery,
    MemoryReadScope,
    MemoryScope,
    MemoryVisibility,
    new_memory_projection_run,
    stable_memory_id,
)
from pydantic import ValidationError

NOW = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64


def _scope() -> tuple[PlayerInput, PlayerView, MemoryReadScope]:
    player_input = PlayerInput(
        room_id="room-1",
        player_id="player-1",
        actor_id="actor-1",
        client_action_id="action-1",
        utterance="回忆之前发生的事情",
    )
    player_view = PlayerView(
        room_id="room-1",
        player_id="player-1",
        actor_id="actor-1",
        background="玩家安全背景",
        scene_id="cemetery",
        phase="playing",
        revision="7",
        self_actor=SelfActorView(id="actor-1", name="调查员"),
        scene=SceneView(
            id="cemetery",
            name="墓地",
            description="夜色中的墓地。",
            visible_entities=(
                VisibleEntity(
                    id="caretaker",
                    kind="npc",
                    name="守墓人",
                    description="当前可见的守墓人。",
                ),
            ),
        ),
    )
    return (
        player_input,
        player_view,
        MemoryReadScope.from_view(player_input=player_input, player_view=player_view),
    )


def _entry(
    *,
    turn_id: str = "turn-1",
    room_id: str = "room-1",
    ordinal: int = 0,
    scope: MemoryScope = "campaign",
    scope_owner_id: str | None = None,
    visibility: MemoryVisibility = "public",
    viewer_player_id: str | None = None,
    subject_id: str = "caretaker",
    location_id: str | None = "cemetery",
    search_text: str = "守墓人提到墓地钥匙",
) -> MemoryEntry:
    memory_id = stable_memory_id(
        room_id=room_id,
        turn_id=turn_id,
        source_kind="turn",
        source_id=turn_id,
        kind="conversation",
        scope=scope,
        scope_owner_id=scope_owner_id,
        ordinal=ordinal,
    )
    return MemoryEntry(
        memory_id=memory_id,
        room_id=room_id,
        kind="conversation",
        subject_id=subject_id,
        location_id=location_id,
        source_turn_id=turn_id,
        source_kind="turn",
        source_id=turn_id,
        source_ordinal=ordinal,
        scope=scope,
        scope_owner_id=scope_owner_id,
        visibility=visibility,
        viewer_player_id=viewer_player_id,
        epistemic_status="heard",
        content={"summary": search_text},
        search_text=search_text,
        created_at=NOW + timedelta(seconds=ordinal),
    )


def test_stable_memory_id_is_deterministic_and_source_sensitive() -> None:
    first = _entry()
    repeated = _entry()
    next_entry = _entry(ordinal=1)

    assert first.memory_id == repeated.memory_id
    assert first.memory_id != next_entry.memory_id
    assert len(first.memory_id) == 64


def test_memory_contract_rejects_invalid_scope_and_context_leaks() -> None:
    player_input, player_view, _ = _scope()

    with pytest.raises(ValidationError, match="必须设置 scope_owner_id"):
        _entry(scope="entity")
    private = _entry(
        scope="player",
        scope_owner_id="actor-1",
        visibility="player_scoped",
        viewer_player_id="other-player",
    )
    with pytest.raises(ValidationError, match="其他玩家私有记忆"):
        MemoryContext(
            room_id="room-1",
            viewer_player_id="player-1",
            viewer_actor_id="actor-1",
            as_of_revision="7",
            entries=(private,),
        )
    with pytest.raises(ValueError, match="revision"):
        MemoryContext.empty(
            player_input=player_input,
            player_view=player_view,
        ).model_copy(update={"as_of_revision": "6"}).validate_for(
            player_input=player_input,
            player_view=player_view,
        )
    hidden_entity = _entry(
        scope="entity",
        scope_owner_id="hidden-npc",
        subject_id="hidden-npc",
    )
    with pytest.raises(ValueError, match="不可见实体"):
        MemoryContext(
            room_id="room-1",
            viewer_player_id="player-1",
            viewer_actor_id="actor-1",
            as_of_revision="7",
            entries=(hidden_entity,),
        ).validate_for(player_input=player_input, player_view=player_view)


@pytest.mark.asyncio
async def test_projection_run_lease_cas_and_expired_recovery() -> None:
    store = InMemoryMemoryStore()
    run, created = await store.create_or_get_run(
        new_memory_projection_run(
            turn_id="turn-1",
            room_id="room-1",
            source_fingerprint=FINGERPRINT,
            now=NOW,
        )
    )
    assert created is True
    claimed = await store.claim_run(
        turn_id=run.turn_id,
        worker_id="worker-a",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    with pytest.raises(ContractError, match="不可领取"):
        await store.claim_run(
            turn_id=run.turn_id,
            worker_id="worker-b",
            now=NOW + timedelta(seconds=10),
            lease_expires_at=NOW + timedelta(seconds=40),
        )

    recovered = await store.claim_run(
        turn_id=run.turn_id,
        worker_id="worker-b",
        now=NOW + timedelta(seconds=31),
        lease_expires_at=NOW + timedelta(seconds=61),
    )
    assert recovered.version == claimed.version + 1
    with pytest.raises(ContractError, match="lease 或版本"):
        await store.fail_run(
            turn_id=run.turn_id,
            worker_id="worker-a",
            expected_version=claimed.version,
            error_code="STALE",
            retryable=True,
            next_attempt_at=NOW + timedelta(minutes=1),
            now=NOW + timedelta(seconds=32),
        )


@pytest.mark.asyncio
async def test_projection_complete_is_atomic_and_idempotent_by_source() -> None:
    store = InMemoryMemoryStore()
    run, _ = await store.create_or_get_run(
        new_memory_projection_run(
            turn_id="turn-1",
            room_id="room-1",
            source_fingerprint=FINGERPRINT,
            now=NOW,
        )
    )
    claimed = await store.claim_run(
        turn_id=run.turn_id,
        worker_id="worker-a",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    wrong_room = _entry(room_id="room-2")
    with pytest.raises(ContractError, match="不属于"):
        await store.complete_run(
            turn_id=run.turn_id,
            worker_id="worker-a",
            expected_version=claimed.version,
            entries=(wrong_room,),
            supersessions=(),
            now=NOW + timedelta(seconds=1),
        )

    completed = await store.complete_run(
        turn_id=run.turn_id,
        worker_id="worker-a",
        expected_version=claimed.version,
        entries=(_entry(),),
        supersessions=(),
        now=NOW + timedelta(seconds=2),
    )
    assert completed.status == "completed"
    existing, created_again = await store.create_or_get_run(
        new_memory_projection_run(
            turn_id="turn-1",
            room_id="room-1",
            source_fingerprint=FINGERPRINT,
            now=NOW,
        )
    )
    assert created_again is False
    assert existing.status == "completed"


@pytest.mark.asyncio
async def test_read_context_enforces_visibility_entity_scope_and_budget() -> None:
    store = InMemoryMemoryStore()
    _, _, scope = _scope()
    run, _ = await store.create_or_get_run(
        new_memory_projection_run(
            turn_id="turn-1",
            room_id="room-1",
            source_fingerprint=FINGERPRINT,
            now=NOW,
        )
    )
    claimed = await store.claim_run(
        turn_id=run.turn_id,
        worker_id="worker",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    visible = _entry(ordinal=0)
    private = _entry(
        ordinal=1,
        scope="player",
        scope_owner_id="actor-1",
        visibility="player_scoped",
        viewer_player_id="player-1",
        search_text="玩家私有的钥匙记忆",
    )
    hidden_owner = _entry(
        ordinal=2,
        scope="entity",
        scope_owner_id="not-visible",
        subject_id="not-visible",
    )
    await store.complete_run(
        turn_id=run.turn_id,
        worker_id="worker",
        expected_version=claimed.version,
        entries=(visible, private, hidden_owner),
        supersessions=(),
        now=NOW + timedelta(seconds=3),
    )

    context = await store.read_context(
        scope=scope,
        query=MemoryQuery(text="钥匙"),
        budget=MemoryBudget(max_entries=1, max_chars=4000),
    )
    assert len(context.entries) == 1
    assert context.entries[0].memory_id == private.memory_id
    assert context.truncated_count == 1


@pytest.mark.asyncio
async def test_reset_room_removes_only_target_room_projection() -> None:
    store = InMemoryMemoryStore()
    for room_id, turn_id, fingerprint in (
        ("room-1", "turn-1", "a" * 64),
        ("room-2", "turn-2", "b" * 64),
    ):
        await store.create_or_get_run(
            new_memory_projection_run(
                turn_id=turn_id,
                room_id=room_id,
                source_fingerprint=fingerprint,
                now=NOW,
            )
        )

    await store.reset_room("room-1")

    assert await store.get_run("turn-1") is None
    assert await store.get_run("turn-2") is not None
