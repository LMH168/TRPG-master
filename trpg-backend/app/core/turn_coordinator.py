"""可靠回合协调器：推进状态、对账 receipt，并原子发布最终叙事。

本层不解释 Host 或 Engine 的领域语义。调用方把现有 ActionPlan 执行链适配成
``TurnExecutor``；协调器只负责持久身份、恢复阶段、提交证明和发布事务。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from collaboration_framework.engine import engine_turn_context

from app.core.turn_events import TurnPhase
from app.core.turn_runtime import (
    NarrationOutboxMessage,
    TurnCommitState,
    TurnErrorStage,
    TurnFailureSnapshot,
    TurnInputSnapshot,
    TurnRecord,
    TurnRecoveryAction,
    TurnReplayEvent,
    TurnResultSnapshot,
    TurnResumePoint,
    TurnRuntimeStore,
    TurnStatus,
    TurnWaitingReason,
    new_turn_record,
    transition_turn,
)

TurnPhaseObserver = Callable[[TurnPhase], Awaitable[None]]

# Narrator 或模型持续失败时最多自动恢复三次；耗尽后结束当前 Turn，避免一个
# 永远无法生成叙事的回合永久占用房间。Engine receipt 已存在时只恢复叙事，绝不重做提交。
MAX_TURN_RECOVERY_ATTEMPTS = 3


@dataclass(frozen=True)
class TurnExecutionOutcome:
    """现有 ActionPlan 执行链完成一次推进后的玩家安全结果。"""

    status: str
    player_view: dict
    view_revision: str
    scene_id: str
    narration: dict | None = None
    waiting_reason: TurnWaitingReason = TurnWaitingReason.NONE
    pending_decision: dict | None = None
    visibility: str = "public"

    @property
    def waiting_for_player(self) -> bool:
        return self.waiting_reason != TurnWaitingReason.NONE


class TurnExecutor(Protocol):
    """在一个已绑定 turn_id 的任务内推进现有 Host/ActionPlan 链。"""

    async def __call__(self, on_phase: TurnPhaseObserver, /) -> TurnExecutionOutcome: ...


class TurnAfterPublish(Protocol):
    """Outbox 落库后收束 ActionPlan 步骤级叙事状态的回调。"""

    async def __call__(self) -> None: ...


class TurnCoordinator:
    """统一接收、恢复和发布一个玩家动作。"""

    def __init__(
        self,
        store: TurnRuntimeStore,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 60,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("Turn lease_seconds 必须大于 0")
        self._store = store
        self._worker_id = worker_id or f"turn-worker-{uuid4().hex}"
        self._lease_seconds = lease_seconds

    async def start(
        self,
        request: TurnInputSnapshot,
        *,
        executor: TurnExecutor,
        after_publish: TurnAfterPublish | None = None,
    ) -> TurnRecord:
        """幂等创建或找到 Turn，并从唯一持久恢复点推进。"""

        proposed = new_turn_record(request)
        turn, _ = await self._store.create_or_get(proposed)
        if turn.is_terminal:
            return turn
        return await self._advance(turn, executor=executor, after_publish=after_publish)

    async def resume(
        self,
        turn_id: str,
        *,
        executor: TurnExecutor,
        after_publish: TurnAfterPublish | None = None,
    ) -> TurnRecord:
        """按持久化 TurnRecord 恢复，绝不根据当前 WebSocket 状态猜测阶段。"""

        turn = await self._store.get(turn_id)
        if turn is None:
            raise LookupError("TurnRecord 不存在")
        if turn.is_terminal:
            return turn
        return await self._advance(turn, executor=executor, after_publish=after_publish)

    async def _advance(
        self,
        turn: TurnRecord,
        *,
        executor: TurnExecutor,
        after_publish: TurnAfterPublish | None,
    ) -> TurnRecord:
        now = datetime.now(UTC)
        current = await self._store.claim(
            turn_id=turn.turn_id,
            worker_id=self._worker_id,
            now=now,
            lease_expires_at=now + timedelta(seconds=self._lease_seconds),
        )
        # 阶段推进可能会清理上一轮的错误投影；恢复次数必须在执行前锁存，
        # 否则每次 Narrator 重试都会被重新计为第一次失败。
        previous_failure_attempts = current.last_error.attempt_count if current.last_error else 0

        async def move(
            status: TurnStatus,
            *,
            resume_point: TurnResumePoint,
            recovery_action: TurnRecoveryAction,
            waiting_reason: TurnWaitingReason = TurnWaitingReason.NONE,
            pending_decision: dict | None = None,
            commit_state: TurnCommitState | None = None,
            last_error: TurnFailureSnapshot | None = None,
            result: TurnResultSnapshot | None = None,
        ) -> TurnRecord:
            nonlocal current
            updated = transition_turn(
                current,
                status=status,
                resume_point=resume_point,
                recovery_action=recovery_action,
                waiting_reason=waiting_reason,
                pending_decision=pending_decision,
                commit_state=commit_state,
                last_error=last_error,
                result=result,
            )
            current = await self._store.compare_and_swap(
                expected_phase_version=current.phase_version,
                updated=updated,
            )
            return current

        async def on_phase(phase: TurnPhase) -> None:
            """把旧执行链的细粒度进度折叠为单一回合状态机。"""

            if phase == "executing_action" and current.status in {
                TurnStatus.RECEIVED,
                TurnStatus.PLANNING,
                TurnStatus.ADJUDICATING,
            }:
                if current.status == TurnStatus.RECEIVED:
                    await move(
                        TurnStatus.PLANNING,
                        resume_point=TurnResumePoint.PLANNING,
                        recovery_action=TurnRecoveryAction.WAIT,
                    )
                if current.status == TurnStatus.PLANNING:
                    await move(
                        TurnStatus.ADJUDICATING,
                        resume_point=TurnResumePoint.ADJUDICATING,
                        recovery_action=TurnRecoveryAction.WAIT,
                    )
                await move(
                    TurnStatus.EXECUTING,
                    resume_point=TurnResumePoint.EXECUTING,
                    recovery_action=TurnRecoveryAction.WAIT,
                )
            elif phase == "generating_narration" and current.status in {
                TurnStatus.RECEIVED,
                TurnStatus.PLANNING,
                TurnStatus.ADJUDICATING,
                TurnStatus.EXECUTING,
                TurnStatus.AWAITING_NARRATION,
            }:
                if current.status == TurnStatus.RECEIVED:
                    await move(
                        TurnStatus.PLANNING,
                        resume_point=TurnResumePoint.PLANNING,
                        recovery_action=TurnRecoveryAction.WAIT,
                    )
                receipts = await self._store.list_receipts(current.turn_id)
                # 等待玩家时可能已有部分计划步骤提交。恢复后若玩家取消剩余步骤，
                # 这里只能保留 partially_committed；提前提升为 committed 会在根据
                # 最终取消结果对账时形成非法降级。尚未标记部分提交的普通回合则可
                # 由 receipt 确认规则结果已经完整落库。
                narration_commit_state = current.commit_state
                if receipts and narration_commit_state == TurnCommitState.NOT_COMMITTED:
                    narration_commit_state = TurnCommitState.COMMITTED
                await move(
                    TurnStatus.AWAITING_NARRATION,
                    resume_point=TurnResumePoint.NARRATING,
                    recovery_action=TurnRecoveryAction.WAIT,
                    commit_state=narration_commit_state,
                )

        if current.status == TurnStatus.RECEIVED:
            await move(
                TurnStatus.PLANNING,
                resume_point=TurnResumePoint.PLANNING,
                recovery_action=TurnRecoveryAction.WAIT,
            )
        try:
            if current.status == TurnStatus.DELIVERING and current.result is not None:
                # 发布事务已经成功时，恢复只能收束同一份持久化结果，绝不再调用
                # Narrator 或 Engine。Outbox worker 会独立完成至少一次投递。
                if after_publish is not None:
                    with engine_turn_context(current.turn_id):
                        await after_publish()
                await move(
                    TurnStatus.COMPLETED,
                    resume_point=TurnResumePoint.NONE,
                    recovery_action=TurnRecoveryAction.FETCH_RESULT,
                    commit_state=current.commit_state,
                    result=current.result,
                )
                return current
            with engine_turn_context(current.turn_id):
                outcome = await executor(on_phase)
            receipts = await self._store.list_receipts(current.turn_id)
            commit_state = self._commit_state(outcome, bool(receipts))
            if outcome.waiting_for_player:
                if current.status == TurnStatus.EXECUTING:
                    status = TurnStatus.ADJUDICATING
                else:
                    status = current.status
                recovery = (
                    TurnRecoveryAction.CHOOSE_SKILL
                    if outcome.waiting_reason == TurnWaitingReason.SKILL_CHOICE
                    else TurnRecoveryAction.CHOOSE_POST_ROLL
                )
                await move(
                    status,
                    resume_point=TurnResumePoint.AWAITING_PLAYER,
                    recovery_action=recovery,
                    waiting_reason=outcome.waiting_reason,
                    pending_decision=outcome.pending_decision,
                    commit_state=commit_state,
                )
                return await self._release(current)
            if outcome.narration is None:
                raise RuntimeError("已结算回合缺少最终 Narration")
            if current.status != TurnStatus.AWAITING_NARRATION:
                await on_phase("generating_narration")
            current = await self._publish(current, outcome, commit_state=commit_state)
            if after_publish is not None:
                with engine_turn_context(current.turn_id):
                    await after_publish()
            await move(
                TurnStatus.COMPLETED,
                resume_point=TurnResumePoint.NONE,
                recovery_action=TurnRecoveryAction.FETCH_RESULT,
                commit_state=commit_state,
                result=current.result,
            )
            return current
        except Exception as exc:
            return await self._record_failure(
                current,
                exc,
                previous_attempts=previous_failure_attempts,
            )

    async def _publish(
        self,
        current: TurnRecord,
        outcome: TurnExecutionOutcome,
        *,
        commit_state: TurnCommitState,
    ) -> TurnRecord:
        """构造稳定消息，并调用 Store 的单一叙事发布事务。"""

        now = datetime.now(UTC)
        message_id = current.turn_id
        narration = dict(outcome.narration or {})
        text = narration.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("最终 Narration 缺少文本")
        push_payload = {
            "turnId": current.turn_id,
            "clientActionId": current.client_action_id,
            "messageId": message_id,
            "text": text.strip(),
        }
        result = TurnResultSnapshot(
            message_id=message_id,
            narration=narration,
            player_view=outcome.player_view,
            view_revision=outcome.view_revision,
        )
        updated = transition_turn(
            current,
            status=TurnStatus.DELIVERING,
            resume_point=TurnResumePoint.DELIVERING,
            recovery_action=TurnRecoveryAction.FETCH_RESULT,
            commit_state=commit_state,
            result=result,
        )
        bundle = {
            "narration": push_payload,
            "completion": {
                "turnId": current.turn_id,
                "roomId": current.room_id,
                "playerId": current.player_id,
                "actorId": current.actor_id,
                "clientActionId": current.client_action_id,
                "narration": narration,
                "playerView": outcome.player_view,
            },
        }
        message = NarrationOutboxMessage(
            outbox_id=str(uuid4()),
            turn_id=current.turn_id,
            room_id=current.room_id,
            message_id=message_id,
            visibility=outcome.visibility,
            player_id=current.player_id,
            payload=bundle,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        event = TurnReplayEvent(
            event_id=str(uuid4()),
            turn_id=current.turn_id,
            room_id=current.room_id,
            player_id=current.player_id,
            correlation_id=current.client_action_id,
            visibility=outcome.visibility,
            actor_id=current.actor_id,
            scene_id=outcome.scene_id,
            view_revision=outcome.view_revision,
            payload=push_payload,
            created_at=now,
        )
        saved, _, _ = await self._store.publish_narration(
            expected_phase_version=current.phase_version,
            updated_turn=updated,
            message=message,
            replay_event=event,
        )
        return saved

    async def _record_failure(
        self,
        current: TurnRecord,
        exc: Exception,
        *,
        previous_attempts: int = 0,
    ) -> TurnRecord:
        """持久化脱敏错误；有限次恢复失败后终止回合并释放房间占用。"""

        receipts = await self._store.list_receipts(current.turn_id)
        commit_state = current.commit_state
        if receipts and commit_state == TurnCommitState.NOT_COMMITTED:
            commit_state = TurnCommitState.PARTIALLY_COMMITTED
        attempt_count = previous_attempts + 1
        retryable = bool(getattr(exc, "retryable", True)) and (
            attempt_count < MAX_TURN_RECOVERY_ATTEMPTS
        )
        stage, resume_point = self._error_location(current)
        failure = TurnFailureSnapshot(
            code=str(getattr(exc, "code", "TURN_INTERNAL_ERROR")),
            stage=stage,
            retryable=retryable,
            attempt_count=attempt_count,
            commit_state=commit_state,
            recovery_action=(
                TurnRecoveryAction.RETRY_SAME_INPUT
                if retryable
                else TurnRecoveryAction.SUBMIT_NEW_INPUT
            ),
            public_message=(
                "规则结果已保存，请稍后恢复本次回合"
                if receipts
                else "本次回合暂时未完成，请稍后使用原请求恢复"
            ),
            occurred_at=datetime.now(UTC),
        )
        status = current.status if retryable else TurnStatus.FAILED
        updated = transition_turn(
            current,
            status=status,
            resume_point=resume_point if retryable else TurnResumePoint.NONE,
            recovery_action=failure.recovery_action,
            commit_state=commit_state,
            last_error=failure,
        )
        saved = await self._store.compare_and_swap(
            expected_phase_version=current.phase_version,
            updated=updated,
        )
        return saved if saved.is_terminal else await self._release(saved)

    async def _release(self, current: TurnRecord) -> TurnRecord:
        """释放 worker lease，但保留活动回合的数据库房间占用。"""

        if current.lease_owner is None:
            return current
        return await self._store.release_claim(
            turn_id=current.turn_id,
            worker_id=self._worker_id,
            expected_phase_version=current.phase_version,
            now=datetime.now(UTC),
        )

    @staticmethod
    def _commit_state(
        outcome: TurnExecutionOutcome,
        has_receipt: bool,
    ) -> TurnCommitState:
        if not has_receipt:
            return TurnCommitState.NOT_COMMITTED
        if outcome.waiting_for_player or outcome.status in {
            "cancelled",
            "stopped",
            "needs_clarification",
        }:
            return TurnCommitState.PARTIALLY_COMMITTED
        return TurnCommitState.COMMITTED

    @staticmethod
    def _error_location(current: TurnRecord) -> tuple[TurnErrorStage, TurnResumePoint]:
        if current.status == TurnStatus.AWAITING_NARRATION:
            return TurnErrorStage.NARRATION, TurnResumePoint.NARRATING
        if current.status == TurnStatus.EXECUTING:
            return TurnErrorStage.EXECUTION, TurnResumePoint.EXECUTING
        if current.status == TurnStatus.ADJUDICATING:
            return TurnErrorStage.ADJUDICATION, TurnResumePoint.ADJUDICATING
        if current.status == TurnStatus.DELIVERING:
            return TurnErrorStage.DELIVERY, TurnResumePoint.DELIVERING
        return TurnErrorStage.PLANNING, TurnResumePoint.PLANNING


__all__ = ["TurnCoordinator", "TurnExecutionOutcome", "TurnExecutor"]
