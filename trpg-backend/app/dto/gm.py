"""Phase 0 AI 主持运行时的严格 DTO 契约。

模型只能生成这些候选命令，真正的状态变化仍由 Kernel 校验并提交。
"""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, TypeAdapter

from app.dto.common import CamelModel, UtcDatetime


class StrictCamelModel(CamelModel):
    """禁止未知字段，防止模型偷偷扩展权威命令。"""

    model_config = ConfigDict(extra="forbid")


class MoveActor(StrictCamelModel):
    """请求把当前调查员移动到已知地点。"""

    kind: Literal["move_actor"]
    target_id: str = Field(min_length=1, max_length=100)


class InspectTarget(StrictCamelModel):
    """请求调查当前可见的地点或对象。"""

    kind: Literal["inspect_target"]
    target_id: str = Field(min_length=1, max_length=100)


class TalkToNpc(StrictCamelModel):
    """请求与当前可见 NPC 交谈。"""

    kind: Literal["talk_to_npc"]
    target_id: str = Field(min_length=1, max_length=100)
    topic: str = Field(default="", max_length=500)


class WaitUntil(StrictCamelModel):
    """请求把时间推进到模组允许的目标时间。"""

    kind: Literal["wait_until"]
    target_time: UtcDatetime


class StartCheck(StrictCamelModel):
    """请求建立一次服务端检定；客户端只能提供目标技能和难度，不能提供骰点。"""

    kind: Literal["start_check"]
    check_id: str = Field(min_length=1, max_length=100)
    skill_id: str = Field(min_length=1, max_length=100)
    goal: str = Field(min_length=1, max_length=500)
    difficulty: Literal["regular", "hard", "extreme"] = "regular"


class RollCheck(StrictCamelModel):
    """请求结算已建立的检定；骰点始终由 Kernel 生成。"""

    kind: Literal["roll_check"]
    check_id: str = Field(min_length=1, max_length=100)


Command = Annotated[
    MoveActor | InspectTarget | TalkToNpc | WaitUntil | StartCheck | RollCheck,
    Field(discriminator="kind"),
]
CommandAdapter = TypeAdapter(Command)


class CommandEnvelope(StrictCamelModel):
    """客户端或 Intent Interpreter 提交的幂等命令信封。"""

    schema_version: Literal[1] = 1
    client_request_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=0)
    actor_id: str = Field(min_length=1, max_length=100)
    command: Command


class DomainEventEnvelope(StrictCamelModel):
    """Kernel 提交给事件日志和 Narrator 的领域事件。"""

    schema_version: Literal[1] = 1
    event_id: str = Field(min_length=1, max_length=100)
    event_type: str = Field(min_length=1, max_length=100)
    actor_id: str = Field(min_length=1, max_length=100)
    visibility: Literal["public", "private", "hidden"] = "public"
    payload: dict[str, object]


class PlayerProjection(StrictCamelModel):
    """只包含当前玩家可见的稳定投影，不允许出现 keeper 字段。"""

    session_id: str
    actor_id: str
    revision: int = Field(ge=0)
    world_time: UtcDatetime
    location_id: str
    visible_facts: list[str]
    pending_command_id: str | None = None


class PendingDecision(StrictCamelModel):
    """玩家必须完成的 Kernel 决策点，例如投骰。"""

    decision_id: str
    kind: Literal["roll_check"]
    check_id: str
    options: list[str]


class CheckRead(StrictCamelModel):
    """对玩家公开的检定状态，不包含 keeper 过程数据。"""

    check_id: str
    skill_id: str
    difficulty: Literal["regular", "hard", "extreme"]
    status: Literal["awaiting_roll", "resolved"]
    roll: int | None = Field(default=None, ge=1, le=100)
    target_value: int = Field(ge=1, le=100)
    success: bool | None = None


class CommandResult(StrictCamelModel):
    """一次命令提交的确定性结果和最新玩家投影。"""

    schema_version: Literal[1] = 1
    client_request_id: str
    revision: int = Field(ge=0)
    events: list[DomainEventEnvelope]
    projection: PlayerProjection
    pending_decisions: list[PendingDecision] = Field(default_factory=list)
    check: CheckRead | None = None


class SessionCreateBody(StrictCamelModel):
    """创建新 GM 会话时冻结的房间、模组和调查员信息。"""

    room_id: str = Field(min_length=1, max_length=100)
    module_id: str = Field(min_length=1, max_length=200)
    actor_id: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)


class SessionRead(StrictCamelModel):
    """创建或重连后返回的玩家安全会话投影。"""

    session_id: str
    module_id: str
    module_version: str
    projection: PlayerProjection


class TurnState(StrictCamelModel):
    """回合生命周期的最小判别状态。"""

    value: Literal[
        "collecting",
        "understanding",
        "validating",
        "awaiting_clarification",
        "awaiting_roll",
        "resolving",
        "narrating",
        "completed",
        "failed",
    ]


class NarrationDraft(StrictCamelModel):
    """Narrator 生成的候选文本，必须引用已提交事件。"""

    text: str = Field(min_length=1, max_length=5000)
    evidence_event_ids: list[str] = Field(min_length=1)
