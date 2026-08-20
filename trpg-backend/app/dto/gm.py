"""Phase 0 AI 主持运行时的严格 DTO 契约。

模型只能生成这些候选命令，真正的状态变化仍由 Kernel 校验并提交。
"""

from __future__ import annotations

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


class ResolveCheck(StrictCamelModel):
    """提交检定后的选择；成功与状态变化仍由 Kernel 权威结算。"""

    kind: Literal["resolve_check"]
    check_id: str = Field(min_length=1, max_length=100)
    option: Literal["accept_failure", "spend_luck", "push"]
    revised_method: str | None = Field(default=None, max_length=500)


class ChooseOption(StrictCamelModel):
    """提交模组声明的不可逆剧情选择，不能由模型直接写结局。"""

    kind: Literal["choose_option"]
    option_id: str = Field(min_length=1, max_length=100)


Command = Annotated[
    MoveActor
    | InspectTarget
    | TalkToNpc
    | WaitUntil
    | StartCheck
    | RollCheck
    | ResolveCheck
    | ChooseOption,
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


class KnownLocationRead(StrictCamelModel):
    """玩家已经知道的地点，不包含未解锁路线或守秘信息。"""

    id: str
    label: str
    visited: bool = True


class PlayerProjection(StrictCamelModel):
    """只包含当前玩家可见的稳定投影，不允许出现 keeper 字段。"""

    session_id: str
    actor_id: str
    revision: int = Field(ge=0)
    world_time: UtcDatetime
    location_id: str
    visible_facts: list[str]
    pending_command_id: str | None = None
    pending_decisions: list[PendingDecision] = Field(default_factory=list)
    checks: list[CheckRead] = Field(default_factory=list)
    pending_clarification: ClarificationRead | None = None
    scene_id: str | None = None
    scene_label: str | None = None
    known_locations: list[KnownLocationRead] = Field(default_factory=list)
    clues: list[str] = Field(default_factory=list)
    hp: int | None = Field(default=None, ge=0)
    san: int | None = Field(default=None, ge=0)
    luck: int | None = Field(default=None, ge=0)
    major_wound: bool = False
    unconscious: bool = False
    temporary_insanity: bool = False
    encounter: EncounterRead | None = None
    ending_id: str | None = None


class PendingDecision(StrictCamelModel):
    """玩家必须完成的 Kernel 决策点，例如投骰。"""

    decision_id: str
    kind: Literal["roll_check", "roll_decision"]
    check_id: str
    options: list[str]


class CheckRead(StrictCamelModel):
    """对玩家公开的检定状态，不包含 keeper 过程数据。"""

    check_id: str
    skill_id: str
    skill_label: str | None = None
    difficulty: Literal["regular", "hard", "extreme"]
    status: Literal["awaiting_roll", "awaiting_roll_decision", "resolved"]
    roll: int | None = Field(default=None, ge=1, le=100)
    target_value: int = Field(ge=1, le=100)
    success: bool | None = None
    success_level: Literal["critical", "extreme", "hard", "regular", "failure", "fumble"] | None = (
        None
    )
    bonus_dice: int = Field(default=0, ge=-2, le=2)
    roll_values: list[int] = Field(default_factory=list)
    luck_spent: int = Field(default=0, ge=0)
    pushed: bool = False
    final_result: bool | None = None


class EncounterRead(StrictCamelModel):
    """玩家可见的最小战斗或追逐状态。"""

    encounter_id: str
    kind: Literal["combat", "chase"]
    status: Literal["active", "won", "lost", "escaped"]
    round: int = Field(ge=1)
    opponent_id: str | None = None
    opponent_hp: int | None = Field(default=None, ge=0)
    progress: int | None = Field(default=None, ge=0)
    distance: int | None = Field(default=None, ge=0)


class CommandResult(StrictCamelModel):
    """一次命令提交的确定性结果和最新玩家投影。"""

    schema_version: Literal[1] = 1
    client_request_id: str
    revision: int = Field(ge=0)
    events: list[DomainEventEnvelope]
    projection: PlayerProjection
    pending_decisions: list[PendingDecision] = Field(default_factory=list)
    check: CheckRead | None = None
    narration_facts: list[str] = Field(default_factory=list)
    narration: str | None = None


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
    opening_narration: str | None = None


class TurnState(StrictCamelModel):
    """回合生命周期的最小判别状态。"""

    value: Literal[
        "interpreting",
        "validating",
        "awaiting_clarification",
        "awaiting_roll",
        "awaiting_roll_decision",
        "resolving",
        "narrating",
        "publishing",
        "completed",
        "paused",
    ]


class ClarificationRead(StrictCamelModel):
    """刷新后可恢复的意图澄清问题和候选答案。"""

    client_request_id: str
    question: str
    options: list[str] = Field(default_factory=list)


class NarrationDraft(StrictCamelModel):
    """Narrator 生成的候选文本，必须引用已提交事件。"""

    text: str = Field(min_length=1, max_length=5000)
    evidence_event_ids: list[str] = Field(min_length=1)


class ActionCandidate(StrictCamelModel):
    """当前玩家可以安全尝试的动作候选；不包含成功后的隐藏结果。"""

    action: Literal[
        "move_actor",
        "inspect_target",
        "talk_to_npc",
        "wait_until",
        "start_check",
        "choose_option",
    ]
    target_id: str | None = None
    label: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list)
    # 检定技能来自会话冻结的模组，模型只能选择候选，不能自行决定技能。
    skill_id: str | None = None


class SourceFragmentRead(StrictCamelModel):
    """当前职责获准读取的一小段模组原文及其人工可追溯坐标。"""

    fragment_id: str
    content: str = Field(min_length=1, max_length=3000)
    source_refs: list[str] = Field(min_length=1)


class ContextSlice(StrictCamelModel):
    """由权威状态确定性选出的结构化模组数据和必要原文片段。"""

    structured_data: dict[str, object] = Field(default_factory=dict)
    source_fragments: list[SourceFragmentRead] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    visibility: Literal["player"] = "player"
    revision: int = Field(ge=0)


class RecentEvent(StrictCamelModel):
    """最近事件窗口中的玩家可见事件，不包含内部命令载荷。"""

    event_id: str
    event_type: str
    visible_content: str = Field(min_length=1, max_length=1000)


class DerivedMemory(StrictCamelModel):
    """从事件和权威状态派生的长期记忆，可按来源事件重新构建。"""

    content: str = Field(min_length=1, max_length=1000)
    source_event_ids: list[str] = Field(min_length=1)


class PromptPack(StrictCamelModel):
    """记录本次模型输入选择与裁剪结果，便于复现上下文。"""

    purpose: Literal["intent", "narration"]
    estimated_tokens: int = Field(ge=0)
    selected_ids: list[str] = Field(default_factory=list)
    trimmed_ids: list[str] = Field(default_factory=list)


class ContextSnapshot(StrictCamelModel):
    """一次模型调用的不可变安全快照，记录模型实际被允许看到的内容。"""

    snapshot_id: str
    session_id: str
    actor_id: str
    audience: str
    revision: int = Field(ge=0)
    world_time: UtcDatetime
    location_id: str
    visible_facts: list[str] = Field(default_factory=list)
    action_candidates: list[ActionCandidate] = Field(default_factory=list)
    recent_event_ids: list[str] = Field(default_factory=list)
    module_slice: ContextSlice | None = None
    recent_events: list[RecentEvent] = Field(default_factory=list)
    derived_memory: list[DerivedMemory] = Field(default_factory=list)
    prompt_pack: PromptPack | None = None


class IntentStep(StrictCamelModel):
    """意图解释器提出的单个有限动作，不是可直接执行的脚本。"""

    action: Literal[
        "move_actor",
        "inspect_target",
        "talk_to_npc",
        "wait_until",
        "start_check",
        "choose_option",
    ]
    target_id: str | None = None
    skill_id: str | None = None
    goal: str | None = None
    topic: str | None = None
    target_time: UtcDatetime | None = None


class IntentResult(StrictCamelModel):
    """模型对玩家自然语言的结构化理解，必须再经过确定性校验。"""

    kind: Literal["proposal", "clarification"]
    # summary 只用于调试，不参与权威执行；兼容模型在澄清时输出 null。
    summary: str | None = Field(default=None, min_length=1, max_length=500)
    steps: list[IntentStep] = Field(default_factory=list, max_length=4)
    clarification_question: str | None = None
    clarification_options: list[str] = Field(default_factory=list)
    source_revision: int = Field(ge=0)


class AdjudicationProposal(StrictCamelModel):
    """AI 对开放行动的最小裁决提案；不包含任何状态修改。"""

    decision: Literal["direct_success", "clarification", "start_check"]
    action_type: str = Field(min_length=1, max_length=100)
    target_id: str | None = Field(default=None, max_length=100)
    skill_id: str | None = Field(default=None, max_length=100)
    difficulty: Literal["regular", "hard", "extreme"] = "regular"
    estimated_minutes: int = Field(default=0, ge=0, le=1440)
    failure_consequence: str | None = Field(default=None, max_length=500)
    reason_refs: list[str] = Field(default_factory=list)
    proposal_revision: int = Field(ge=0)


class TurnInputBody(StrictCamelModel):
    """浏览器提交的一回合自然语言输入；服务端从认证会话取得玩家身份。"""

    client_request_id: str = Field(min_length=1, max_length=200)
    actor_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    input: str = Field(min_length=1, max_length=4000)


class GmTurnRead(StrictCamelModel):
    """AI 主持回合的公开结果，包含澄清、Kernel 回执或安全叙事。"""

    client_request_id: str
    status: Literal["clarification", "completed", "failed"]
    revision: int = Field(ge=0)
    clarification_question: str | None = None
    clarification_options: list[str] = Field(default_factory=list)
    narration: str | None = None
    command_result: CommandResult | None = None
