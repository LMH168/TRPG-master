"""在 Engine 当前 revision 下把不可信 Proposal 编译为短生命周期内部命令。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NoReturn

from collaboration_framework.contracts import (
    ActionAdjudication,
    ActionEffect,
    ActionMethod,
    ActionTarget,
    AdjudicationValidationError,
    AdvanceWorldTimeEffect,
    ChangeEntityStateEffect,
    ChangeItemConditionEffect,
    CommitTerminalEndingEffect,
    ConsumeEntityEffect,
    EnsureRuntimeEntityEffect,
    EnsureRuntimeLocationEffect,
    EnterLocationEffect,
    HideInformationEffect,
    MarkCoreResolvedEffect,
    MoveEntityEffect,
    NarrativeOnlyEffect,
    NoAdjudicationCheck,
    PersistenceIntent,
    ProposalRef,
    RequiredAdjudicationCheck,
    RevealInformationEffect,
    RuleDecisionRef,
    SetEndingAvailabilityEffect,
    SetVisibilityEffect,
    SingleActionProposal,
    SkillCheckCandidate,
    SubmitProposalRequest,
    ValidationResult,
)
from collaboration_framework.contracts.module_v3 import (
    AdjudicatedCheckStep,
    AgentMatchTriggerSpec,
    CheckStep,
    RuleSpecV3,
)
from collaboration_framework.contracts.proposal import (
    AdvanceWorldTimeEffectProposal,
    ChangeEntityStateEffectProposal,
    ChangeItemConditionEffectProposal,
    CommitTerminalEndingEffectProposal,
    ConsumeEntityEffectProposal,
    EffectProposal,
    EffectsGoalCompletionProposal,
    EnsureRuntimeEntityEffectProposal,
    EnsureRuntimeLocationEffectProposal,
    EnterLocationEffectProposal,
    HideInformationEffectProposal,
    ItemActionMeansProposal,
    MarkCoreResolvedEffectProposal,
    MoveEntityEffectProposal,
    NarrativeOnlyEffectProposal,
    ProcessGoalCompletionProposal,
    RevealInformationEffectProposal,
    SetEndingAvailabilityEffectProposal,
    SetVisibilityEffectProposal,
)
from collaboration_framework.contracts.validation import Repairability, ValidationFault

from .models import EngineRuntimeSnapshot, ValidatedActionCommand
from .persistent_results import validate_persistent_effects
from .rules_v3 import (
    agent_match_scope_admits,
    pending_check_for,
    resolve_rule_option,
)

_HOST_FORBIDDEN_EFFECTS = (
    MarkCoreResolvedEffectProposal,
    SetEndingAvailabilityEffectProposal,
    CommitTerminalEndingEffectProposal,
)


def derive_runtime_object_id(*, room_id: str, request_id: str, ref: ProposalRef) -> str:
    """由可信回合身份和逻辑引用派生稳定 ID，供恢复与规范化代码复用。"""

    digest = hashlib.sha256(
        f"{room_id}\0{request_id}\0{ref.kind}\0{ref.id}".encode()
    ).hexdigest()[:20]
    prefix = "location" if ref.kind == "runtime_location" else "entity"
    return f"runtime-{prefix}-{digest}"


@dataclass(frozen=True)
class ProposalShadowComparison:
    """纯比较结果；只含机器结论，不保存玩家原话、Prompt 或隐藏上下文。"""

    matches: bool
    proposal_fingerprint: str
    differing_fields: tuple[str, ...]


class ProposalShadowCompiler:
    """并排比较新编译结果与 legacy 裁决，且不持有任何写端口。"""

    def __init__(self, compiler: ProposalCompiler | None = None) -> None:
        self._compiler = compiler or ProposalCompiler()

    def compare(
        self,
        runtime: EngineRuntimeSnapshot,
        request: SubmitProposalRequest,
        legacy: ActionAdjudication,
    ) -> ProposalShadowComparison:
        """只执行确定性编译和字段比较，不调用 Engine、骰点或叙事设施。"""

        compiled = self._compiler.compile(runtime, request)
        candidate = compiled.adjudication
        fields = (
            "summary",
            "target",
            "method",
            "check",
            "rule_decision",
            "success_effects",
            "failure_effects",
        )
        differing = tuple(
            field
            for field in fields
            if getattr(candidate, field) != getattr(legacy, field)
        )
        return ProposalShadowComparison(
            matches=not differing,
            proposal_fingerprint=compiled.proposal_fingerprint,
            differing_fields=differing,
        )


class ProposalCompiler:
    """解析引用并集中执行 Host Proposal 的确定性权限预检。"""

    def compile(
        self,
        runtime: EngineRuntimeSnapshot,
        request: SubmitProposalRequest,
    ) -> ValidatedActionCommand:
        """绑定可信身份与 revision，并产出只能立即交给 Engine 的内部命令。"""

        self._require_trusted_context(runtime, request)
        proposal = request.proposal
        if (
            proposal.schema_version == 2
            and proposal.semantic_goal != request.requested_goal
        ):
            self._reject(
                "PROPOSAL_SEMANTIC_GOAL_CHANGED",
                "动作提议改变了玩家目标，请重新确认行动",
                repairability="requires_player_choice",
            )
        self._reject_forbidden_host_effects(
            (*proposal.success_effect_proposals, *proposal.failure_effect_proposals)
        )

        runtime_refs: dict[tuple[str, str], str] = {}
        success = self._compile_effects(
            request, proposal.success_effect_proposals, runtime_refs
        )
        failure = self._compile_effects(
            request, proposal.failure_effect_proposals, runtime_refs
        )
        if proposal.schema_version == 2 and proposal.rule_ref is None:
            # Host 只能移动当前行动者实际可接触的物品。这里按 Effect 顺序模拟
            # custody，既允许“创建当前地点的普通物品后立即拾取”，也阻止模型
            # 利用 Keeper capability 中的远端 ID 把物品隔空取回背包。
            self._validate_host_item_custody_sequence(runtime, request, success)
            self._validate_host_item_custody_sequence(runtime, request, failure)
        if proposal.schema_version == 2:
            self._validate_execution_means(runtime, request)
        completion_mode: str = "legacy"
        process_interaction = None
        completion_requirements: tuple[ActionEffect, ...] = ()
        if isinstance(proposal.completion, ProcessGoalCompletionProposal):
            completion_mode = "process"
            process_interaction = proposal.completion.interaction
        elif isinstance(proposal.completion, EffectsGoalCompletionProposal):
            completion_mode = "effects"
            completion_requirements = self._compile_effects(
                request, proposal.completion.requirements, runtime_refs
            )
        focus = self._resolve_focus(runtime, proposal, runtime_refs)
        rule_decision, check = self._bind_rule_and_check(runtime, request, focus)
        if rule_decision is not None and (
            proposal.success_effect_proposals or proposal.failure_effect_proposals
        ):
            self._reject(
                "RULE_EFFECT_OVERRIDE",
                "规则已经拥有结果，不能同时提交 Host 结果",
            )
        if rule_decision is not None and proposal.target_interaction is None:
            # 规则可以拥有最终 Effect，但不能替 Host 补猜它如何作用于目标。
            self._reject(
                "TARGET_INTERACTION_REQUIRED",
                "当前行动缺少目标交互类型，请重新确认行动",
            )
        if (
            rule_decision is None
            and completion_mode == "process"
            and process_interaction == "other"
            and focus.kind == "actor"
            and focus.id == request.actor_id
            and check.mode != "none"
            and not success
            and not failure
        ):
            # 单独报出技能名不构成行动目标；继续掷骰只会得到无法授权任何结果的
            # 空成功。要求玩家先说明想观察、影响或完成什么。
            self._reject(
                "CHECK_GOAL_REQUIRED",
                "请先说明这次检定想达成什么目标",
                repairability="requires_player_choice",
                fault="player",
            )
        adjudication = ActionAdjudication(
            request_id=request.request_id,
            source_revision=request.source_revision,
            actor_id=request.actor_id,
            summary=proposal.semantic_goal,
            target=focus,
            method=ActionMethod(
                family=proposal.method_family,
                description=proposal.method_description,
            ),
            persistence_intent=self._derive_persistence_intent(
                proposal.method_family, success, failure
            ),
            check=check,
            rule_decision=rule_decision,
            success_effects=success,
            failure_effects=failure,
        )
        if proposal.schema_version == 2:
            self._validate_requirement_permissions(runtime, request, success)
            self._validate_requirement_permissions(runtime, request, failure)
            self._validate_completion_contract(
                runtime,
                request,
                target_id=focus.id,
                completion_mode=completion_mode,
                process_interaction=process_interaction,
                requirements=completion_requirements,
                success_effects=success,
                rule_owned=rule_decision is not None,
            )
        # v1 仅供历史读取，继续沿用原完整性规则；生产 writer 不再依赖 family 词表。
        elif proposal.rule_ref is None:
            problem = validate_persistent_effects(adjudication)
            if problem is not None:
                self._reject(problem.code, problem.player_safe_reason)
        fingerprint = hashlib.sha256(
            json.dumps(
                proposal.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return ValidatedActionCommand(
            schema_version=2 if proposal.schema_version == 2 else 1,
            request=request,
            proposal_fingerprint=fingerprint,
            adjudication=adjudication,
            validation=ValidationResult(
                status="accepted",
                authority_level=None,
                code="OK",
                player_safe_reason="动作提议已通过权威编译",
            ),
            completion_mode=completion_mode,
            process_interaction=process_interaction,
            completion_requirements=completion_requirements,
        )

    def _bind_rule_and_check(
        self,
        runtime: EngineRuntimeSnapshot,
        request: SubmitProposalRequest,
        focus: ActionTarget,
    ):
        """绑定唯一必选 Rule，并从固定规则图编译权威检定。"""

        proposal = request.proposal
        requested_goal = request.requested_goal or proposal.semantic_goal
        if not runtime.is_v3:
            return proposal.rule_ref, proposal.check_proposal
        scoped_required = tuple(
            rule
            for rule in runtime.v3.rules
            if isinstance(rule.trigger, AgentMatchTriggerSpec)
            and rule.trigger.required
            and agent_match_scope_admits(
                rule,
                location_id=runtime.game_state.scene_id,
                action_family=proposal.method_family,
                target_kind=focus.kind,
                target_id=focus.id,
            )
        )
        matching = tuple(
            rule
            for rule in scoped_required
            if agent_match_scope_admits(
                rule,
                location_id=runtime.game_state.scene_id,
                state=runtime.game_state,
                actor_id=request.actor_id,
                action_family=proposal.method_family,
                target_kind=focus.kind,
                target_id=focus.id,
            )
        )
        decision = proposal.rule_ref
        auto_bound = decision is None
        if decision is None:
            if not matching and scoped_required:
                # required Rule 的 when 是动作前置条件，不是可绕过的 Prompt 过滤器。
                # 即使 Host 没看到或省略 rule_ref，也不能退化成普通过程成功。
                self._reject(
                    "RULE_PRECONDITION_UNMET",
                    "当前条件尚不允许执行这项行动",
                    repairability="requires_player_choice",
                    fault="player",
                )
            if len(matching) > 1:
                self._reject(
                    "RULE_SELECTION_AMBIGUOUS",
                    "当前行动同时匹配多个规则，请进一步说明行动方式",
                    repairability="requires_player_choice",
                    fault="player",
                )
            if len(matching) == 1:
                rule = matching[0]
                trigger = rule.trigger
                assert isinstance(trigger, AgentMatchTriggerSpec)
                options = trigger.options
                if len(options) == 1:
                    decision = RuleDecisionRef(
                        rule_id=rule.id,
                        option_id=options[0].id,
                    )
                else:
                    decision = self._decision_from_player_words(
                        requested_goal,
                        rule,
                    )
                    if decision is None:
                        self._reject(
                            "RULE_OPTION_REQUIRED",
                            "请明确选择一种处理方式："
                            + " / ".join(
                                option.semantic_hints[0] for option in options
                            ),
                            repairability="requires_player_choice",
                            fault="player",
                        )
        if decision is None:
            return None, proposal.check_proposal
        rule, option_id = resolve_rule_option(
            runtime.v3,
            rule_id=decision.rule_id,
            option_id=decision.option_id,
        )
        trigger = rule.trigger
        if not isinstance(trigger, AgentMatchTriggerSpec):
            self._reject("RULE_OUT_OF_SCOPE", "当前行动不能使用该规则选项")
        if not agent_match_scope_admits(
            rule,
            location_id=runtime.game_state.scene_id,
            state=runtime.game_state,
            actor_id=request.actor_id,
            action_family=proposal.method_family,
            target_kind=focus.kind,
            target_id=focus.id,
        ):
            self._reject(
                "RULE_OUT_OF_SCOPE",
                "当前行动不能使用该规则选项",
                repairability="auto_repairable",
                fault="agent",
            )
        if (
            len(trigger.options) > 1
            and self._decision_from_player_words(
                requested_goal,
                rule,
            )
            != decision
        ):
            self._reject(
                "RULE_OPTION_UNCONFIRMED",
                "请明确选择一种处理方式："
                + " / ".join(option.semantic_hints[0] for option in trigger.options),
                repairability="requires_player_choice",
                fault="player",
            )
        check_step, _ = pending_check_for(rule, option_id)
        if check_step is None:
            canonical_check = NoAdjudicationCheck()
        elif isinstance(check_step, CheckStep):
            if check_step.check.initiation_kind != "active_action":
                canonical_check = NoAdjudicationCheck()
            else:
                skill_id = check_step.check.parameters.get("skill_id")
                if not isinstance(skill_id, str) or not skill_id:
                    self._reject("RULE_CHECK_INVALID", "模组规则缺少可执行的检定技能")
                canonical_check = RequiredAdjudicationCheck(
                    candidates=(
                        SkillCheckCandidate(
                            candidate_id=option_id,
                            skill_id=skill_id,
                            difficulty=check_step.check.difficulty,
                            method_summary=requested_goal,
                            player_safe_reason="使用模组规则声明的检定方式",
                        ),
                    )
                )
        elif isinstance(check_step, AdjudicatedCheckStep):
            canonical_check = proposal.check_proposal
        else:
            canonical_check = NoAdjudicationCheck()
        if not (
            auto_bound
            and proposal.check_proposal.mode == "none"
            and canonical_check.mode == "required"
        ) and not self._check_shape_matches(proposal.check_proposal, canonical_check):
            # 自动绑定时允许 Host 省略未知的 Rule-owned check；一旦 Host 主动
            # 提供技能，仍必须与固定 ModuleVersion 完全一致。
            self._reject(
                "RULE_CHECK_MISMATCH",
                "当前检定方式与模组规则不一致",
                repairability="auto_repairable",
                fault="agent",
            )
        return decision, canonical_check

    @staticmethod
    def _decision_from_player_words(
        goal: str, rule: RuleSpecV3
    ) -> RuleDecisionRef | None:
        """只用模组作者发布的提示确认多分支选择，不解释任意动作词表。"""

        trigger = rule.trigger
        if not isinstance(trigger, AgentMatchTriggerSpec):
            return None
        normalized = "".join(goal.casefold().split())
        matched = [
            option
            for option in trigger.options
            if any(
                "".join(hint.casefold().split()) in normalized
                for hint in option.semantic_hints
            )
        ]
        if len(matched) != 1:
            return None
        return RuleDecisionRef(rule_id=rule.id, option_id=matched[0].id)

    @staticmethod
    def _check_shape_matches(proposed, canonical) -> bool:
        """忽略不可信 candidate ID，但技能、难度与是否掷骰必须一致。"""

        if proposed.mode != canonical.mode:
            return False
        if proposed.mode == "none":
            return True
        return tuple(
            (item.skill_id, item.difficulty) for item in proposed.candidates
        ) == tuple((item.skill_id, item.difficulty) for item in canonical.candidates)

    def _validate_completion_contract(
        self,
        runtime: EngineRuntimeSnapshot,
        request: SubmitProposalRequest,
        *,
        target_id: str,
        completion_mode: str,
        process_interaction: str | None,
        requirements: tuple[ActionEffect, ...],
        success_effects: tuple[ActionEffect, ...],
        rule_owned: bool,
    ) -> None:
        """校验目标完成条件、成功 Effect 和当前权威状态之间的一致性。"""

        # target_interaction 独立于完成条件：规则动作即使声明 effects，也必须先
        # 满足目标当前能够参与该交互的前置条件。
        interaction = request.proposal.target_interaction or process_interaction
        if interaction == "social":
            state = runtime.game_state.entities.get(target_id, {})
            if state.get("consciousness") in {"dead", "unconscious"}:
                self._reject(
                    "TARGET_NOT_RESPONSIVE",
                    "目标当前无法回应这次交互",
                    repairability="requires_player_choice",
                    fault="player",
                )

        if completion_mode == "process":
            if any(
                not isinstance(item, NarrativeOnlyEffect) for item in success_effects
            ):
                self._reject("GOAL_EFFECT_MISMATCH", "过程行动不能夹带未声明的持久结果")
            return
        if completion_mode != "effects":
            self._reject("GOAL_COMPLETION_REQUIRED", "新动作必须声明目标完成条件")
        self._validate_requirement_permissions(runtime, request, requirements)
        if rule_owned:
            return
        missing = tuple(item for item in requirements if item not in success_effects)
        if missing:
            self._reject(
                "GOAL_EFFECT_MISMATCH",
                "成功结果不足以完成玩家声明的目标",
            )
        supporting = (
            NarrativeOnlyEffect,
            EnsureRuntimeEntityEffect,
            EnsureRuntimeLocationEffect,
        )
        undeclared = tuple(
            item
            for item in success_effects
            if not isinstance(item, supporting) and item not in requirements
        )
        if undeclared:
            self._reject("GOAL_EFFECT_MISMATCH", "成功分支包含未声明的持久结果")

    def _validate_requirement_permissions(
        self,
        runtime: EngineRuntimeSnapshot,
        request: SubmitProposalRequest,
        requirements: tuple[ActionEffect, ...],
    ) -> None:
        """限制 AI 可裁量的高权威结果，并阻止物品或死亡状态绕过前置条件。"""

        for requirement in requirements:
            if isinstance(
                requirement,
                ChangeEntityStateEffect | ChangeItemConditionEffect | MoveEntityEffect,
            ) and self._terminal_requirement_is_satisfied(
                requirement, runtime, actor_id=request.actor_id
            ):
                continue
            if isinstance(requirement, ChangeEntityStateEffect):
                allowed = {
                    "consciousness": {"conscious", "unconscious", "dead"},
                    "posture": {"standing", "prone"},
                    "restraint": {"free", "restrained"},
                    "injury": {"none", "minor", "major", "critical"},
                    "open": {True, False},
                    "locked": {True, False},
                    "broken": {True, False},
                }
                if (
                    requirement.key not in allowed
                    or requirement.value not in allowed[requirement.key]
                ):
                    self._reject("AUTHORITY_NOT_GRANTED", "该状态不在 AI 可裁量范围内")
                current = runtime.game_state.entities.get(requirement.entity_id, {})
                if current.get("consciousness") == "dead" and not (
                    requirement.key == "consciousness" and requirement.value == "dead"
                ):
                    self._reject(
                        "TERMINAL_STATE_CONFLICT", "死亡状态只能由明确模组规则改变"
                    )
                severe = (
                    requirement.key == "consciousness"
                    and requirement.value
                    in {
                        "dead",
                        "unconscious",
                    }
                    or requirement.key == "injury"
                    and requirement.value in {"major", "critical"}
                )
                if severe and request.proposal.check_proposal.mode == "none":
                    self._reject("CHECK_REQUIRED_FOR_L3", "该结果必须先通过一次检定")
                if requirement.key == "consciousness" and requirement.value == "dead":
                    self._require_visible_npc(runtime, requirement.entity_id)
            elif isinstance(requirement, ChangeItemConditionEffect):
                item = runtime.game_state.item_instances.get(requirement.entity_id)
                if (
                    item is None
                    or item.custody.kind != "actor_inventory"
                    or item.custody.ref_id != request.actor_id
                ):
                    self._reject("ITEM_NOT_OWNED", "只能改变当前角色持有物品的状态")
            elif isinstance(requirement, MoveEntityEffect):
                item = runtime.game_state.item_instances.get(requirement.entity_id)
                if item is None:
                    continue
                if requirement.location_id is not None:
                    if (
                        item.custody.kind != "actor_inventory"
                        or item.custody.ref_id != request.actor_id
                    ):
                        self._reject("ITEM_NOT_OWNED", "只能丢下当前角色实际持有的物品")
                    if requirement.location_id != runtime.game_state.scene_id:
                        self._reject(
                            "DROP_LOCATION_MISMATCH", "丢弃物品只能落在当前位置"
                        )
                elif requirement.holder_actor_id is not None and not (
                    item.custody.kind == "location"
                    and item.custody.ref_id == runtime.game_state.scene_id
                ):
                    self._reject(
                        "ITEM_NOT_AT_CURRENT_LOCATION",
                        "物品不在当前位置，无法取得",
                        repairability="requires_player_choice",
                        fault="player",
                    )

    def _validate_host_item_custody_sequence(
        self,
        runtime: EngineRuntimeSnapshot,
        request: SubmitProposalRequest,
        effects: tuple[ActionEffect, ...],
    ) -> None:
        """按顺序校验 Host 物品移动，防止跨地点或跨角色转移 custody。"""

        state = runtime.game_state
        custodies = {
            item_id: (item.custody.kind, item.custody.ref_id)
            for item_id, item in state.item_instances.items()
            if item.state.status == "active"
        }
        for effect in effects:
            if (
                isinstance(effect, EnsureRuntimeEntityEffect)
                and effect.entity_kind == "object"
            ):
                custodies[effect.entity_id] = ("location", effect.location_id)
                continue
            if not isinstance(effect, MoveEntityEffect):
                continue
            source = custodies.get(effect.entity_id)
            if source is None:
                # 非 ItemInstance 的普通实体继续由 Engine 的可携带类型门禁处理。
                continue
            if effect.holder_actor_id is not None:
                if effect.holder_actor_id != request.actor_id:
                    self._reject("ITEM_NOT_OWNED", "不能把物品放入其他角色的背包")
                if source not in {
                    ("actor_inventory", request.actor_id),
                    ("location", state.scene_id),
                }:
                    self._reject(
                        "ITEM_NOT_AT_CURRENT_LOCATION",
                        "物品不在当前位置，无法取得",
                        repairability="requires_player_choice",
                        fault="player",
                    )
                custodies[effect.entity_id] = (
                    "actor_inventory",
                    request.actor_id,
                )
                continue
            if source == ("location", effect.location_id):
                continue
            if source != ("actor_inventory", request.actor_id):
                self._reject("ITEM_NOT_OWNED", "只能丢下当前角色实际持有的物品")
            if effect.location_id != state.scene_id:
                self._reject("DROP_LOCATION_MISMATCH", "丢弃物品只能落在当前位置")
            custodies[effect.entity_id] = ("location", state.scene_id)

    def _validate_execution_means(
        self,
        runtime: EngineRuntimeSnapshot,
        request: SubmitProposalRequest,
    ) -> None:
        """在掷骰前校验结构化实施手段，不解释或枚举玩家自然语言。"""

        means = request.proposal.execution_means
        if means is None:
            # 旧持久化 Proposal 允许读取，但不能以缺失实施手段的新请求进入 Engine。
            self._reject(
                "ACTION_MEANS_REQUIRED",
                "这次行动缺少可确认的实施方式，请重新提交",
                repairability="requires_player_choice",
                fault="player",
            )
        if not isinstance(means, ItemActionMeansProposal):
            return
        item = runtime.game_state.item_instances.get(means.item_ref.id)
        if (
            item is None
            or item.state.status != "active"
            or item.custody.kind != "actor_inventory"
            or item.custody.ref_id != request.actor_id
        ):
            self._reject(
                "ACTION_RESOURCE_NOT_HELD",
                "执行这次行动所需的物品不在当前角色身上",
                repairability="requires_player_choice",
                fault="player",
            )

    @staticmethod
    def _terminal_requirement_is_satisfied(
        requirement: ActionEffect,
        runtime: EngineRuntimeSnapshot,
        *,
        actor_id: str,
    ) -> bool:
        """在编译期识别已满足终态，避免重复命令产生第二次状态写入。"""

        state = runtime.game_state
        if isinstance(requirement, ChangeEntityStateEffect):
            return (
                state.entities.get(requirement.entity_id, {}).get(requirement.key)
                == requirement.value
                or state.runtime_entities.get(requirement.entity_id, {}).get(
                    requirement.key
                )
                == requirement.value
            )
        if isinstance(requirement, ChangeItemConditionEffect):
            item = state.item_instances.get(requirement.entity_id)
            return item is not None and item.state.condition == requirement.condition
        if isinstance(requirement, MoveEntityEffect):
            item = state.item_instances.get(requirement.entity_id)
            if item is not None:
                return (
                    requirement.holder_actor_id == actor_id
                    and item.custody.kind == "actor_inventory"
                    and item.custody.ref_id == actor_id
                ) or (
                    requirement.location_id is not None
                    and item.custody.kind == "location"
                    and item.custody.ref_id == requirement.location_id
                )
            # Canon 实体的位置变化写入 entities 覆盖层；动态实体则写入
            # runtime_entities。编译期幂等判断必须和提交后完成判断保持一致。
            payload = state.runtime_entities.get(
                requirement.entity_id
            ) or state.entities.get(requirement.entity_id, {})
            return (
                payload.get("holder_actor_id") == requirement.holder_actor_id
                and payload.get("location_id") == requirement.location_id
            )
        return False

    @staticmethod
    def _require_visible_npc(runtime: EngineRuntimeSnapshot, entity_id: str) -> None:
        """致命 AI 裁决只允许作用于当前场景中玩家可见的 NPC。"""

        if runtime.is_v3:
            spec = next(
                (item for item in runtime.v3.entities if item.id == entity_id), None
            )
            if spec is not None:
                override = runtime.game_state.entities.get(entity_id, {})
                location_id = override.get("location_id", spec.located_in)
                if (
                    spec.kind == "npc"
                    and spec.visibility in {"public", "party", "actor"}
                    and location_id == runtime.game_state.scene_id
                ):
                    return
        runtime_entity = runtime.game_state.runtime_entities.get(entity_id)
        if (
            runtime_entity is not None
            and runtime_entity.get("kind") == "npc"
            and runtime_entity.get("location_id") == runtime.game_state.scene_id
        ):
            return
        ProposalCompiler._reject(
            "AUTHORITY_NOT_GRANTED", "只能裁决当前可见 NPC 的致命结果"
        )

    @staticmethod
    def _require_trusted_context(
        runtime: EngineRuntimeSnapshot,
        request: SubmitProposalRequest,
    ) -> None:
        """可信信封仍需与事务内 runtime 对账，不能把调用方身份当成授权。"""

        if runtime.game_state.room_id != request.room_id:
            ProposalCompiler._reject("ROOM_MISMATCH", "动作不属于当前房间")
        actor = runtime.game_state.actors.get(request.actor_id)
        if actor is None or actor.player_id != request.player_id:
            ProposalCompiler._reject("ACTOR_NOT_OWNED", "当前角色不可用于该动作")
        if runtime.revision != request.source_revision:
            ProposalCompiler._reject(
                "SOURCE_REVISION_STALE",
                "动作基于过期视图，请刷新后重试",
                repairability="retry_with_latest_revision",
                fault="player",
            )

    @staticmethod
    def _reject_forbidden_host_effects(effects: Iterable[EffectProposal]) -> None:
        for effect in effects:
            if isinstance(effect, _HOST_FORBIDDEN_EFFECTS):
                ProposalCompiler._reject(
                    "AUTHORITY_NOT_GRANTED",
                    "该持久结果需要模组规则授权",
                )

    def _compile_effects(
        self,
        request: SubmitProposalRequest,
        effects: tuple[EffectProposal, ...],
        runtime_refs: dict[tuple[str, str], str],
    ) -> tuple[ActionEffect, ...]:
        """按顺序扩展本命令引用集合，使创建后使用保持原子且可审计。"""

        compiled: list[ActionEffect] = []
        for effect in effects:
            if isinstance(effect, EnsureRuntimeLocationEffectProposal):
                location_id = self._declare_runtime_ref(
                    request, effect.runtime_ref, runtime_refs
                )
                compiled.append(
                    EnsureRuntimeLocationEffect(
                        location_id=location_id,
                        name=effect.name,
                        parent_location_id=(
                            self._resolve_ref(effect.parent_ref, runtime_refs)
                            if effect.parent_ref is not None
                            else None
                        ),
                        connected_location_id=self._resolve_ref(
                            effect.connected_ref, runtime_refs
                        ),
                    )
                )
            elif isinstance(effect, EnsureRuntimeEntityEffectProposal):
                entity_id = self._declare_runtime_ref(
                    request, effect.runtime_ref, runtime_refs
                )
                compiled.append(
                    EnsureRuntimeEntityEffect(
                        entity_id=entity_id,
                        entity_kind=effect.entity_kind,
                        name=effect.name,
                        location_id=self._resolve_ref(
                            effect.location_ref, runtime_refs
                        ),
                    )
                )
            else:
                compiled.append(self._compile_effect(request, effect, runtime_refs))
        return tuple(compiled)

    def _compile_effect(
        self,
        request: SubmitProposalRequest,
        effect: EffectProposal,
        runtime_refs: dict[tuple[str, str], str],
    ) -> ActionEffect:
        """把单个受控 Effect Proposal 转换为现有 Engine Effect。"""

        if isinstance(effect, RevealInformationEffectProposal):
            return RevealInformationEffect(
                information_id=self._resolve_ref(effect.information_ref, runtime_refs),
                scope="actor" if effect.scope == "self" else "party",
            )
        if isinstance(effect, HideInformationEffectProposal):
            return HideInformationEffect(
                information_id=self._resolve_ref(effect.information_ref, runtime_refs),
                scope="actor" if effect.scope == "self" else "party",
            )
        if isinstance(effect, SetVisibilityEffectProposal):
            target_id = self._resolve_ref(effect.target_ref, runtime_refs)
            target_kind = effect.target_ref.kind.removeprefix("runtime_")
            if target_kind not in {"information", "entity", "location"}:
                self._reject("TARGET_KIND_INVALID", "该对象不能设置可见性")
            return SetVisibilityEffect(
                target_kind=(
                    "information"
                    if target_kind == "information"
                    else "entity"
                    if target_kind == "entity"
                    else "location"
                ),
                target_id=target_id,
                visible=effect.visible,
                scope="actor" if effect.scope == "self" else "party",
            )
        if isinstance(effect, EnterLocationEffectProposal):
            return EnterLocationEffect(
                location_id=self._resolve_ref(effect.location_ref, runtime_refs)
            )
        if isinstance(effect, MoveEntityEffectProposal):
            entity_id = self._resolve_ref(effect.entity_ref, runtime_refs)
            if effect.destination.kind == "self_inventory":
                return MoveEntityEffect(
                    entity_id=entity_id,
                    holder_actor_id=request.actor_id,
                )
            return MoveEntityEffect(
                entity_id=entity_id,
                location_id=self._resolve_ref(
                    effect.destination.location_ref, runtime_refs
                ),
            )
        if isinstance(effect, ChangeEntityStateEffectProposal):
            return ChangeEntityStateEffect(
                entity_id=self._resolve_ref(effect.entity_ref, runtime_refs),
                key=effect.key,
                value=effect.value,
            )
        if isinstance(effect, ChangeItemConditionEffectProposal):
            return ChangeItemConditionEffect(
                entity_id=self._resolve_ref(effect.entity_ref, runtime_refs),
                condition=effect.condition,
            )
        if isinstance(effect, ConsumeEntityEffectProposal):
            return ConsumeEntityEffect(
                entity_id=self._resolve_ref(effect.entity_ref, runtime_refs)
            )
        if isinstance(effect, AdvanceWorldTimeEffectProposal):
            return AdvanceWorldTimeEffect(to_point_id=effect.to_point_id)
        if isinstance(effect, NarrativeOnlyEffectProposal):
            return NarrativeOnlyEffect()
        if isinstance(effect, MarkCoreResolvedEffectProposal):
            return MarkCoreResolvedEffect()
        if isinstance(effect, SetEndingAvailabilityEffectProposal):
            return SetEndingAvailabilityEffect(available=effect.available)
        if isinstance(effect, CommitTerminalEndingEffectProposal):
            return CommitTerminalEndingEffect(ending_id=effect.ending_id)
        raise AssertionError(f"未处理的 Effect Proposal: {effect.type}")

    @staticmethod
    def _declare_runtime_ref(
        request: SubmitProposalRequest,
        ref: ProposalRef,
        runtime_refs: dict[tuple[str, str], str],
    ) -> str:
        key = (ref.kind, ref.id)
        if key in runtime_refs:
            ProposalCompiler._reject("RUNTIME_REF_DUPLICATED", "动态对象被重复声明")
        resolved = derive_runtime_object_id(
            room_id=request.room_id,
            request_id=request.request_id,
            ref=ref,
        )
        runtime_refs[key] = resolved
        return resolved

    @staticmethod
    def _resolve_ref(
        ref: ProposalRef,
        runtime_refs: dict[tuple[str, str], str],
    ) -> str:
        if ref.kind.startswith("runtime_"):
            resolved = runtime_refs.get((ref.kind, ref.id))
            if resolved is None:
                ProposalCompiler._reject(
                    "RUNTIME_REF_UNDECLARED",
                    "动态对象必须先创建再使用",
                )
            return resolved
        return ref.id

    def _resolve_focus(
        self,
        runtime: EngineRuntimeSnapshot,
        proposal: SingleActionProposal,
        runtime_refs: dict[tuple[str, str], str],
    ) -> ActionTarget:
        """动态焦点尚不存在时使用现有锚点，避免把语义焦点误作授权主体。"""

        focus = proposal.semantic_focus
        if focus.kind.startswith("runtime_"):
            if proposal.anchor_ref is None:
                self._reject("ANCHOR_REQUIRED", "动态内容必须连接到当前可见对象")
            focus = proposal.anchor_ref
        target_id = self._resolve_ref(focus, runtime_refs)
        target_kind = focus.kind.removeprefix("runtime_")
        if target_kind == "world":
            return ActionTarget(kind="world", id=target_id)
        if target_kind == "actor":
            return ActionTarget(kind="actor", id=target_id)
        if target_kind == "information":
            return ActionTarget(kind="information", id=target_id)
        if target_kind == "entity":
            return ActionTarget(kind="entity", id=target_id)
        if target_kind == "location":
            return ActionTarget(kind="location", id=target_id)
        self._reject("TARGET_UNAVAILABLE", "当前语义焦点不可用于该动作")

    @staticmethod
    def _derive_persistence_intent(
        method_family: str,
        success: tuple[ActionEffect, ...],
        failure: tuple[ActionEffect, ...],
    ) -> PersistenceIntent:
        """只从实际 Effect 派生旧内核提示，开放方法文本不再承担授权职责。"""

        del method_family
        effects = (*success, *failure)
        if any(
            isinstance(effect, MoveEntityEffect) and effect.holder_actor_id is not None
            for effect in effects
        ):
            return "inventory"
        if any(isinstance(effect, EnterLocationEffect) for effect in effects):
            return "location"
        if any(isinstance(effect, ChangeItemConditionEffect) for effect in effects):
            return "object_state"
        for effect in effects:
            if isinstance(effect, ChangeEntityStateEffect):
                return (
                    "character_state"
                    if effect.key in {"consciousness", "posture", "restraint", "injury"}
                    else "object_state"
                )
        return "none"

    @staticmethod
    def _reject(
        code: str,
        player_safe_reason: str,
        *,
        repairability: Repairability = "auto_repairable",
        fault: ValidationFault = "agent",
    ) -> NoReturn:
        raise AdjudicationValidationError(
            ValidationResult(
                status="rejected",
                code=code,
                repairability=repairability,
                fault=fault,
                player_safe_reason=player_safe_reason,
            )
        )
