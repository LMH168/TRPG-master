"""验证 Turn Coordinator 的幂等、receipt 对账与叙事发布边界。"""

from datetime import UTC, datetime, timedelta

import pytest
from collaboration_framework.engine import current_turn_id

from app.core.turn_coordinator import TurnCoordinator, TurnExecutionOutcome
from app.core.turn_runtime import (
    InMemoryTurnStore,
    TurnCommitReceipt,
    TurnCommitState,
    TurnInputSnapshot,
    TurnRecoveryAction,
    TurnResumePoint,
    TurnStatus,
    TurnWaitingReason,
    new_turn_record,
)
from app.service.reliable_turn_runtime import TurnRuntimeSupervisor


def _request(client_action_id: str = "action-1") -> TurnInputSnapshot:
    return TurnInputSnapshot(
        room_id="room-1",
        player_id="player-1",
        actor_id="actor-1",
        client_action_id=client_action_id,
        utterance="检查书桌",
    )


def _outcome(*, waiting: TurnWaitingReason = TurnWaitingReason.NONE) -> TurnExecutionOutcome:
    return TurnExecutionOutcome(
        status="waiting_for_player" if waiting != TurnWaitingReason.NONE else "completed",
        player_view={"scene": {"id": "study"}, "revision": "1"},
        view_revision="1",
        scene_id="study",
        narration=None
        if waiting != TurnWaitingReason.NONE
        else {"kind": "narration", "text": "你发现了线索。"},
        waiting_reason=waiting,
        pending_decision={"decision_id": "choice-1", "options": [{"id": "spot-hidden"}]}
        if waiting != TurnWaitingReason.NONE
        else None,
    )


@pytest.mark.asyncio
async def test_completed_retry_reuses_turn_without_reexecuting() -> None:
    store = InMemoryTurnStore()
    coordinator = TurnCoordinator(store, worker_id="worker-1")
    executions = 0

    async def execute(on_phase):  # noqa: ANN001
        nonlocal executions
        executions += 1
        await on_phase("executing_action")
        turn_id = current_turn_id()
        assert turn_id is not None
        await store.append_receipt(
            TurnCommitReceipt(
                turn_id=turn_id,
                room_id="room-1",
                engine_request_id="engine-1",
                action_request_id="action-1",
                committed_state_version=1,
                created_at=datetime.now(UTC),
            )
        )
        await on_phase("generating_narration")
        return _outcome()

    first = await coordinator.start(_request(), executor=execute)
    second = await coordinator.start(_request(), executor=execute)

    assert first.turn_id == second.turn_id
    assert second.status == TurnStatus.COMPLETED
    assert second.commit_state == TurnCommitState.COMMITTED
    assert executions == 1
    outbox = await store.get_outbox(first.turn_id)
    assert outbox is not None
    assert outbox.message_id == first.turn_id


@pytest.mark.asyncio
async def test_waiting_turn_keeps_room_reservation_and_recovery_action() -> None:
    store = InMemoryTurnStore()
    coordinator = TurnCoordinator(store, worker_id="worker-1")

    async def execute(on_phase):  # noqa: ANN001
        await on_phase("executing_action")
        return _outcome(waiting=TurnWaitingReason.SKILL_CHOICE)

    waiting = await coordinator.start(_request(), executor=execute)

    assert waiting.status == TurnStatus.ADJUDICATING
    assert waiting.waiting_reason == TurnWaitingReason.SKILL_CHOICE
    assert waiting.recovery_action == TurnRecoveryAction.CHOOSE_SKILL
    assert waiting.pending_decision == {
        "decision_id": "choice-1",
        "options": [{"id": "spot-hidden"}],
    }
    assert waiting.lease_owner is None
    with pytest.raises(Exception, match="未完成回合"):
        await coordinator.start(_request("action-2"), executor=execute)


@pytest.mark.asyncio
async def test_cancel_after_partial_commit_keeps_partial_state_during_narration() -> None:
    """取消剩余计划时不能先把部分提交提升为完整提交再非法降级。"""

    store = InMemoryTurnStore()
    coordinator = TurnCoordinator(store, worker_id="worker-1")

    async def wait_for_choice(on_phase):  # noqa: ANN001
        await on_phase("executing_action")
        turn_id = current_turn_id()
        assert turn_id is not None
        await store.append_receipt(
            TurnCommitReceipt(
                turn_id=turn_id,
                room_id="room-1",
                engine_request_id="travel-engine-1",
                action_request_id="travel-step-1",
                committed_state_version=1,
                created_at=datetime.now(UTC),
            )
        )
        return _outcome(waiting=TurnWaitingReason.SKILL_CHOICE)

    waiting = await coordinator.start(_request(), executor=wait_for_choice)
    assert waiting.commit_state == TurnCommitState.PARTIALLY_COMMITTED

    async def cancel_remaining(on_phase):  # noqa: ANN001
        await on_phase("executing_action")
        await on_phase("generating_narration")
        return TurnExecutionOutcome(
            status="cancelled",
            player_view={"scene": {"id": "study"}, "revision": "2"},
            view_revision="2",
            scene_id="study",
            narration={"kind": "narration", "text": "后续行动已取消。"},
        )

    cancelled = await coordinator.resume(waiting.turn_id, executor=cancel_remaining)

    assert cancelled.status == TurnStatus.COMPLETED
    assert cancelled.commit_state == TurnCommitState.PARTIALLY_COMMITTED
    assert cancelled.last_error is None


@pytest.mark.asyncio
async def test_narrator_failure_resumes_without_second_engine_receipt() -> None:
    store = InMemoryTurnStore()
    coordinator = TurnCoordinator(store, worker_id="worker-1")
    attempts = 0

    async def execute(on_phase):  # noqa: ANN001
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await on_phase("executing_action")
            turn_id = current_turn_id()
            assert turn_id is not None
            await store.append_receipt(
                TurnCommitReceipt(
                    turn_id=turn_id,
                    room_id="room-1",
                    engine_request_id="engine-1",
                    action_request_id="action-1",
                    committed_state_version=1,
                    created_at=datetime.now(UTC),
                )
            )
        await on_phase("generating_narration")
        if attempts == 1:
            raise TimeoutError("narrator unavailable")
        return _outcome()

    failed = await coordinator.start(_request(), executor=execute)
    assert failed.status == TurnStatus.AWAITING_NARRATION
    assert failed.commit_state == TurnCommitState.COMMITTED
    assert failed.last_error is not None

    completed = await coordinator.resume(failed.turn_id, executor=execute)
    assert completed.status == TurnStatus.COMPLETED
    assert len(await store.list_receipts(failed.turn_id)) == 1


@pytest.mark.asyncio
async def test_retryable_rule_failure_remains_recoverable_after_restart() -> None:
    """规则引擎暂时失败后，恢复扫描不能把房间永久锁死。"""

    store = InMemoryTurnStore()
    coordinator = TurnCoordinator(store, worker_id="worker-1")
    attempts = 0

    async def execute(on_phase):  # noqa: ANN001
        nonlocal attempts
        attempts += 1
        await on_phase("executing_action")
        if attempts == 1:
            raise RuntimeError("temporary rule engine failure")
        return _outcome()

    failed = await coordinator.start(_request(), executor=execute)
    assert failed.status == TurnStatus.EXECUTING
    assert failed.last_error is not None
    assert failed.last_error.retryable is True
    assert failed.resume_point == TurnResumePoint.EXECUTING
    assert failed.lease_owner is None

    recoverable = await store.list_recoverable_turns(
        now=failed.updated_at + timedelta(minutes=2),
        limit=10,
    )
    assert [item.turn_id for item in recoverable] == [failed.turn_id]

    recovered = await coordinator.resume(failed.turn_id, executor=execute)
    assert recovered.status == TurnStatus.COMPLETED
    assert attempts == 2

    # 终态转换必须删除 reservation，后续新输入不应继续得到 ACTION_IN_PROGRESS。
    async def execute_replacement(_on_phase):  # noqa: ANN001
        return _outcome()

    replacement = await coordinator.start(
        _request("action-2"),
        executor=execute_replacement,
    )
    assert replacement.client_action_id == "action-2"


@pytest.mark.asyncio
async def test_partial_execution_failure_releases_room_for_new_action() -> None:
    """复合计划部分提交后失败，保留 receipt 但不得阻塞玩家的新行动。"""

    store = InMemoryTurnStore()
    coordinator = TurnCoordinator(store, worker_id="worker-1")

    async def execute_partial(on_phase):  # noqa: ANN001
        await on_phase("executing_action")
        turn_id = current_turn_id()
        assert turn_id is not None
        await store.append_receipt(
            TurnCommitReceipt(
                turn_id=turn_id,
                room_id="room-1",
                engine_request_id="engine-partial-1",
                action_request_id="step-partial-1",
                committed_state_version=1,
                created_at=datetime.now(UTC),
            )
        )
        raise RuntimeError("second step failed")

    failed = await coordinator.start(_request(), executor=execute_partial)

    assert failed.status == TurnStatus.FAILED
    assert failed.commit_state == TurnCommitState.PARTIALLY_COMMITTED
    assert failed.last_error is not None
    assert failed.last_error.retryable is False
    assert failed.last_error.recovery_action == TurnRecoveryAction.SUBMIT_NEW_INPUT
    assert "已完成的步骤已经保存" in failed.last_error.public_message

    async def execute_replacement(_on_phase):  # noqa: ANN001
        return _outcome()

    replacement = await coordinator.start(
        _request("action-after-partial"),
        executor=execute_replacement,
    )
    assert replacement.status == TurnStatus.COMPLETED


@pytest.mark.asyncio
async def test_repeated_narration_failure_releases_room_after_retry_budget() -> None:
    """叙事服务持续失败时，不能无限保留房间 reservation。"""

    store = InMemoryTurnStore()
    coordinator = TurnCoordinator(store, worker_id="worker-1")

    async def execute(on_phase):  # noqa: ANN001
        await on_phase("executing_action")
        turn_id = current_turn_id()
        assert turn_id is not None
        await store.append_receipt(
            TurnCommitReceipt(
                turn_id=turn_id,
                room_id="room-1",
                engine_request_id="engine-narration-budget",
                action_request_id="action-1",
                committed_state_version=1,
                created_at=datetime.now(UTC),
            )
        )
        await on_phase("generating_narration")
        raise TimeoutError("narrator unavailable")

    current = await coordinator.start(_request(), executor=execute)
    assert current.status == TurnStatus.AWAITING_NARRATION
    assert current.last_error is not None
    assert current.last_error.attempt_count == 1

    current = await coordinator.resume(current.turn_id, executor=execute)
    assert current.status == TurnStatus.AWAITING_NARRATION
    assert current.last_error is not None
    assert current.last_error.attempt_count == 2

    current = await coordinator.resume(current.turn_id, executor=execute)
    assert current.status == TurnStatus.FAILED
    assert current.last_error is not None
    assert current.last_error.retryable is False
    assert current.last_error.attempt_count == 3

    async def execute_replacement(_on_phase):  # noqa: ANN001
        return _outcome()

    replacement = await coordinator.start(
        _request("action-after-narration-failure"),
        executor=execute_replacement,
    )
    assert replacement.client_action_id == "action-after-narration-failure"


@pytest.mark.asyncio
async def test_supervisor_recovers_only_stale_safe_turns_before_outbox() -> None:
    """启动扫描不能抢正在创建的回合，也不能替玩家或模型失败回合做决定。"""

    store = InMemoryTurnStore()
    now = datetime.now(UTC)
    stale = new_turn_record(_request("stale-action"), now=now - timedelta(minutes=2))
    await store.create_or_get(stale)
    # 每个房间只能有一个活动回合，第二个请求放到另一个房间。
    fresh_request = _request("fresh-action").model_copy(update={"room_id": "room-2"})
    fresh = new_turn_record(fresh_request, now=now)
    await store.create_or_get(fresh)
    recovered: list[str] = []

    class Dispatcher:
        calls = 0

        async def dispatch_due(self, *, limit: int = 20) -> int:
            self.calls += 1
            return 0

    dispatcher = Dispatcher()

    async def resume(turn_id: str):
        recovered.append(turn_id)
        record = await store.get(turn_id)
        assert record is not None
        return record

    supervisor = TurnRuntimeSupervisor(
        store=store,
        dispatcher=dispatcher,
        resume=resume,
    )
    await supervisor.run_once()

    assert recovered == [stale.turn_id]
    assert dispatcher.calls == 1
