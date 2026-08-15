"""验证 Narration Outbox 的固定发送顺序、作用域与有限重试。"""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import SqlAlchemyTurnStore
from app.core.turn_coordinator import TurnCoordinator, TurnExecutionOutcome
from app.core.turn_runtime import (
    InMemoryTurnStore,
    TurnCommitReceipt,
    TurnInputSnapshot,
    TurnOutboxStatus,
)
from app.models.event import Event
from app.models.turn import NarrationOutboxRecord, TurnRecordModel
from app.service.turn_outbox import TurnOutboxDispatcher
from app.service.ws_manager import ConnectionManager
from tests.test_engine_runtime import _start_room


class _Socket:
    """记录测试帧的最小 WebSocket 替身。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.frames: list[dict] = []
        self.fail = fail

    async def send_json(self, message: dict) -> None:
        if self.fail:
            raise RuntimeError("socket closed")
        self.frames.append(message)


async def _completed_turn(
    store: InMemoryTurnStore,
    *,
    visibility: str = "public",
):
    coordinator = TurnCoordinator(store, worker_id="turn-worker")

    async def execute(on_phase):  # noqa: ANN001
        await on_phase("generating_narration")
        return TurnExecutionOutcome(
            status="completed",
            player_view={"scene": {"id": "study"}, "revision": "0"},
            view_revision="0",
            scene_id="study",
            narration={"kind": "narration", "text": "第一句。第二句。"},
            visibility=visibility,
        )

    return await coordinator.start(
        TurnInputSnapshot(
            room_id="room-1",
            player_id="player-1",
            actor_id="actor-1",
            client_action_id="action-1",
            utterance="观察房间",
        ),
        executor=execute,
    )


@pytest.mark.asyncio
async def test_outbox_sends_authoritative_frames_in_fixed_order() -> None:
    store = InMemoryTurnStore()
    turn = await _completed_turn(store)
    manager = ConnectionManager()
    socket = _Socket()
    manager.add("room-1", socket, "player-1")
    dispatcher = TurnOutboxDispatcher(store, manager, worker_id="outbox-worker")

    assert await dispatcher.dispatch_due() == 1

    types = [frame.get("type") or frame.get("message_type") for frame in socket.frames]
    assert types[-3:] == ["narration.push", "view.updated", "turn.completed"]
    assert all(item == "narration.chunk" for item in types[:-3])
    assert socket.frames[-1]["turn_id"] == turn.turn_id
    outbox = await store.get_outbox(turn.turn_id)
    assert outbox is not None
    assert outbox.status == TurnOutboxStatus.DISPATCHED
    assert outbox.attempt_count == 1


@pytest.mark.asyncio
async def test_no_online_recipient_does_not_consume_failure_budget() -> None:
    store = InMemoryTurnStore()
    turn = await _completed_turn(store)
    dispatcher = TurnOutboxDispatcher(
        store,
        ConnectionManager(),
        worker_id="outbox-worker",
        retry_seconds=0,
    )

    assert await dispatcher.dispatch_due() == 1
    outbox = await store.get_outbox(turn.turn_id)
    assert outbox is not None
    assert outbox.status == TurnOutboxStatus.PENDING
    assert outbox.attempt_count == 0


@pytest.mark.asyncio
async def test_player_scoped_outbox_never_reaches_other_player() -> None:
    store = InMemoryTurnStore()
    turn = await _completed_turn(store, visibility="player_scoped")
    manager = ConnectionManager()
    owner = _Socket()
    other = _Socket()
    manager.add("room-1", owner, "player-1")
    manager.add("room-1", other, "player-2")
    dispatcher = TurnOutboxDispatcher(store, manager, worker_id="outbox-worker")

    await dispatcher.dispatch_due()

    assert owner.frames
    assert other.frames == []
    outbox = await store.get_outbox(turn.turn_id)
    assert outbox is not None
    assert outbox.status == TurnOutboxStatus.DISPATCHED


@pytest.mark.asyncio
async def test_real_send_failure_enters_dead_letter_after_five_attempts() -> None:
    store = InMemoryTurnStore()
    turn = await _completed_turn(store)
    manager = ConnectionManager()
    manager.add("room-1", _Socket(fail=True), "player-1")
    dispatcher = TurnOutboxDispatcher(
        store,
        manager,
        worker_id="outbox-worker",
        retry_seconds=0,
    )

    for _ in range(5):
        await dispatcher.dispatch_due()

    outbox = await store.get_outbox(turn.turn_id)
    assert outbox is not None
    assert outbox.status == TurnOutboxStatus.DEAD_LETTER
    assert outbox.attempt_count == 5
    assert outbox.last_error_code == "WS_SEND_FAILED"


async def test_sql_publish_atomically_persists_result_outbox_and_replay(
    db_session: AsyncSession,
    turn_store_factory: Callable[[], SqlAlchemyTurnStore],
) -> None:
    """SQL 适配器必须把最终结果、Outbox 和玩家安全回放写进同一事务。"""

    room, players, _ = await _start_room(db_session, room_number=125)
    store = turn_store_factory()
    coordinator = TurnCoordinator(store, worker_id="sql-outbox-turn")

    async def execute(on_phase):  # noqa: ANN001
        await on_phase("executing_action")
        turn = await store.get_by_client_action(room.id, "sql-outbox-125")
        assert turn is not None
        await store.append_receipt(
            TurnCommitReceipt(
                turn_id=turn.turn_id,
                room_id=room.id,
                engine_request_id="sql-engine-125",
                action_request_id="sql-outbox-125",
                committed_state_version=0,
                created_at=datetime.now(UTC),
            )
        )
        await on_phase("generating_narration")
        return TurnExecutionOutcome(
            status="completed",
            player_view={"scene": {"id": "cemetery"}, "revision": "0"},
            view_revision="0",
            scene_id="cemetery",
            narration={"kind": "narration", "text": "持久化叙事。"},
        )

    completed = await coordinator.start(
        TurnInputSnapshot(
            room_id=room.id,
            player_id=players[0].id,
            actor_id="actor_1",
            client_action_id="sql-outbox-125",
            utterance="观察持久化边界",
        ),
        executor=execute,
    )

    db_session.expire_all()
    turn_record = await db_session.get(TurnRecordModel, completed.turn_id)
    outboxes = (
        await db_session.scalars(
            select(NarrationOutboxRecord).where(NarrationOutboxRecord.turn_id == completed.turn_id)
        )
    ).all()
    replay = (
        await db_session.scalars(select(Event).where(Event.turn_id == completed.turn_id))
    ).all()
    assert turn_record is not None
    assert turn_record.status == "completed"
    assert turn_record.result_json is not None
    assert len(outboxes) == 1
    assert outboxes[0].message_id == completed.turn_id
    assert len(replay) == 1
    assert replay[0].payload == {"messageId": completed.turn_id, "text": "持久化叙事。"}
