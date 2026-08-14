"""可靠回合 REST API 的玩家安全响应 DTO。

这里有意不包含请求原文、Prompt、模型原始输出、隐藏事件和内部堆栈；客户端只
获得恢复流程所需的状态、公开错误、待选择项与最终结果。
"""

from typing import Any

from app.core.turn_runtime import (
    TurnCommitState,
    TurnErrorStage,
    TurnRecoveryAction,
    TurnResumePoint,
    TurnStatus,
    TurnWaitingReason,
)
from app.dto.common import CamelModel, UtcDatetime


class TurnErrorRead(CamelModel):
    """可以安全显示给当前玩家的脱敏错误。"""

    code: str
    stage: TurnErrorStage
    retryable: bool
    public_message: str
    occurred_at: UtcDatetime


class TurnRead(CamelModel):
    """刷新、重连和重复请求时的最终恢复来源。"""

    turn_id: str
    room_id: str
    client_action_id: str
    status: TurnStatus
    commit_state: TurnCommitState
    resume_point: TurnResumePoint
    waiting_reason: TurnWaitingReason
    recovery_action: TurnRecoveryAction
    phase_version: int
    error: TurnErrorRead | None = None
    pending_decision: dict[str, Any] | None = None
    narration: dict[str, Any] | None = None
    message_id: str | None = None
    player_view: dict[str, Any] | None = None
    view_revision: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    completed_at: UtcDatetime | None = None
