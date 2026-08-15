"""可靠回合 SQLAlchemy Store 的数据库约束与恢复语义测试。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import SqlAlchemyTurnStore
from app.core.turn_runtime import (
    NarrationOutboxMessage,
    TurnCommitReceipt,
    TurnConflictError,
    TurnContractError,
    TurnErrorStage,
    TurnFailureSnapshot,
    TurnInputSnapshot,
    TurnRecoveryAction,
    TurnResumePoint,
    TurnStatus,
    new_turn_record,
    transition_turn,
)
from app.models.room import Player, Room


async def _room_player(db_session: AsyncSession) -> tuple[str, str]:
    """创建满足外键约束的房间与玩家。"""

    room_id = str(uuid4())
    player_id = str(uuid4())
    db_session.add(
        Room(
            id=room_id,
            room_code=uuid4().hex[:6].upper(),
            room_name="可靠回合测试",
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
    return room_id, player_id


def _turn(room_id: str, player_id: str, action_id: str = "action-1", text: str = "调查书桌"):
    """生成使用真实 UUID 外键的初始回合。"""

    return new_turn_record(
        TurnInputSnapshot(
            room_id=room_id,
            player_id=player_id,
            actor_id="investigator-1",
            client_action_id=action_id,
            utterance=text,
        ),
        now=datetime(2026, 8, 15, 8, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_create_or_get_is_idempotent_and_rejects_changed_input(
    db_session: AsyncSession,
    turn_store_factory,
) -> None:
    room_id, player_id = await _room_player(db_session)
    store: SqlAlchemyTurnStore = turn_store_factory()
    proposed = _turn(room_id, player_id)

    created, was_created = await store.create_or_get(proposed)
    retried, retry_created = await store.create_or_get(_turn(room_id, player_id))

    assert was_created is True
    assert retry_created is False
    assert retried.turn_id == created.turn_id
    with pytest.raises(TurnConflictError, match="幂等键") as conflict:
        await store.create_or_get(_turn(room_id, player_id, text="打开房门"))
    assert conflict.value.code == "TURN_IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_room_reservation_survives_wait_and_releases_only_at_terminal(
    db_session: AsyncSession,
    turn_store_factory,
) -> None:
    room_id, player_id = await _room_player(db_session)
    store: SqlAlchemyTurnStore = turn_store_factory()
    current, _ = await store.create_or_get(_turn(room_id, player_id))
    waiting = transition_turn(
        current,
        status=TurnStatus.PLANNING,
        resume_point=TurnResumePoint.PLANNING,
        recovery_action=TurnRecoveryAction.WAIT,
    )
    await store.compare_and_swap(expected_phase_version=current.phase_version, updated=waiting)

    with pytest.raises(TurnConflictError) as busy:
        await store.create_or_get(_turn(room_id, player_id, action_id="action-2"))
    assert busy.value.code == "TURN_IN_PROGRESS"

    failed = transition_turn(
        waiting,
        status=TurnStatus.FAILED,
        resume_point=TurnResumePoint.NONE,
        recovery_action=TurnRecoveryAction.SUBMIT_NEW_INPUT,
    )
    await store.compare_and_swap(expected_phase_version=waiting.phase_version, updated=failed)
    replacement, was_created = await store.create_or_get(
        _turn(room_id, player_id, action_id="action-2")
    )
    assert was_created is True
    assert replacement.client_action_id == "action-2"


@pytest.mark.asyncio
async def test_retryable_failure_is_recoverable_from_sql_store(
    db_session: AsyncSession,
    turn_store_factory,
) -> None:
    """数据库恢复扫描要保留可重试失败，同时过滤不可重试失败。"""

    room_id, player_id = await _room_player(db_session)
    store: SqlAlchemyTurnStore = turn_store_factory()
    current, _ = await store.create_or_get(_turn(room_id, player_id))
    retryable = transition_turn(
        current,
        status=TurnStatus.PLANNING,
        resume_point=TurnResumePoint.PLANNING,
        recovery_action=TurnRecoveryAction.RETRY_SAME_INPUT,
        last_error=TurnFailureSnapshot(
            code="RULE_ENGINE_UNAVAILABLE",
            stage=TurnErrorStage.EXECUTION,
            retryable=True,
            commit_state=current.commit_state,
            recovery_action=TurnRecoveryAction.RETRY_SAME_INPUT,
            public_message="规则引擎暂时不可用",
            occurred_at=current.updated_at,
        ),
    )
    await store.compare_and_swap(
        expected_phase_version=current.phase_version,
        updated=retryable,
    )

    recoverable = await store.list_recoverable_turns(
        now=retryable.updated_at + timedelta(minutes=2),
        limit=10,
    )
    assert [item.turn_id for item in recoverable] == [retryable.turn_id]

    # 类型检查器无法从 transition_turn 推断错误快照仍然存在，先固定该不变量。
    assert retryable.last_error is not None
    non_retryable = transition_turn(
        retryable,
        status=TurnStatus.PLANNING,
        resume_point=TurnResumePoint.PLANNING,
        recovery_action=TurnRecoveryAction.SUBMIT_NEW_INPUT,
        last_error=retryable.last_error.model_copy(
            update={"retryable": False, "recovery_action": TurnRecoveryAction.SUBMIT_NEW_INPUT}
        ),
    )
    await store.compare_and_swap(
        expected_phase_version=retryable.phase_version,
        updated=non_retryable,
    )
    assert (
        await store.list_recoverable_turns(
            now=non_retryable.updated_at + timedelta(minutes=2),
            limit=10,
        )
        == ()
    )


@pytest.mark.asyncio
async def test_cas_and_worker_lease_reject_stale_writers(
    db_session: AsyncSession,
    turn_store_factory,
) -> None:
    room_id, player_id = await _room_player(db_session)
    store: SqlAlchemyTurnStore = turn_store_factory()
    current, _ = await store.create_or_get(_turn(room_id, player_id))
    now = datetime(2026, 8, 15, 8, 1, tzinfo=UTC)
    claimed = await store.claim(
        turn_id=current.turn_id,
        worker_id="worker-a",
        now=now,
        lease_expires_at=now + timedelta(seconds=30),
    )

    with pytest.raises(TurnConflictError) as busy:
        await store.claim(
            turn_id=current.turn_id,
            worker_id="worker-b",
            now=now + timedelta(seconds=10),
            lease_expires_at=now + timedelta(seconds=40),
        )
    assert busy.value.code == "TURN_WORKER_BUSY"

    taken_over = await store.claim(
        turn_id=current.turn_id,
        worker_id="worker-b",
        now=now + timedelta(seconds=31),
        lease_expires_at=now + timedelta(seconds=61),
    )
    assert taken_over.lease_owner == "worker-b"
    with pytest.raises(TurnConflictError) as stale:
        await store.release_claim(
            turn_id=current.turn_id,
            worker_id="worker-a",
            expected_phase_version=claimed.phase_version,
            now=now + timedelta(seconds=32),
        )
    assert stale.value.code == "TURN_VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_database_cas_rejects_forged_phase_jump(
    db_session: AsyncSession,
    turn_store_factory,
) -> None:
    room_id, player_id = await _room_player(db_session)
    store: SqlAlchemyTurnStore = turn_store_factory()
    current, _ = await store.create_or_get(_turn(room_id, player_id))
    forged = current.model_copy(
        update={
            "status": TurnStatus.EXECUTING,
            "phase_version": current.phase_version + 1,
            "resume_point": TurnResumePoint.EXECUTING,
            "updated_at": datetime.now(UTC),
        },
        deep=True,
    )

    with pytest.raises(TurnContractError, match="非法回合状态转换"):
        await store.compare_and_swap(
            expected_phase_version=current.phase_version,
            updated=forged,
        )


@pytest.mark.asyncio
async def test_receipt_accepts_no_event_command_and_is_idempotent(
    db_session: AsyncSession,
    turn_store_factory,
) -> None:
    room_id, player_id = await _room_player(db_session)
    store: SqlAlchemyTurnStore = turn_store_factory()
    turn, _ = await store.create_or_get(_turn(room_id, player_id))
    receipt = TurnCommitReceipt(
        turn_id=turn.turn_id,
        room_id=room_id,
        engine_request_id="engine-1",
        action_request_id="action-1/step-0",
        committed_state_version=3,
        created_at=datetime(2026, 8, 15, 8, 2, tzinfo=UTC),
    )

    assert await store.append_receipt(receipt) == receipt
    assert await store.append_receipt(receipt) == receipt
    assert await store.get_receipt(room_id, "engine-1") == receipt

    changed = receipt.model_copy(update={"committed_state_version": 4})
    with pytest.raises(TurnConflictError) as conflict:
        await store.append_receipt(changed)
    assert conflict.value.code == "TURN_RECEIPT_CONFLICT"


@pytest.mark.asyncio
async def test_outbox_keeps_one_stable_final_message_per_turn(
    db_session: AsyncSession,
    turn_store_factory,
) -> None:
    room_id, player_id = await _room_player(db_session)
    store: SqlAlchemyTurnStore = turn_store_factory()
    turn, _ = await store.create_or_get(_turn(room_id, player_id))
    now = datetime(2026, 8, 15, 8, 3, tzinfo=UTC)
    message = NarrationOutboxMessage(
        outbox_id=str(uuid4()),
        turn_id=turn.turn_id,
        room_id=room_id,
        player_id=player_id,
        message_id="turn-result-1",
        visibility="player_scoped",
        payload={"text": "你在抽屉里发现了一把钥匙。"},
        next_attempt_at=now,
        created_at=now,
        updated_at=now,
    )

    saved, was_created = await store.put_outbox(message)
    retried, retry_created = await store.put_outbox(message)
    assert was_created is True
    assert retry_created is False
    assert saved == retried == message

    changed = message.model_copy(update={"payload": {"text": "不同叙事"}})
    with pytest.raises(TurnConflictError) as conflict:
        await store.put_outbox(changed)
    assert conflict.value.code == "TURN_OUTBOX_CONFLICT"
