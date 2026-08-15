"""组装唯一生产回合入口：Turn Coordinator、ActionPlan 与 Narration Outbox。

Controller 的所有玩家动作都经由本模块创建或恢复 Turn；已提交结果只从持久化
Outbox 重投，不再保留进程内锁、直接广播或零散 ActionPlan 恢复旁路。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import structlog
from collaboration_framework.contracts import PlayerInput, PlayerView
from collaboration_framework.runtime_context import current_turn_id

from app.adapters import SqlAlchemyTurnStore
from app.core.action_plan_turn import (
    ActionPlanTurnResult,
    action_plan_turn_application,
)
from app.core.db import async_session_factory
from app.core.turn import session_view_application
from app.core.turn_coordinator import (
    TurnCoordinator,
    TurnExecutionOutcome,
    TurnPhaseObserver,
)
from app.core.turn_events import TurnPhase
from app.core.turn_runtime import (
    TurnInputSnapshot,
    TurnRecord,
    TurnRuntimeStore,
    TurnStatus,
    TurnWaitingReason,
)
from app.service.turn_outbox import TurnOutboxDispatcher
from app.service.ws_manager import manager

logger = structlog.get_logger()
turn_store = SqlAlchemyTurnStore(async_session_factory)
turn_coordinator = TurnCoordinator(turn_store)
turn_outbox_dispatcher = TurnOutboxDispatcher(turn_store, manager)


async def _settle_failed_plan(turn: TurnRecord) -> None:
    """终态失败 Turn 不得遗留仍占住房间的 ActionPlan。"""

    if turn.status != TurnStatus.FAILED:
        return
    try:
        code = turn.last_error.code if turn.last_error is not None else "TURN_FAILED"
        # Turn 已是最终恢复来源。无论此前提交到哪一步，都要保留 receipt 并把
        # ActionPlan 收束为 stopped，避免步骤级 reservation 永久阻塞新行动。
        await action_plan_turn_application.settle_failed_turn_plan(
            room_id=turn.room_id,
            parent_action_id=turn.client_action_id,
            code=code,
        )
    except Exception as exc:
        # Turn 已经安全终止，清理失败不能改写玩家错误；记录类型供后台诊断。
        logger.error(
            "failed_turn_plan_cleanup_failed",
            turn_id=turn.turn_id,
            error_type=type(exc).__name__,
        )


@dataclass(frozen=True)
class ReliableTurnResponse:
    """供 WebSocket Controller 投影即时等待状态的协调结果。"""

    turn: TurnRecord
    action_result: ActionPlanTurnResult | None = None


async def start_action(
    *,
    room_id: str,
    player_id: str,
    client_action_id: str,
    utterance: str,
    on_progress: Callable[[object], Awaitable[None]] | None = None,
    on_phase: Callable[[TurnPhase], Awaitable[None]] | None = None,
    on_started: Callable[[str], Awaitable[None]] | None = None,
    on_input_accepted: Callable[[PlayerInput, PlayerView], Awaitable[None]] | None = None,
) -> ReliableTurnResponse:
    """创建可靠回合，并用既有 ActionPlan 链推进到等待或最终结果。"""

    actor_id = await session_view_application.resolve_actor_id(room_id, player_id)
    request = TurnInputSnapshot(
        room_id=room_id,
        player_id=player_id,
        actor_id=actor_id,
        client_action_id=client_action_id,
        utterance=utterance,
    )
    captured: ActionPlanTurnResult | None = None

    async def execute(on_phase_observer: TurnPhaseObserver) -> TurnExecutionOutcome:
        nonlocal captured

        turn_id = current_turn_id()
        if turn_id is None:
            raise RuntimeError("可靠回合执行缺少 turn_id 上下文")
        if on_started is not None:
            await on_started(turn_id)

        async def phases(phase: TurnPhase) -> None:
            await on_phase_observer(phase)
            if on_phase is not None:
                await on_phase(phase)

        captured = await action_plan_turn_application.start(
            room_id=room_id,
            player_id=player_id,
            client_action_id=client_action_id,
            utterance=utterance,
            on_progress=on_progress,
            on_phase=phases,
            on_input_accepted=on_input_accepted,
        )
        return _adapt_action_result(captured)

    turn = await turn_coordinator.start(
        request,
        executor=execute,
        after_publish=lambda: _mark_plan_narration(room_id, client_action_id, on_progress),
    )
    await _settle_failed_plan(turn)
    if captured is None and turn.result is not None:
        await turn_outbox_dispatcher.redispatch_turn(turn.turn_id)
    else:
        await turn_outbox_dispatcher.dispatch_due()
    return ReliableTurnResponse(turn=turn, action_result=captured)


async def continue_after_decision(
    *,
    room_id: str,
    player_id: str,
    client_action_id: str,
    decide: Callable[[], Awaitable[object]],
    on_progress: Callable[[object], Awaitable[None]] | None = None,
    on_phase: Callable[[TurnPhase], Awaitable[None]] | None = None,
) -> ReliableTurnResponse:
    """在原 Turn 的执行上下文内提交玩家选择，再恢复同一计划。"""

    turn = await _owned_turn(room_id, player_id, client_action_id)
    captured: ActionPlanTurnResult | None = None

    async def execute(on_phase_observer: TurnPhaseObserver) -> TurnExecutionOutcome:
        nonlocal captured
        await decide()

        async def phases(phase: TurnPhase) -> None:
            await on_phase_observer(phase)
            if on_phase is not None:
                await on_phase(phase)

        captured = await action_plan_turn_application.resume_pending(
            room_id=room_id,
            player_id=player_id,
            parent_action_id=client_action_id,
            on_progress=on_progress,
            on_phase=phases,
        )
        return _adapt_action_result(captured)

    saved = await turn_coordinator.resume(
        turn.turn_id,
        executor=execute,
        after_publish=lambda: _mark_plan_narration(room_id, client_action_id, on_progress),
    )
    await _settle_failed_plan(saved)
    if captured is None and saved.result is not None:
        await turn_outbox_dispatcher.redispatch_turn(saved.turn_id)
    else:
        await turn_outbox_dispatcher.dispatch_due()
    return ReliableTurnResponse(turn=saved, action_result=captured)


async def cancel_action(
    *,
    room_id: str,
    player_id: str,
    client_action_id: str,
    request_id: str,
) -> ReliableTurnResponse:
    """把取消操作归入原 Turn，并只收束尚未提交的计划步骤。"""

    turn = await _owned_turn(room_id, player_id, client_action_id)
    captured: ActionPlanTurnResult | None = None

    async def execute(on_phase: TurnPhaseObserver) -> TurnExecutionOutcome:
        nonlocal captured
        await on_phase("executing_action")
        captured = await action_plan_turn_application.cancel_remaining(
            room_id=room_id,
            player_id=player_id,
            parent_action_id=client_action_id,
            request_id=request_id,
        )
        await on_phase("generating_narration")
        return _adapt_action_result(captured)

    saved = await turn_coordinator.resume(
        turn.turn_id,
        executor=execute,
        after_publish=lambda: _mark_plan_narration(room_id, client_action_id, None),
    )
    await _settle_failed_plan(saved)
    if captured is None and saved.result is not None:
        await turn_outbox_dispatcher.redispatch_turn(saved.turn_id)
    else:
        await turn_outbox_dispatcher.dispatch_due()
    return ReliableTurnResponse(turn=saved, action_result=captured)


async def resume_turn_by_id(turn_id: str) -> TurnRecord:
    """按状态和 receipt 选择唯一恢复入口；等待玩家时只返回，不自动决策。"""

    turn = await turn_store.get(turn_id)
    if turn is None:
        raise LookupError("TurnRecord 不存在")
    if turn.is_terminal:
        await turn_outbox_dispatcher.dispatch_due()
        return turn
    if turn.resume_point.value == "awaiting_player":
        return turn

    async def execute(on_phase_observer: TurnPhaseObserver) -> TurnExecutionOutcome:
        plan = await action_plan_turn_application.get_plan(
            turn.room_id,
            turn.client_action_id,
        )

        async def phases(phase: TurnPhase) -> None:
            await on_phase_observer(phase)

        if plan is not None:
            result = await action_plan_turn_application.resume_owned(
                room_id=turn.room_id,
                player_id=turn.player_id,
                parent_action_id=turn.client_action_id,
                on_phase=phases,
            )
        elif turn.status in {TurnStatus.EXECUTING, TurnStatus.AWAITING_NARRATION}:
            result = await action_plan_turn_application.resume_single(
                room_id=turn.room_id,
                player_id=turn.player_id,
                parent_action_id=turn.client_action_id,
                on_phase=phases,
            )
        else:
            result = await action_plan_turn_application.start(
                room_id=turn.room_id,
                player_id=turn.player_id,
                client_action_id=turn.client_action_id,
                utterance=turn.request.utterance,
                on_phase=phases,
            )
        return _adapt_action_result(result)

    saved = await turn_coordinator.resume(
        turn.turn_id,
        executor=execute,
        after_publish=lambda: _mark_plan_narration(
            turn.room_id,
            turn.client_action_id,
            None,
        ),
    )
    await _settle_failed_plan(saved)
    await turn_outbox_dispatcher.dispatch_due()
    return saved


async def _owned_turn(room_id: str, player_id: str, client_action_id: str) -> TurnRecord:
    """加载玩家自己的原 Turn，防止选择事件跨回合或跨 owner 注入。"""

    turn = await turn_store.get_by_client_action(room_id, client_action_id)
    if turn is None or turn.player_id != player_id:
        raise LookupError("没有属于当前玩家的可靠回合")
    return turn


async def _mark_plan_narration(
    room_id: str,
    client_action_id: str,
    on_progress: Callable[[object], Awaitable[None]] | None,
) -> None:
    """Outbox 已持久化后，才允许步骤级 ActionPlan 标记叙事完成。"""

    await action_plan_turn_application.mark_narration_persisted(
        room_id=room_id,
        parent_action_id=client_action_id,
        on_progress=on_progress,
    )


def _adapt_action_result(result: ActionPlanTurnResult) -> TurnExecutionOutcome:
    """只把玩家安全结果交给 Coordinator，不传递 Prompt 或隐藏上下文。"""

    waiting_reason = TurnWaitingReason.NONE
    pending: dict | None = None
    if result.waiting_for_player:
        execution = result.execution
        if execution is None:
            raise RuntimeError("等待玩家的回合缺少 adjudication execution")
        if execution.status == "awaiting_skill_choice":
            waiting_reason = TurnWaitingReason.SKILL_CHOICE
        elif execution.status == "awaiting_post_roll_decision":
            waiting_reason = TurnWaitingReason.POST_ROLL_DECISION
        else:
            raise RuntimeError("等待玩家的回合状态不可识别")
        source = execution.pending_decision or execution.check_run
        if source is not None:
            pending = _safe_json(source)
    narration = None
    visibility = "public"
    if result.narration is not None:
        narration = {
            "kind": result.narration.kind,
            "text": result.narration.text,
            "claimedFactIds": list(result.narration.claimed_evidence_refs),
            "suggestedActions": list(result.narration.suggested_actions),
        }
        if result.narration.kind == "clarification":
            visibility = "player_scoped"
    view = result.player_view.to_json_dict()
    return TurnExecutionOutcome(
        status=result.status,
        player_view=view,
        view_revision=result.player_view.revision,
        scene_id=result.player_view.scene_id,
        narration=narration,
        waiting_reason=waiting_reason,
        pending_decision=pending,
        visibility=visibility,
    )


def _safe_json(value: Any) -> dict:
    """序列化玩家安全契约，拒绝把任意对象或内部堆栈写入 Turn。"""

    if hasattr(value, "to_json_dict"):
        payload = value.to_json_dict()
    elif hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
    else:
        raise TypeError("pending decision 不是可序列化契约")
    if not isinstance(payload, dict):
        raise TypeError("pending decision 必须序列化为对象")
    return payload


class OutboxDispatcher(Protocol):
    """Supervisor 依赖的最小 Outbox 扫描端口。"""

    async def dispatch_due(self, *, limit: int = 20) -> int: ...


class LegacyTurnAdopter(Protocol):
    """PR3 切换期使用的旧非终态记录收养端口。"""

    async def adopt_legacy_inflight_turns(self, *, limit: int = 20) -> tuple[TurnRecord, ...]: ...


class TurnRuntimeSupervisor:
    """单进程后台扫描器：恢复安全回合与到期 Outbox，不替玩家做选择。"""

    def __init__(
        self,
        *,
        store: TurnRuntimeStore | None = None,
        dispatcher: OutboxDispatcher | None = None,
        adopter: LegacyTurnAdopter | None = None,
        resume: Callable[[str], Awaitable[TurnRecord]] | None = None,
        interval_seconds: float = 2.0,
    ) -> None:
        self._store = store or turn_store
        self._dispatcher = dispatcher or turn_outbox_dispatcher
        # 内存测试 Store 不承担数据迁移；生产 SQL Store 才暴露收养能力。
        self._adopter: LegacyTurnAdopter | None = adopter or (
            cast(LegacyTurnAdopter, self._store)
            if hasattr(self._store, "adopt_legacy_inflight_turns")
            else None
        )
        self._resume = resume or resume_turn_by_id
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            await self.run_once()
            self._task = asyncio.create_task(self._run(), name="turn-runtime-supervisor")

    async def shutdown(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self.run_once()
            except Exception as exc:
                logger.warning("turn_outbox_supervisor_failed", error_type=type(exc).__name__)

    async def run_once(self, *, limit: int = 20) -> None:
        """先恢复过期回合，再投递 Outbox；单个坏回合不能阻塞整批扫描。"""

        # 先把仍可恢复且信息完整的旧记录绑定到 Turn，再与普通过期租约使用同一
        # 恢复函数推进；终态历史和信息不完整记录不会被收养。
        adopted = (
            await self._adopter.adopt_legacy_inflight_turns(limit=limit)
            if self._adopter is not None
            else ()
        )
        for turn in adopted:
            try:
                await self._resume(turn.turn_id)
            except Exception as exc:
                logger.warning(
                    "adopted_turn_recovery_failed",
                    turn_id=turn.turn_id,
                    error_type=type(exc).__name__,
                )

        now = datetime.now(UTC)
        recoverable = await self._store.list_recoverable_turns(now=now, limit=limit)
        for turn in recoverable:
            try:
                await self._resume(turn.turn_id)
            except Exception as exc:
                logger.warning(
                    "turn_recovery_failed",
                    turn_id=turn.turn_id,
                    error_type=type(exc).__name__,
                )
        await self._dispatcher.dispatch_due(limit=limit)


turn_runtime_supervisor = TurnRuntimeSupervisor()


__all__ = [
    "ReliableTurnResponse",
    "cancel_action",
    "continue_after_decision",
    "resume_turn_by_id",
    "start_action",
    "turn_outbox_dispatcher",
    "turn_runtime_supervisor",
    "turn_store",
]
