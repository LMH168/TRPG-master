"""定义 GM 回合编排的持久模式、阶段轨迹与保守恢复契约。

本文件只描述应用层如何调度既有 Host、Engine、Agenda、Narrator 与 Outbox，
不保存领域状态，也不提供任何绕过 TurnCoordinator 的写入口。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GameMasterExecutionMode(StrEnum):
    """一个可靠 Turn 当前由哪类生产执行路径负责推进。"""

    LEGACY_UNKNOWN = "legacy_unknown"
    NEW_ACTION = "new_action"
    ACTION_PLAN = "action_plan"
    SINGLE_ADJUDICATION = "single_adjudication"
    AGENDA_CONTINUATION = "agenda_continuation"
    PLAYER_DECISION = "player_decision"
    NARRATION_ONLY = "narration_only"
    DELIVERY_ONLY = "delivery_only"


class GameMasterStage(StrEnum):
    """可观察但不承载领域语义的 GM 编排阶段。"""

    ACCEPTED = "accepted"
    CONTEXT_LOADED = "context_loaded"
    HOST_COMPLETED = "host_completed"
    VALIDATED = "validated"
    ENGINE_COMMITTED = "engine_committed"
    AGENDA_SETTLED = "agenda_settled"
    NARRATION_COMPLETED = "narration_completed"
    OUTBOX_PERSISTED = "outbox_persisted"
    DELIVERY_SCHEDULED = "delivery_scheduled"


_STAGE_ORDER = {stage: index for index, stage in enumerate(GameMasterStage)}


class GameMasterOrchestrationSnapshot(BaseModel):
    """随 Turn 持久化的最小编排索引；权威恢复仍需 execution/receipt 证明。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    execution_mode: GameMasterExecutionMode
    completed_stages: tuple[GameMasterStage, ...] = ()
    attempt_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_monotonic_stages(self) -> GameMasterOrchestrationSnapshot:
        """拒绝倒序或重复轨迹，避免恢复日志伪装成另一个执行历史。"""

        indexes = [_STAGE_ORDER[stage] for stage in self.completed_stages]
        if indexes != sorted(set(indexes)):
            raise ValueError("GM 编排阶段必须按固定顺序且不能重复")
        if self.execution_mode == GameMasterExecutionMode.LEGACY_UNKNOWN and self.completed_stages:
            raise ValueError("未知旧执行模式不能伪造编排阶段")
        return self

    def advance(
        self,
        stage: GameMasterStage,
        *,
        execution_mode: GameMasterExecutionMode | None = None,
    ) -> GameMasterOrchestrationSnapshot:
        """单调追加一个阶段，并允许首次获得权威证明时收窄执行模式。"""

        mode = execution_mode or self.execution_mode
        if mode == GameMasterExecutionMode.LEGACY_UNKNOWN:
            raise ValueError("旧回合必须先解析出唯一执行模式")
        if self.completed_stages and _STAGE_ORDER[stage] <= _STAGE_ORDER[self.completed_stages[-1]]:
            raise ValueError("GM 编排阶段不能倒退或重复")
        return GameMasterOrchestrationSnapshot(
            execution_mode=mode,
            completed_stages=(*self.completed_stages, stage),
            attempt_count=self.attempt_count,
        )


class GameMasterRecoveryEvidence(BaseModel):
    """恢复路由可读取的权威存在性证明，不包含 Prompt 或玩家不可见内容。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    has_action_plan: bool = False
    has_adjudication_execution: bool = False
    has_agenda_execution: bool = False
    receipt_count: int = Field(default=0, ge=0)
    has_result: bool = False
    has_outbox: bool = False


class GameMasterOrchestrationRequest(BaseModel):
    """统一编排入口所需的可信 Turn 身份；不携带 GameState 或隐藏模组内容。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: str = Field(min_length=1)
    room_id: str = Field(min_length=1)
    player_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    client_action_id: str = Field(min_length=1, max_length=200)
    execution_mode: GameMasterExecutionMode
    recovering: bool = False


class GameMasterOrchestrationResult(BaseModel):
    """执行适配器返回的玩家安全编排结果索引，不复制具体叙事或状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: str = Field(min_length=1)
    execution_mode: GameMasterExecutionMode
    completed_stages: tuple[GameMasterStage, ...]
    waiting_for_player: bool = False


GameMasterStageObserver = Callable[[GameMasterStage], Awaitable[None]]


class GameMasterExecutionPort(Protocol):
    """PR2 生产适配器的最小端口；领域执行仍由既有组件负责。"""

    async def execute(
        self,
        request: GameMasterOrchestrationRequest,
        *,
        on_stage: GameMasterStageObserver,
    ) -> GameMasterOrchestrationResult: ...


class GameMasterOrchestrator:
    """校验并调度一个 GM 回合，不保存独立状态或解释领域结果。"""

    def __init__(self, execution: GameMasterExecutionPort) -> None:
        self._execution = execution

    async def run(
        self,
        request: GameMasterOrchestrationRequest,
        *,
        snapshot: GameMasterOrchestrationSnapshot,
        on_stage: GameMasterStageObserver | None = None,
    ) -> GameMasterOrchestrationResult:
        """委托执行并验证返回轨迹，阻止适配器跳过或伪造既有阶段。"""

        if request.execution_mode != snapshot.execution_mode:
            raise ValueError("GM 编排请求与持久执行模式不一致")
        if request.execution_mode == GameMasterExecutionMode.LEGACY_UNKNOWN:
            raise ValueError("未知旧执行模式必须先由权威证据解析")
        observed = snapshot

        async def observe(stage: GameMasterStage) -> None:
            nonlocal observed
            observed = observed.advance(stage)
            if on_stage is not None:
                await on_stage(stage)

        result = await self._execution.execute(request, on_stage=observe)
        if result.turn_id != request.turn_id:
            raise ValueError("GM 执行结果指向了不同 Turn")
        if (
            result.execution_mode != request.execution_mode
            and request.execution_mode != GameMasterExecutionMode.NEW_ACTION
        ):
            raise ValueError("GM 执行结果切换了已确定的持久执行模式")
        if result.execution_mode == GameMasterExecutionMode.LEGACY_UNKNOWN:
            raise ValueError("GM 执行结果不得保留未知旧模式")
        if result.completed_stages != observed.completed_stages:
            raise ValueError("GM 执行结果与已观察阶段轨迹不一致")
        return result


def resolve_legacy_execution_mode(
    evidence: GameMasterRecoveryEvidence,
) -> GameMasterExecutionMode | None:
    """只在旧回合的权威证据唯一时收养；歧义时交由玩家安全失败处理。"""

    if evidence.has_outbox or evidence.has_result:
        return GameMasterExecutionMode.DELIVERY_ONLY
    if evidence.has_action_plan and not evidence.has_adjudication_execution:
        return GameMasterExecutionMode.ACTION_PLAN
    if evidence.has_adjudication_execution and not evidence.has_action_plan:
        return GameMasterExecutionMode.SINGLE_ADJUDICATION
    if evidence.has_agenda_execution and evidence.receipt_count > 0:
        return GameMasterExecutionMode.AGENDA_CONTINUATION
    if evidence.receipt_count > 0:
        return GameMasterExecutionMode.NARRATION_ONLY
    return None


def validate_orchestration_update(
    current: GameMasterOrchestrationSnapshot | None,
    updated: GameMasterOrchestrationSnapshot | None,
) -> None:
    """校验 Turn CAS 中的编排索引只能收窄模式并向前追加阶段。"""

    if current is None:
        return
    if updated is None:
        raise ValueError("GM 编排快照不得被删除")
    if updated.completed_stages[: len(current.completed_stages)] != current.completed_stages:
        raise ValueError("GM 编排阶段不得被改写")
    if updated.attempt_count < current.attempt_count:
        raise ValueError("GM 编排尝试次数不得减少")
    if (
        current.execution_mode != updated.execution_mode
        and current.execution_mode != GameMasterExecutionMode.NEW_ACTION
        and current.execution_mode != GameMasterExecutionMode.LEGACY_UNKNOWN
    ):
        raise ValueError("已确定的 GM 执行模式不得切换")


__all__ = [
    "GameMasterExecutionPort",
    "GameMasterExecutionMode",
    "GameMasterOrchestrationRequest",
    "GameMasterOrchestrationResult",
    "GameMasterOrchestrationSnapshot",
    "GameMasterOrchestrator",
    "GameMasterRecoveryEvidence",
    "GameMasterStage",
    "GameMasterStageObserver",
    "resolve_legacy_execution_mode",
    "validate_orchestration_update",
]
