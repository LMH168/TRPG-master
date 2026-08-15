"""定义 Host 只能提出、不能直接授权执行的动作 Proposal 契约。"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, JsonValue, model_validator

from .adjudication import AdjudicationCheck, RuleDecisionRef
from .common import ContractModel


class ProposalRef(ContractModel):
    """引用当前 capability 中的对象，或本次 Proposal 内声明的运行时对象。"""

    kind: Literal[
        "information",
        "entity",
        "location",
        "actor",
        "world",
        "runtime_entity",
        "runtime_location",
    ]
    id: str = Field(min_length=1, max_length=200)


class RevealInformationEffectProposal(ContractModel):
    type: Literal["reveal_information"] = "reveal_information"
    information_ref: ProposalRef
    scope: Literal["party", "self"] = "party"


class HideInformationEffectProposal(ContractModel):
    type: Literal["hide_information"] = "hide_information"
    information_ref: ProposalRef
    scope: Literal["party", "self"] = "party"


class SetVisibilityEffectProposal(ContractModel):
    type: Literal["set_visibility"] = "set_visibility"
    target_ref: ProposalRef
    visible: bool
    scope: Literal["party", "self"] = "party"


class EnterLocationEffectProposal(ContractModel):
    type: Literal["enter_location"] = "enter_location"
    location_ref: ProposalRef


class EnsureRuntimeLocationEffectProposal(ContractModel):
    type: Literal["ensure_runtime_location"] = "ensure_runtime_location"
    runtime_ref: ProposalRef
    name: str = Field(min_length=1, max_length=200)
    parent_ref: ProposalRef | None = None
    connected_ref: ProposalRef

    @model_validator(mode="after")
    def require_runtime_location(self) -> EnsureRuntimeLocationEffectProposal:
        """运行时地点只能声明逻辑引用，不能伪装成 Canon 地点。"""

        if self.runtime_ref.kind != "runtime_location":
            raise ValueError("ensure_runtime_location 必须使用 runtime_location 引用")
        return self


class EnsureRuntimeEntityEffectProposal(ContractModel):
    type: Literal["ensure_runtime_entity"] = "ensure_runtime_entity"
    runtime_ref: ProposalRef
    entity_kind: Literal["npc", "object"]
    name: str = Field(min_length=1, max_length=200)
    location_ref: ProposalRef

    @model_validator(mode="after")
    def require_runtime_entity(self) -> EnsureRuntimeEntityEffectProposal:
        """运行时实体只能声明逻辑引用，最终 ID 由可信编译器派生。"""

        if self.runtime_ref.kind != "runtime_entity":
            raise ValueError("ensure_runtime_entity 必须使用 runtime_entity 引用")
        return self


class LocationDestinationProposal(ContractModel):
    kind: Literal["location"] = "location"
    location_ref: ProposalRef


class SelfInventoryDestinationProposal(ContractModel):
    kind: Literal["self_inventory"] = "self_inventory"


MoveDestinationProposal = Annotated[
    LocationDestinationProposal | SelfInventoryDestinationProposal,
    Field(discriminator="kind"),
]


class MoveEntityEffectProposal(ContractModel):
    type: Literal["move_entity"] = "move_entity"
    entity_ref: ProposalRef
    destination: MoveDestinationProposal


class ChangeEntityStateEffectProposal(ContractModel):
    type: Literal["change_entity_state"] = "change_entity_state"
    entity_ref: ProposalRef
    key: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    value: JsonValue


class ChangeItemConditionEffectProposal(ContractModel):
    """提议修改 ItemInstance 的公开 condition，例如将手枪标记为 empty。"""

    type: Literal["change_item_condition"] = "change_item_condition"
    entity_ref: ProposalRef
    condition: str = Field(min_length=1, max_length=100)


class ConsumeEntityEffectProposal(ContractModel):
    type: Literal["consume_entity"] = "consume_entity"
    entity_ref: ProposalRef


class MarkCoreResolvedEffectProposal(ContractModel):
    type: Literal["mark_core_resolved"] = "mark_core_resolved"


class SetEndingAvailabilityEffectProposal(ContractModel):
    type: Literal["set_ending_availability"] = "set_ending_availability"
    available: bool


class CommitTerminalEndingEffectProposal(ContractModel):
    type: Literal["commit_terminal_ending"] = "commit_terminal_ending"
    ending_id: str = Field(min_length=1, max_length=200)


class AdvanceWorldTimeEffectProposal(ContractModel):
    type: Literal["advance_world_time"] = "advance_world_time"
    to_point_id: str | None = Field(default=None, min_length=1, max_length=200)


class NarrativeOnlyEffectProposal(ContractModel):
    type: Literal["narrative_only"] = "narrative_only"


EffectProposal = Annotated[
    RevealInformationEffectProposal
    | HideInformationEffectProposal
    | SetVisibilityEffectProposal
    | EnterLocationEffectProposal
    | EnsureRuntimeLocationEffectProposal
    | EnsureRuntimeEntityEffectProposal
    | MoveEntityEffectProposal
    | ChangeEntityStateEffectProposal
    | ChangeItemConditionEffectProposal
    | ConsumeEntityEffectProposal
    | MarkCoreResolvedEffectProposal
    | SetEndingAvailabilityEffectProposal
    | CommitTerminalEndingEffectProposal
    | AdvanceWorldTimeEffectProposal
    | NarrativeOnlyEffectProposal,
    Field(discriminator="type"),
]


class ProcessGoalCompletionProposal(ContractModel):
    """描述不要求持久写入的过程目标，并声明目标是否必须能够回应。"""

    kind: Literal["process"] = "process"
    interaction: Literal["observe", "social", "physical", "other"]


class EffectsGoalCompletionProposal(ContractModel):
    """列出实现玩家目标所必需的后置条件，独立于实际提交 Effect。"""

    kind: Literal["effects"] = "effects"
    requirements: tuple[EffectProposal, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def reject_non_terminal_requirements(self) -> EffectsGoalCompletionProposal:
        """完成条件只能描述最终事实，不能包含创建辅助步骤或纯叙事。"""

        forbidden = (
            EnsureRuntimeLocationEffectProposal,
            EnsureRuntimeEntityEffectProposal,
            NarrativeOnlyEffectProposal,
        )
        if any(isinstance(item, forbidden) for item in self.requirements):
            raise ValueError("目标完成条件只能包含可验证的最终结果")
        return self


GoalCompletionProposal: TypeAlias = Annotated[  # noqa: UP040
    ProcessGoalCompletionProposal | EffectsGoalCompletionProposal,
    Field(discriminator="kind"),
]


class ClarificationProposal(ContractModel):
    """真实歧义无法安全编译时，Host 返回给玩家的最小澄清。"""

    kind: Literal["clarification"] = "clarification"
    schema_version: Literal[1] = 1
    reason_code: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=1000)


class SingleActionProposal(ContractModel):
    """不可信的单动作语义；其中任何字段都不代表提交授权。"""

    kind: Literal["single_action"] = "single_action"
    schema_version: Literal[1, 2] = 1
    semantic_goal: str = Field(min_length=1, max_length=2000)
    semantic_focus: ProposalRef
    anchor_ref: ProposalRef | None = None
    method_family: str = Field(min_length=1, max_length=100)
    method_description: str = Field(min_length=1, max_length=1000)
    check_proposal: AdjudicationCheck
    rule_ref: RuleDecisionRef | None = None
    success_effect_proposals: tuple[EffectProposal, ...] = ()
    failure_effect_proposals: tuple[EffectProposal, ...] = ()
    completion: GoalCompletionProposal | None = None

    @model_validator(mode="after")
    def validate_versioned_completion(self) -> SingleActionProposal:
        """v2 必须显式声明完成条件，v1 仅供历史记录读取。"""

        if self.schema_version == 2 and self.completion is None:
            raise ValueError("SingleActionProposal v2 必须包含 completion")
        if self.schema_version == 1 and self.completion is not None:
            raise ValueError("SingleActionProposal v1 不得包含 completion")
        return self


class SubmitProposalRequest(ContractModel):
    """由 Turn Coordinator 构造的可信信封；Host 只能提供其中的 Proposal。"""

    request_id: str = Field(min_length=1, max_length=200)
    room_id: str = Field(min_length=1, max_length=200)
    player_id: str = Field(min_length=1, max_length=200)
    actor_id: str = Field(min_length=1, max_length=200)
    source_revision: str = Field(min_length=1, max_length=200)
    proposal: SingleActionProposal
    requested_goal: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def require_trusted_goal_for_v2(self) -> SubmitProposalRequest:
        """新 writer 必须携带 Coordinator 冻结的目标，旧请求继续显式兼容。"""

        if self.proposal.schema_version == 2 and self.requested_goal is None:
            raise ValueError("Proposal v2 必须绑定 requested_goal")
        return self


class ActionPlanProposalStep(ContractModel):
    """计划阶段只冻结玩家安全语义，不提前生成未来步骤的 Effect。"""

    semantic_goal: str = Field(min_length=1, max_length=1000)
    public_progress_label: str | None = Field(
        default=None, min_length=1, max_length=200
    )


class ActionPlanProposal(ContractModel):
    kind: Literal["action_plan"] = "action_plan"
    schema_version: Literal[1] = 1
    semantic_goal: str = Field(min_length=1, max_length=2000)
    steps: tuple[ActionPlanProposalStep, ...] = Field(min_length=2)


HostDecisionProposal: TypeAlias = Annotated[  # noqa: UP040
    ClarificationProposal | SingleActionProposal | ActionPlanProposal,
    Field(discriminator="kind"),
]
