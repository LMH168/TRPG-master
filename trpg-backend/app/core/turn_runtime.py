"""定义可靠回合的状态机、持久化契约与存储端口。

本文件只描述回合级生命周期，不接管 ActionPlan 的步骤游标，也不解释 Engine
领域效果。生产数据库与测试内存实现都必须遵守这里冻结的幂等、CAS 和恢复语义。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.gm_orchestration import (
    GameMasterExecutionMode,
    GameMasterOrchestrationSnapshot,
    GameMasterStage,
    validate_orchestration_update,
)


class TurnStatus(StrEnum):
    """一次玩家输入在回合协调器中的持久化阶段。"""

    RECEIVED = "received"
    PLANNING = "planning"
    ADJUDICATING = "adjudicating"
    EXECUTING = "executing"
    AWAITING_NARRATION = "awaiting_narration"
    DELIVERING = "delivering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TurnCommitState(StrEnum):
    """玩家目标进入 Engine 权威提交边界的程度。"""

    NOT_COMMITTED = "not_committed"
    PARTIALLY_COMMITTED = "partially_committed"
    COMMITTED = "committed"


class TurnResumePoint(StrEnum):
    """服务重建或玩家重试时唯一允许继续的位置。"""

    PLANNING = "planning"
    ADJUDICATING = "adjudicating"
    EXECUTING = "executing"
    NARRATING = "narrating"
    DELIVERING = "delivering"
    AWAITING_PLAYER = "awaiting_player"
    NONE = "none"


class TurnWaitingReason(StrEnum):
    """回合暂停等待玩家输入的公开原因。"""

    SKILL_CHOICE = "skill_choice"
    POST_ROLL_DECISION = "post_roll_decision"
    NONE = "none"


class TurnRecoveryAction(StrEnum):
    """客户端根据持久状态可以安全执行的下一步。"""

    WAIT = "wait"
    RETRY_SAME_INPUT = "retry_same_input"
    CHOOSE_SKILL = "choose_skill"
    CHOOSE_POST_ROLL = "choose_post_roll"
    FETCH_RESULT = "fetch_result"
    SUBMIT_NEW_INPUT = "submit_new_input"
    NONE = "none"


class TurnErrorStage(StrEnum):
    """错误发生的稳定阶段；不得把内部函数名暴露给客户端。"""

    RECEIVE = "receive"
    PLANNING = "planning"
    VALIDATION = "validation"
    ADJUDICATION = "adjudication"
    EXECUTION = "execution"
    NARRATION = "narration"
    DELIVERY = "delivery"
    RECOVERY = "recovery"


class TurnOutboxStatus(StrEnum):
    """最终叙事在可靠投递层中的状态。"""

    PENDING = "pending"
    LEASED = "leased"
    DISPATCHED = "dispatched"
    DEAD_LETTER = "dead_letter"


TERMINAL_TURN_STATUSES = frozenset({TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED})

_ALLOWED_STATUS_TRANSITIONS: dict[TurnStatus, frozenset[TurnStatus]] = {
    TurnStatus.RECEIVED: frozenset({TurnStatus.PLANNING, TurnStatus.FAILED, TurnStatus.CANCELLED}),
    TurnStatus.PLANNING: frozenset(
        {
            TurnStatus.ADJUDICATING,
            TurnStatus.AWAITING_NARRATION,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
        }
    ),
    TurnStatus.ADJUDICATING: frozenset(
        {
            TurnStatus.EXECUTING,
            TurnStatus.AWAITING_NARRATION,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
        }
    ),
    TurnStatus.EXECUTING: frozenset(
        {
            TurnStatus.ADJUDICATING,
            TurnStatus.AWAITING_NARRATION,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
        }
    ),
    TurnStatus.AWAITING_NARRATION: frozenset(
        {TurnStatus.DELIVERING, TurnStatus.FAILED, TurnStatus.CANCELLED}
    ),
    TurnStatus.DELIVERING: frozenset(
        {TurnStatus.COMPLETED, TurnStatus.CANCELLED, TurnStatus.FAILED}
    ),
    TurnStatus.COMPLETED: frozenset(),
    TurnStatus.FAILED: frozenset(),
    TurnStatus.CANCELLED: frozenset(),
}

_COMMIT_STATE_ORDER = {
    TurnCommitState.NOT_COMMITTED: 0,
    TurnCommitState.PARTIALLY_COMMITTED: 1,
    TurnCommitState.COMMITTED: 2,
}


class TurnContractError(ValueError):
    """回合契约自身不满足，调用方必须停止写入。"""


class TurnConflictError(RuntimeError):
    """幂等键、房间占用或 CAS 发生冲突。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class TurnNotFoundError(LookupError):
    """指定回合不存在。"""


class TurnInputSnapshot(BaseModel):
    """规划阶段恢复所需的版本化玩家输入快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    room_id: str = Field(min_length=1)
    player_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    client_action_id: str = Field(min_length=1, max_length=200)
    utterance: str = Field(min_length=1, max_length=2000)

    @field_validator(
        "room_id",
        "player_id",
        "actor_id",
        "client_action_id",
        "utterance",
    )
    @classmethod
    def strip_non_empty(cls, value: str) -> str:
        """统一去除协议边界空白，保证 fingerprint 跨重试稳定。"""

        stripped = value.strip()
        if not stripped:
            raise ValueError("不能为空")
        return stripped

    def fingerprint(self) -> str:
        """对身份与规范化输入生成稳定 SHA-256，不记录额外明文日志。"""

        payload = {
            "actor_id": self.actor_id,
            "player_id": self.player_id,
            "room_id": self.room_id,
            "utterance": self.utterance,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class TurnFailureSnapshot(BaseModel):
    """允许持久化和返回给玩家的脱敏错误快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    code: str = Field(min_length=1, max_length=100)
    stage: TurnErrorStage
    retryable: bool
    attempt_count: int = Field(default=1, ge=1)
    commit_state: TurnCommitState
    recovery_action: TurnRecoveryAction
    public_message: str = Field(min_length=1, max_length=1000)
    occurred_at: datetime


class TurnResultSnapshot(BaseModel):
    """最终输出的玩家安全快照；不包含隐藏事件或模型原始响应。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    message_id: str = Field(min_length=1, max_length=200)
    narration: dict = Field(default_factory=dict)
    player_view: dict = Field(default_factory=dict)
    view_revision: str = Field(min_length=1)


class TurnRecord(BaseModel):
    """回合级权威记录，与步骤级 ActionPlanRun 保持职责分离。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: str = Field(min_length=1)
    room_id: str = Field(min_length=1)
    client_action_id: str = Field(min_length=1, max_length=200)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    player_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    request: TurnInputSnapshot
    orchestration: GameMasterOrchestrationSnapshot | None = None
    status: TurnStatus = TurnStatus.RECEIVED
    phase_version: int = Field(default=1, ge=1)
    resume_point: TurnResumePoint = TurnResumePoint.PLANNING
    waiting_reason: TurnWaitingReason = TurnWaitingReason.NONE
    commit_state: TurnCommitState = TurnCommitState.NOT_COMMITTED
    recovery_action: TurnRecoveryAction = TurnRecoveryAction.WAIT
    pending_decision: dict | None = None
    last_error: TurnFailureSnapshot | None = None
    result: TurnResultSnapshot | None = None
    lease_owner: str | None = Field(default=None, min_length=1, max_length=200)
    lease_expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_consistency(self) -> TurnRecord:
        """拒绝身份漂移、半份 lease 和不完整终态。"""

        request = self.request
        if (
            request.room_id != self.room_id
            or request.player_id != self.player_id
            or request.actor_id != self.actor_id
            or request.client_action_id != self.client_action_id
        ):
            raise ValueError("TurnRecord 与请求快照身份不一致")
        if request.fingerprint() != self.input_fingerprint:
            raise ValueError("TurnRecord 输入 fingerprint 不一致")
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("lease_owner 与 lease_expires_at 必须同时存在或为空")
        if self.status in TERMINAL_TURN_STATUSES:
            if self.resume_point != TurnResumePoint.NONE:
                raise ValueError("终态回合不得保留恢复点")
            if self.recovery_action not in {
                TurnRecoveryAction.FETCH_RESULT,
                TurnRecoveryAction.SUBMIT_NEW_INPUT,
                TurnRecoveryAction.NONE,
            }:
                raise ValueError("终态回合恢复动作不合法")
            if self.completed_at is None:
                raise ValueError("终态回合必须记录 completed_at")
            if self.lease_owner is not None:
                raise ValueError("终态回合不得持有 worker lease")
        elif self.completed_at is not None:
            raise ValueError("非终态回合不得记录 completed_at")
        if self.waiting_reason != TurnWaitingReason.NONE and (
            self.status != TurnStatus.ADJUDICATING
            or self.resume_point != TurnResumePoint.AWAITING_PLAYER
        ):
            raise ValueError("玩家等待原因只能出现在 adjudicating/awaiting_player")
        if self.pending_decision is not None and (
            self.status != TurnStatus.ADJUDICATING
            or self.resume_point != TurnResumePoint.AWAITING_PLAYER
        ):
            raise ValueError("待决策快照只能出现在 adjudicating/awaiting_player")
        if self.result is not None and self.status not in {
            TurnStatus.DELIVERING,
            TurnStatus.COMPLETED,
            TurnStatus.CANCELLED,
        }:
            raise ValueError("最终结果只能出现在投递或成功/取消终态")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TURN_STATUSES


class TurnCommitReceipt(BaseModel):
    """一次 Engine 命令已跨过权威事务边界的证明。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: str = Field(min_length=1)
    room_id: str = Field(min_length=1)
    engine_request_id: str = Field(min_length=1, max_length=200)
    action_request_id: str = Field(min_length=1, max_length=200)
    committed_state_version: int = Field(ge=0)
    first_event_sequence: int | None = Field(default=None, ge=1)
    last_event_sequence: int | None = Field(default=None, ge=1)
    created_at: datetime

    @model_validator(mode="after")
    def validate_event_range(self) -> TurnCommitReceipt:
        """允许无事件命令，但禁止只写范围的一端或倒序范围。"""

        if (self.first_event_sequence is None) != (self.last_event_sequence is None):
            raise ValueError("Event sequence 范围必须同时存在或为空")
        if (
            self.first_event_sequence is not None
            and self.last_event_sequence is not None
            and self.first_event_sequence > self.last_event_sequence
        ):
            raise ValueError("Event sequence 范围不能倒序")
        return self


class NarrationOutboxMessage(BaseModel):
    """已通过证据校验、可安全重复发送的最终叙事。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outbox_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    room_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1, max_length=200)
    message_type: str = Field(default="narration.push", min_length=1, max_length=50)
    visibility: str = Field(pattern=r"^(public|player_scoped)$")
    player_id: str = Field(min_length=1)
    payload_schema_version: int = Field(default=1, ge=1)
    payload: dict
    status: TurnOutboxStatus = TurnOutboxStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)
    lease_owner: str | None = Field(default=None, min_length=1, max_length=200)
    lease_expires_at: datetime | None = None
    next_attempt_at: datetime
    last_error_code: str | None = Field(default=None, min_length=1, max_length=100)
    created_at: datetime
    updated_at: datetime
    last_dispatched_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lease(self) -> NarrationOutboxMessage:
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("Outbox lease 必须完整存在或为空")
        if self.status == TurnOutboxStatus.LEASED and self.lease_owner is None:
            raise ValueError("leased Outbox 必须持有 lease")
        if self.status != TurnOutboxStatus.LEASED and self.lease_owner is not None:
            raise ValueError("非 leased Outbox 不得持有 lease")
        return self


class TurnReplayEvent(BaseModel):
    """叙事发布事务要写入的玩家安全回放事件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    room_id: str = Field(min_length=1)
    player_id: str = Field(min_length=1)
    event_type: str = Field(default="narration.push", min_length=1, max_length=50)
    correlation_id: str = Field(min_length=1, max_length=200)
    visibility: str = Field(pattern=r"^(public|player_scoped)$")
    actor_id: str = Field(min_length=1)
    scene_id: str = Field(min_length=1)
    view_revision: str = Field(min_length=1)
    payload: dict
    created_at: datetime


def new_turn_record(request: TurnInputSnapshot, *, now: datetime | None = None) -> TurnRecord:
    """为首次被接受的玩家输入创建服务端回合身份。"""

    current = now or datetime.now(UTC)
    return TurnRecord(
        turn_id=str(uuid4()),
        room_id=request.room_id,
        client_action_id=request.client_action_id,
        input_fingerprint=request.fingerprint(),
        player_id=request.player_id,
        actor_id=request.actor_id,
        request=request,
        orchestration=GameMasterOrchestrationSnapshot(
            execution_mode=GameMasterExecutionMode.NEW_ACTION,
            completed_stages=(GameMasterStage.ACCEPTED,),
        ),
        created_at=current,
        updated_at=current,
    )


def transition_turn(
    current: TurnRecord,
    *,
    status: TurnStatus,
    now: datetime | None = None,
    resume_point: TurnResumePoint,
    waiting_reason: TurnWaitingReason = TurnWaitingReason.NONE,
    commit_state: TurnCommitState | None = None,
    recovery_action: TurnRecoveryAction,
    pending_decision: dict | None = None,
    last_error: TurnFailureSnapshot | None = None,
    result: TurnResultSnapshot | None = None,
) -> TurnRecord:
    """执行一次单调状态转换；任何阶段跳跃都必须在这里显式获准。"""

    if status != current.status and status not in _ALLOWED_STATUS_TRANSITIONS[current.status]:
        raise TurnContractError(f"非法回合状态转换: {current.status} -> {status}")
    current_time = now or datetime.now(UTC)
    terminal = status in TERMINAL_TURN_STATUSES
    values = current.model_dump()
    values.update(
        {
            "status": status,
            "phase_version": current.phase_version + 1,
            "resume_point": resume_point,
            "waiting_reason": waiting_reason,
            "commit_state": commit_state or current.commit_state,
            "recovery_action": recovery_action,
            "pending_decision": pending_decision,
            "last_error": last_error,
            "result": result if result is not None else current.result,
            "lease_owner": None if terminal else current.lease_owner,
            "lease_expires_at": None if terminal else current.lease_expires_at,
            "updated_at": current_time,
            "completed_at": current_time if terminal else None,
        }
    )
    # model_copy(update=...) 不会重新运行 Pydantic validator；状态转换必须重新
    # 校验整份记录，不能让等待原因、终态 lease 等不变量被内部调用绕过。
    updated = TurnRecord.model_validate(values)
    validate_turn_cas_update(current, updated, expected_phase_version=current.phase_version)
    return updated


def validate_turn_cas_update(
    current: TurnRecord,
    updated: TurnRecord,
    *,
    expected_phase_version: int,
) -> None:
    """校验状态 CAS 的身份、版本、阶段和提交证明都只能单调推进。"""

    _require_same_turn_identity(current, updated)
    if current.phase_version != expected_phase_version:
        raise TurnConflictError("TURN_VERSION_CONFLICT", "回合已被其他 worker 更新")
    if updated.phase_version != expected_phase_version + 1:
        raise TurnContractError("CAS 更新必须将 phase_version 精确增加 1")
    if (
        updated.status != current.status
        and updated.status not in _ALLOWED_STATUS_TRANSITIONS[current.status]
    ):
        raise TurnContractError(f"非法回合状态转换: {current.status} -> {updated.status}")
    if _COMMIT_STATE_ORDER[updated.commit_state] < _COMMIT_STATE_ORDER[current.commit_state]:
        raise TurnContractError("commit_state 不得从已提交状态降级")
    if updated.request != current.request or updated.created_at != current.created_at:
        raise TurnContractError("CAS 不得修改回合请求快照或创建时间")
    try:
        validate_orchestration_update(current.orchestration, updated.orchestration)
    except ValueError as exc:
        raise TurnContractError(str(exc)) from exc


class TurnStore(Protocol):
    """回合协调器依赖的持久化端口。"""

    async def create_or_get(self, proposed: TurnRecord) -> tuple[TurnRecord, bool]: ...

    async def get(self, turn_id: str) -> TurnRecord | None: ...

    async def get_by_client_action(
        self, room_id: str, client_action_id: str
    ) -> TurnRecord | None: ...

    async def list_for_player(
        self,
        *,
        room_id: str,
        player_id: str,
        active_only: bool,
        limit: int,
    ) -> tuple[TurnRecord, ...]: ...

    async def compare_and_swap(
        self, *, expected_phase_version: int, updated: TurnRecord
    ) -> TurnRecord: ...

    async def claim(
        self,
        *,
        turn_id: str,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> TurnRecord: ...

    async def release_claim(
        self,
        *,
        turn_id: str,
        worker_id: str,
        expected_phase_version: int,
        now: datetime,
    ) -> TurnRecord: ...


class TurnRuntimeStore(TurnStore, Protocol):
    """Coordinator 所需的 receipt、发布事务与 Outbox 消费端口。"""

    async def list_receipts(self, turn_id: str) -> tuple[TurnCommitReceipt, ...]: ...

    async def list_recoverable_turns(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[TurnRecord, ...]: ...

    async def publish_narration(
        self,
        *,
        expected_phase_version: int,
        updated_turn: TurnRecord,
        message: NarrationOutboxMessage,
        replay_event: TurnReplayEvent,
    ) -> tuple[TurnRecord, NarrationOutboxMessage, bool]: ...

    async def get_outbox(
        self,
        turn_id: str,
        message_type: str = "narration.push",
    ) -> NarrationOutboxMessage | None: ...

    async def claim_due_outbox(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[NarrationOutboxMessage, ...]: ...

    async def settle_outbox(
        self,
        *,
        outbox_id: str,
        worker_id: str,
        outcome: str,
        now: datetime,
        next_attempt_at: datetime,
        error_code: str | None = None,
        max_attempts: int = 5,
    ) -> NarrationOutboxMessage: ...


class InMemoryTurnStore(TurnStore):
    """供状态机和上层协调器测试使用的确定性内存 Store。"""

    def __init__(self) -> None:
        self._records: dict[str, TurnRecord] = {}
        self._client_index: dict[tuple[str, str], str] = {}
        self._room_reservations: dict[str, str] = {}
        self._receipts: dict[tuple[str, str], TurnCommitReceipt] = {}
        self._outbox: dict[str, NarrationOutboxMessage] = {}
        self._outbox_by_turn_type: dict[tuple[str, str], str] = {}
        self._replay_events: dict[str, TurnReplayEvent] = {}

    async def create_or_get(self, proposed: TurnRecord) -> tuple[TurnRecord, bool]:
        key = (proposed.room_id, proposed.client_action_id)
        existing_id = self._client_index.get(key)
        if existing_id is not None:
            existing = self._records[existing_id]
            _require_same_idempotent_request(existing, proposed)
            return existing.model_copy(deep=True), False
        owner = self._room_reservations.get(proposed.room_id)
        if owner is not None:
            raise TurnConflictError("TURN_IN_PROGRESS", "当前房间已有未完成回合")
        saved = proposed.model_copy(deep=True)
        self._records[saved.turn_id] = saved
        self._client_index[key] = saved.turn_id
        self._room_reservations[saved.room_id] = saved.turn_id
        return saved.model_copy(deep=True), True

    async def get(self, turn_id: str) -> TurnRecord | None:
        record = self._records.get(turn_id)
        return record.model_copy(deep=True) if record is not None else None

    async def get_by_client_action(self, room_id: str, client_action_id: str) -> TurnRecord | None:
        turn_id = self._client_index.get((room_id, client_action_id))
        return await self.get(turn_id) if turn_id is not None else None

    async def list_for_player(
        self,
        *,
        room_id: str,
        player_id: str,
        active_only: bool,
        limit: int,
    ) -> tuple[TurnRecord, ...]:
        matches: Iterable[TurnRecord] = (
            record
            for record in self._records.values()
            if record.room_id == room_id and record.player_id == player_id
        )
        if active_only:
            matches = (record for record in matches if not record.is_terminal)
        ordered = sorted(matches, key=lambda item: (item.created_at, item.turn_id), reverse=True)
        return tuple(item.model_copy(deep=True) for item in ordered[:limit])

    async def compare_and_swap(
        self, *, expected_phase_version: int, updated: TurnRecord
    ) -> TurnRecord:
        current = self._records.get(updated.turn_id)
        if current is None:
            raise TurnNotFoundError("TurnRecord 不存在")
        validate_turn_cas_update(
            current,
            updated,
            expected_phase_version=expected_phase_version,
        )
        saved = updated.model_copy(deep=True)
        self._records[saved.turn_id] = saved
        if saved.is_terminal:
            if self._room_reservations.get(saved.room_id) == saved.turn_id:
                del self._room_reservations[saved.room_id]
        elif self._room_reservations.get(saved.room_id) != saved.turn_id:
            raise TurnConflictError("TURN_RESERVATION_LOST", "回合已失去房间占用")
        return saved.model_copy(deep=True)

    async def claim(
        self,
        *,
        turn_id: str,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> TurnRecord:
        """领取非终态回合；同 worker 可续租，过期 lease 可被接管。"""

        if not worker_id or lease_expires_at <= now:
            raise TurnContractError("worker lease 必须具有有效 owner 和未来截止时间")
        current = self._records.get(turn_id)
        if current is None:
            raise TurnNotFoundError("TurnRecord 不存在")
        if current.is_terminal:
            return current.model_copy(deep=True)
        if (
            current.lease_owner is not None
            and current.lease_owner != worker_id
            and current.lease_expires_at is not None
            and current.lease_expires_at > now
        ):
            raise TurnConflictError("TURN_WORKER_BUSY", "回合正由其他 worker 推进")
        claimed = TurnRecord.model_validate(
            {
                **current.model_dump(),
                "phase_version": current.phase_version + 1,
                "lease_owner": worker_id,
                "lease_expires_at": lease_expires_at,
                "updated_at": now,
            }
        )
        self._records[turn_id] = claimed
        return claimed.model_copy(deep=True)

    async def release_claim(
        self,
        *,
        turn_id: str,
        worker_id: str,
        expected_phase_version: int,
        now: datetime,
    ) -> TurnRecord:
        """只有当前 lease owner 能按 CAS 释放自己的领取。"""

        current = self._records.get(turn_id)
        if current is None:
            raise TurnNotFoundError("TurnRecord 不存在")
        if current.phase_version != expected_phase_version:
            raise TurnConflictError("TURN_VERSION_CONFLICT", "回合已被其他 worker 更新")
        if current.lease_owner != worker_id:
            raise TurnConflictError("TURN_LEASE_LOST", "回合 worker lease 已失效")
        released = TurnRecord.model_validate(
            {
                **current.model_dump(),
                "phase_version": current.phase_version + 1,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            }
        )
        self._records[turn_id] = released
        return released.model_copy(deep=True)

    async def append_receipt(self, receipt: TurnCommitReceipt) -> TurnCommitReceipt:
        """测试与离线执行器用的幂等 receipt 写入。"""

        key = (receipt.room_id, receipt.engine_request_id)
        existing = self._receipts.get(key)
        if existing is not None and existing != receipt:
            raise TurnConflictError("TURN_RECEIPT_CONFLICT", "Engine request 已存在不同提交证明")
        self._receipts[key] = receipt.model_copy(deep=True)
        return receipt.model_copy(deep=True)

    async def list_receipts(self, turn_id: str) -> tuple[TurnCommitReceipt, ...]:
        receipts = sorted(
            (item for item in self._receipts.values() if item.turn_id == turn_id),
            key=lambda item: (item.created_at, item.engine_request_id),
        )
        return tuple(item.model_copy(deep=True) for item in receipts)

    async def list_recoverable_turns(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[TurnRecord, ...]:
        """返回无需玩家补充输入、且当前没有有效 worker lease 的回合。"""

        if limit < 1:
            raise TurnContractError("limit 必须大于 0")
        matches = (
            record
            for record in self._records.values()
            if not record.is_terminal
            and record.resume_point != TurnResumePoint.AWAITING_PLAYER
            and (
                record.last_error is None
                or record.last_error.retryable
                or record.status == TurnStatus.DELIVERING
            )
            and (
                (record.lease_owner is None and record.updated_at <= now - timedelta(seconds=60))
                or (record.lease_expires_at is not None and record.lease_expires_at <= now)
            )
        )
        ordered = sorted(matches, key=lambda item: (item.updated_at, item.turn_id))
        return tuple(item.model_copy(deep=True) for item in ordered[:limit])

    async def publish_narration(
        self,
        *,
        expected_phase_version: int,
        updated_turn: TurnRecord,
        message: NarrationOutboxMessage,
        replay_event: TurnReplayEvent,
    ) -> tuple[TurnRecord, NarrationOutboxMessage, bool]:
        """在内存测试中模拟 Turn、Result、Outbox 与回放事件的原子发布。"""

        if updated_turn.status != TurnStatus.DELIVERING or updated_turn.result is None:
            raise TurnContractError("叙事发布必须把带结果的回合推进到 delivering")
        if not (
            updated_turn.turn_id == message.turn_id == replay_event.turn_id
            and updated_turn.room_id == message.room_id == replay_event.room_id
        ):
            raise TurnContractError("叙事发布的 Turn、Outbox 与回放事件身份不一致")
        key = (message.turn_id, message.message_type)
        existing_id = self._outbox_by_turn_type.get(key)
        if existing_id is not None:
            existing = self._outbox[existing_id]
            return self._records[updated_turn.turn_id], existing.model_copy(deep=True), False
        saved_turn = await self.compare_and_swap(
            expected_phase_version=expected_phase_version,
            updated=updated_turn,
        )
        self._outbox[message.outbox_id] = message.model_copy(deep=True)
        self._outbox_by_turn_type[key] = message.outbox_id
        self._replay_events[replay_event.event_id] = replay_event.model_copy(deep=True)
        return saved_turn, message.model_copy(deep=True), True

    async def get_outbox(
        self,
        turn_id: str,
        message_type: str = "narration.push",
    ) -> NarrationOutboxMessage | None:
        """读取一个回合的稳定 Outbox，供恢复与测试使用。"""

        outbox_id = self._outbox_by_turn_type.get((turn_id, message_type))
        if outbox_id is None:
            return None
        return self._outbox[outbox_id].model_copy(deep=True)

    async def claim_due_outbox(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[NarrationOutboxMessage, ...]:
        """按稳定顺序领取到期消息，允许接管过期 lease。"""

        candidates = sorted(
            self._outbox.values(), key=lambda item: (item.created_at, item.outbox_id)
        )
        claimed: list[NarrationOutboxMessage] = []
        for current in candidates:
            if len(claimed) >= limit:
                break
            lease_expired = (
                current.status == TurnOutboxStatus.LEASED
                and current.lease_expires_at is not None
                and current.lease_expires_at <= now
            )
            if (
                not (current.status == TurnOutboxStatus.PENDING or lease_expired)
                or current.next_attempt_at > now
            ):
                continue
            item = NarrationOutboxMessage.model_validate(
                {
                    **current.model_dump(),
                    "status": TurnOutboxStatus.LEASED,
                    "lease_owner": worker_id,
                    "lease_expires_at": lease_expires_at,
                    "updated_at": now,
                }
            )
            self._outbox[item.outbox_id] = item
            claimed.append(item.model_copy(deep=True))
        return tuple(claimed)

    async def settle_outbox(
        self,
        *,
        outbox_id: str,
        worker_id: str,
        outcome: str,
        now: datetime,
        next_attempt_at: datetime,
        error_code: str | None = None,
        max_attempts: int = 5,
    ) -> NarrationOutboxMessage:
        """结束一次领取；无接收者不计失败，真实发送失败才消耗预算。"""

        current = self._outbox[outbox_id]
        if current.status != TurnOutboxStatus.LEASED or current.lease_owner != worker_id:
            raise TurnConflictError("TURN_OUTBOX_LEASE_LOST", "Outbox lease 已失效")
        attempts = current.attempt_count + (0 if outcome == "no_recipient" else 1)
        if outcome == "dispatched":
            status = TurnOutboxStatus.DISPATCHED
        elif outcome == "failed" and attempts >= max_attempts:
            status = TurnOutboxStatus.DEAD_LETTER
        elif outcome in {"failed", "no_recipient"}:
            status = TurnOutboxStatus.PENDING
        else:
            raise TurnContractError("未知 Outbox 投递结果")
        updated = NarrationOutboxMessage.model_validate(
            {
                **current.model_dump(),
                "status": status,
                "attempt_count": attempts,
                "lease_owner": None,
                "lease_expires_at": None,
                "next_attempt_at": next_attempt_at,
                "last_error_code": error_code if outcome == "failed" else None,
                "updated_at": now,
                "last_dispatched_at": now
                if outcome == "dispatched"
                else current.last_dispatched_at,
            }
        )
        self._outbox[outbox_id] = updated
        return updated.model_copy(deep=True)

    def snapshot(self) -> tuple[TurnRecord, ...]:
        """仅供测试检查内部状态，返回深拷贝避免绕过 Store 修改。"""

        return tuple(deepcopy(record) for record in self._records.values())


def _require_same_idempotent_request(current: TurnRecord, proposed: TurnRecord) -> None:
    """同一客户端幂等键不得更换输入、玩家或 Actor。"""

    if (
        current.room_id != proposed.room_id
        or current.client_action_id != proposed.client_action_id
        or current.input_fingerprint != proposed.input_fingerprint
        or current.player_id != proposed.player_id
        or current.actor_id != proposed.actor_id
    ):
        raise TurnConflictError("TURN_IDEMPOTENCY_CONFLICT", "回合幂等键已被不同输入占用")


def _require_same_turn_identity(current: TurnRecord, proposed: TurnRecord) -> None:
    """CAS 更新除请求身份外还必须指向同一个服务端 Turn。"""

    _require_same_idempotent_request(current, proposed)
    if current.turn_id != proposed.turn_id:
        raise TurnConflictError("TURN_IDENTITY_CONFLICT", "CAS 更新指向了不同回合")


__all__ = [
    "InMemoryTurnStore",
    "NarrationOutboxMessage",
    "TERMINAL_TURN_STATUSES",
    "TurnCommitReceipt",
    "TurnCommitState",
    "TurnConflictError",
    "TurnContractError",
    "TurnErrorStage",
    "TurnFailureSnapshot",
    "TurnInputSnapshot",
    "TurnNotFoundError",
    "TurnOutboxStatus",
    "TurnRecord",
    "TurnRecoveryAction",
    "TurnResultSnapshot",
    "TurnResumePoint",
    "TurnStatus",
    "TurnStore",
    "TurnRuntimeStore",
    "TurnReplayEvent",
    "TurnWaitingReason",
    "new_turn_record",
    "transition_turn",
    "validate_turn_cas_update",
]
