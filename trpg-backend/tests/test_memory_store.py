"""验证 SQLAlchemy Memory Store 的事务、约束、隔离与可重建语义。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from collaboration_framework.contracts import ContractError
from collaboration_framework.memory import (
    MemoryBudget,
    MemoryEntry,
    MemoryQuery,
    MemoryReadScope,
    MemoryVisibility,
    new_memory_projection_run,
    stable_memory_id,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import SqlAlchemyMemoryStore, SqlAlchemyTurnStore
from app.core.turn_runtime import (
    TurnInputSnapshot,
    TurnRecoveryAction,
    TurnResumePoint,
    TurnStatus,
    new_turn_record,
    transition_turn,
)
from app.models.memory import MemoryEntryRecord, MemoryProjectionRunRecord
from app.models.room import Player, Room

NOW = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)


async def _room_player_turn(
    db_session: AsyncSession,
    turn_store: SqlAlchemyTurnStore,
) -> tuple[str, str, str]:
    """建立 Memory 外键依赖的真实房间、玩家与可靠 Turn。"""

    room_id = str(uuid4())
    player_id = str(uuid4())
    db_session.add(
        Room(
            id=room_id,
            room_code=uuid4().hex[:6].upper(),
            room_name="Memory Store 测试",
            max_players=4,
            phase="InGame",
        )
    )
    db_session.add(
        Player(
            id=player_id,
            room_id=room_id,
            nickname="调查员",
            is_host=True,
            reconnect_token=str(uuid4()),
        )
    )
    await db_session.commit()
    turn, _ = await turn_store.create_or_get(
        new_turn_record(
            TurnInputSnapshot(
                room_id=room_id,
                player_id=player_id,
                actor_id="actor-1",
                client_action_id=f"action-{uuid4()}",
                utterance="询问守墓人",
            ),
            now=NOW,
        )
    )
    planning = transition_turn(
        turn,
        status=TurnStatus.PLANNING,
        resume_point=TurnResumePoint.PLANNING,
        recovery_action=TurnRecoveryAction.WAIT,
    )
    await turn_store.compare_and_swap(
        expected_phase_version=turn.phase_version,
        updated=planning,
    )
    failed = transition_turn(
        planning,
        status=TurnStatus.FAILED,
        resume_point=TurnResumePoint.NONE,
        recovery_action=TurnRecoveryAction.SUBMIT_NEW_INPUT,
    )
    await turn_store.compare_and_swap(
        expected_phase_version=planning.phase_version,
        updated=failed,
    )
    return room_id, player_id, failed.turn_id


def _entry(
    *,
    room_id: str,
    turn_id: str,
    ordinal: int,
    visibility: MemoryVisibility = "public",
    viewer_player_id: str | None = None,
) -> MemoryEntry:
    memory_id = stable_memory_id(
        room_id=room_id,
        turn_id=turn_id,
        source_kind="turn",
        source_id=turn_id,
        kind="conversation",
        scope="entity",
        scope_owner_id="caretaker",
        ordinal=ordinal,
    )
    return MemoryEntry(
        memory_id=memory_id,
        room_id=room_id,
        kind="conversation",
        subject_id="caretaker",
        location_id="cemetery",
        source_turn_id=turn_id,
        source_kind="turn",
        source_id=turn_id,
        source_ordinal=ordinal,
        scope="entity",
        scope_owner_id="caretaker",
        visibility=visibility,
        viewer_player_id=viewer_player_id,
        epistemic_status="heard",
        content={"summary": "守墓人谈到墓地钥匙"},
        search_text="守墓人 墓地 钥匙",
        created_at=NOW + timedelta(seconds=ordinal),
    )


@pytest.mark.asyncio
async def test_sql_projection_complete_and_player_safe_read(
    db_session: AsyncSession,
    turn_store_factory,
    memory_store_factory,
) -> None:
    turn_store: SqlAlchemyTurnStore = turn_store_factory()
    store: SqlAlchemyMemoryStore = memory_store_factory()
    room_id, player_id, turn_id = await _room_player_turn(db_session, turn_store)
    run, created = await store.create_or_get_run(
        new_memory_projection_run(
            turn_id=turn_id,
            room_id=room_id,
            source_fingerprint="a" * 64,
            now=NOW,
        )
    )
    claimed = await store.claim_run(
        turn_id=turn_id,
        worker_id="worker-a",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    public = _entry(
        room_id=room_id,
        turn_id=turn_id,
        ordinal=0,
    )
    private = _entry(
        room_id=room_id,
        turn_id=turn_id,
        ordinal=1,
        visibility="player_scoped",
        viewer_player_id=player_id,
    )
    completed = await store.complete_run(
        turn_id=turn_id,
        worker_id="worker-a",
        expected_version=claimed.version,
        entries=(public, private),
        supersessions=(),
        now=NOW + timedelta(seconds=1),
    )

    assert created is True
    assert run.status == "pending"
    assert completed.status == "completed"
    context = await store.read_context(
        scope=MemoryReadScope(
            room_id=room_id,
            viewer_player_id=player_id,
            viewer_actor_id="actor-1",
            as_of_revision="1",
            current_location_id="cemetery",
            visible_entity_ids=("caretaker",),
        ),
        query=MemoryQuery(),
        budget=MemoryBudget(),
    )
    assert {entry.memory_id for entry in context.entries} == {
        public.memory_id,
        private.memory_id,
    }

    other_context = await store.read_context(
        scope=MemoryReadScope(
            room_id=room_id,
            viewer_player_id=str(uuid4()),
            viewer_actor_id="actor-2",
            as_of_revision="1",
            current_location_id="cemetery",
            visible_entity_ids=("caretaker",),
        ),
        query=MemoryQuery(),
        budget=MemoryBudget(),
    )
    assert [entry.memory_id for entry in other_context.entries] == [public.memory_id]


@pytest.mark.asyncio
async def test_sql_text_search_reaches_memory_older_than_preload_window(
    db_session: AsyncSession,
    turn_store_factory,
    memory_store_factory,
) -> None:
    """显式长期搜索不能先按最新候选截断，避免退化为近期历史。"""

    turn_store: SqlAlchemyTurnStore = turn_store_factory()
    store: SqlAlchemyMemoryStore = memory_store_factory()
    room_id, player_id, turn_id = await _room_player_turn(db_session, turn_store)
    run, _ = await store.create_or_get_run(
        new_memory_projection_run(
            turn_id=turn_id,
            room_id=room_id,
            source_fingerprint="9" * 64,
            now=NOW,
        )
    )
    claimed = await store.claim_run(
        turn_id=turn_id,
        worker_id="worker-search",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    entries = tuple(
        _entry(room_id=room_id, turn_id=turn_id, ordinal=ordinal).model_copy(
            update={
                "search_text": (
                    "很久以前守墓人提到银钥匙" if ordinal == 0 else f"普通记忆 {ordinal}"
                )
            }
        )
        for ordinal in range(300)
    )
    await store.complete_run(
        turn_id=turn_id,
        worker_id="worker-search",
        expected_version=claimed.version,
        entries=entries,
        supersessions=(),
        now=NOW + timedelta(seconds=31),
    )

    context = await store.read_context(
        scope=MemoryReadScope(
            room_id=room_id,
            viewer_player_id=player_id,
            viewer_actor_id="actor-1",
            as_of_revision="1",
            current_location_id="cemetery",
            visible_entity_ids=("caretaker",),
        ),
        query=MemoryQuery(text="银钥匙"),
        budget=MemoryBudget(),
    )

    assert [entry.source_ordinal for entry in context.entries] == [0]


@pytest.mark.asyncio
async def test_sql_store_rejects_stale_lease_without_partial_entries(
    db_session: AsyncSession,
    turn_store_factory,
    memory_store_factory,
) -> None:
    turn_store: SqlAlchemyTurnStore = turn_store_factory()
    store: SqlAlchemyMemoryStore = memory_store_factory()
    room_id, player_id, turn_id = await _room_player_turn(db_session, turn_store)
    run, _ = await store.create_or_get_run(
        new_memory_projection_run(
            turn_id=turn_id,
            room_id=room_id,
            source_fingerprint="b" * 64,
            now=NOW,
        )
    )
    claimed = await store.claim_run(
        turn_id=run.turn_id,
        worker_id="worker-a",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=10),
    )
    recovered = await store.claim_run(
        turn_id=run.turn_id,
        worker_id="worker-b",
        now=NOW + timedelta(seconds=11),
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    with pytest.raises(ContractError, match="lease 或版本"):
        await store.complete_run(
            turn_id=run.turn_id,
            worker_id="worker-a",
            expected_version=claimed.version,
            entries=(
                _entry(
                    room_id=room_id,
                    turn_id=turn_id,
                    ordinal=0,
                ),
            ),
            supersessions=(),
            now=NOW + timedelta(seconds=12),
        )
    async with db_session.begin():
        entry_count = await db_session.scalar(select(func.count()).select_from(MemoryEntryRecord))
    assert entry_count == 0

    failed = await store.fail_run(
        turn_id=run.turn_id,
        worker_id="worker-b",
        expected_version=recovered.version,
        error_code="SOURCE_UNAVAILABLE",
        retryable=True,
        next_attempt_at=NOW + timedelta(minutes=1),
        now=NOW + timedelta(seconds=13),
    )
    assert failed.status == "retryable_failure"
    assert failed.attempt_count == 1
    assert await store.list_due_runs(now=NOW + timedelta(seconds=59), limit=10) == ()
    assert [
        item.turn_id for item in await store.list_due_runs(now=NOW + timedelta(minutes=1), limit=10)
    ] == [turn_id]


@pytest.mark.asyncio
async def test_sql_reset_room_removes_projection_but_keeps_turn(
    db_session: AsyncSession,
    turn_store_factory,
    memory_store_factory,
) -> None:
    turn_store: SqlAlchemyTurnStore = turn_store_factory()
    store: SqlAlchemyMemoryStore = memory_store_factory()
    room_id, _, turn_id = await _room_player_turn(db_session, turn_store)
    await store.create_or_get_run(
        new_memory_projection_run(
            turn_id=turn_id,
            room_id=room_id,
            source_fingerprint="c" * 64,
            now=NOW,
        )
    )

    await store.reset_room(room_id)

    assert await store.get_run(turn_id) is None
    assert await turn_store.get(turn_id) is not None
    async with db_session.begin():
        run_count = await db_session.scalar(
            select(func.count()).select_from(MemoryProjectionRunRecord)
        )
    assert run_count == 0


@pytest.mark.asyncio
async def test_sql_supersede_and_room_reset_keep_rebuild_consistent(
    db_session: AsyncSession,
    turn_store_factory,
    memory_store_factory,
) -> None:
    """同房间 supersede 必须成立，重建清理不能受自引用关系阻塞。"""

    turn_store: SqlAlchemyTurnStore = turn_store_factory()
    store: SqlAlchemyMemoryStore = memory_store_factory()
    room_id, player_id, first_turn_id = await _room_player_turn(db_session, turn_store)
    first_run, _ = await store.create_or_get_run(
        new_memory_projection_run(
            turn_id=first_turn_id,
            room_id=room_id,
            source_fingerprint="d" * 64,
            now=NOW,
        )
    )
    first_claim = await store.claim_run(
        turn_id=first_run.turn_id,
        worker_id="worker-a",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    previous = _entry(
        room_id=room_id,
        turn_id=first_turn_id,
        ordinal=0,
    )
    await store.complete_run(
        turn_id=first_turn_id,
        worker_id="worker-a",
        expected_version=first_claim.version,
        entries=(previous,),
        supersessions=(),
        now=NOW + timedelta(seconds=1),
    )

    second_turn, _ = await turn_store.create_or_get(
        new_turn_record(
            TurnInputSnapshot(
                room_id=room_id,
                player_id=player_id,
                actor_id="actor-1",
                client_action_id=f"action-{uuid4()}",
                utterance="再次询问守墓人",
            ),
            now=NOW + timedelta(minutes=1),
        )
    )
    second_planning = transition_turn(
        second_turn,
        status=TurnStatus.PLANNING,
        resume_point=TurnResumePoint.PLANNING,
        recovery_action=TurnRecoveryAction.WAIT,
    )
    await turn_store.compare_and_swap(
        expected_phase_version=second_turn.phase_version,
        updated=second_planning,
    )
    second_failed = transition_turn(
        second_planning,
        status=TurnStatus.FAILED,
        resume_point=TurnResumePoint.NONE,
        recovery_action=TurnRecoveryAction.SUBMIT_NEW_INPUT,
    )
    await turn_store.compare_and_swap(
        expected_phase_version=second_planning.phase_version,
        updated=second_failed,
    )
    second_run, _ = await store.create_or_get_run(
        new_memory_projection_run(
            turn_id=second_failed.turn_id,
            room_id=room_id,
            source_fingerprint="e" * 64,
            now=NOW + timedelta(minutes=1),
        )
    )
    second_claim = await store.claim_run(
        turn_id=second_run.turn_id,
        worker_id="worker-b",
        now=NOW + timedelta(minutes=1),
        lease_expires_at=NOW + timedelta(minutes=1, seconds=30),
    )
    replacement = _entry(
        room_id=room_id,
        turn_id=second_failed.turn_id,
        ordinal=0,
    )
    await store.complete_run(
        turn_id=second_failed.turn_id,
        worker_id="worker-b",
        expected_version=second_claim.version,
        entries=(replacement,),
        supersessions=((previous.memory_id, replacement.memory_id),),
        now=NOW + timedelta(minutes=1, seconds=1),
    )

    async with db_session.begin():
        persisted_previous = await db_session.get(MemoryEntryRecord, previous.memory_id)
        assert persisted_previous is not None
        assert persisted_previous.superseded_by == replacement.memory_id

    run_room_fk = next(
        foreign_key
        for foreign_key in MemoryProjectionRunRecord.__table__.foreign_keys
        if foreign_key.target_fullname == "rooms.id"
    )
    entry_room_fk = next(
        foreign_key
        for foreign_key in MemoryEntryRecord.__table__.foreign_keys
        if foreign_key.target_fullname == "rooms.id"
    )
    supersede_fk = next(
        foreign_key.constraint
        for foreign_key in MemoryEntryRecord.__table__.foreign_keys
        if foreign_key.parent.name == "superseded_by"
    )
    assert run_room_fk.ondelete == "CASCADE"
    assert entry_room_fk.ondelete == "CASCADE"
    assert supersede_fk is not None
    assert supersede_fk.deferrable is True
    assert supersede_fk.initially == "DEFERRED"

    await store.reset_room(room_id)

    async with db_session.begin():
        entry_count = await db_session.scalar(select(func.count()).select_from(MemoryEntryRecord))
        run_count = await db_session.scalar(
            select(func.count()).select_from(MemoryProjectionRunRecord)
        )
    assert entry_count == 0
    assert run_count == 0
