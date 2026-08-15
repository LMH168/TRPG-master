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
    CommitTerminalEndingEffect,
    ConsumeEntityEffect,
    EnsureRuntimeEntityEffect,
    EnsureRuntimeLocationEffect,
    EnterLocationEffect,
    HideInformationEffect,
    MarkCoreResolvedEffect,
    MoveEntityEffect,
    NarrativeOnlyEffect,
    PersistenceIntent,
    ProposalRef,
    RevealInformationEffect,
    SetEndingAvailabilityEffect,
    SetVisibilityEffect,
    SingleActionProposal,
    SubmitProposalRequest,
    ValidationResult,
)
from collaboration_framework.contracts.proposal import (
    AdvanceWorldTimeEffectProposal,
    ChangeEntityStateEffectProposal,
    CommitTerminalEndingEffectProposal,
    ConsumeEntityEffectProposal,
    EffectProposal,
    EnsureRuntimeEntityEffectProposal,
    EnsureRuntimeLocationEffectProposal,
    EnterLocationEffectProposal,
    HideInformationEffectProposal,
    MarkCoreResolvedEffectProposal,
    MoveEntityEffectProposal,
    NarrativeOnlyEffectProposal,
    RevealInformationEffectProposal,
    SetEndingAvailabilityEffectProposal,
    SetVisibilityEffectProposal,
)
from collaboration_framework.contracts.validation import Repairability, ValidationFault

from .models import EngineRuntimeSnapshot, ValidatedActionCommand

_HOST_FORBIDDEN_EFFECTS = (
    MarkCoreResolvedEffectProposal,
    SetEndingAvailabilityEffectProposal,
    CommitTerminalEndingEffectProposal,
)


def derive_runtime_object_id(*, room_id: str, request_id: str, ref: ProposalRef) -> str:
    """由可信回合身份和逻辑引用派生稳定 ID，供恢复与规范化代码复用。"""

    digest = hashlib.sha256(f"{room_id}\0{request_id}\0{ref.kind}\0{ref.id}".encode()).hexdigest()[
        :20
    ]
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
            field for field in fields if getattr(candidate, field) != getattr(legacy, field)
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
        if proposal.rule_ref is not None and (
            proposal.success_effect_proposals or proposal.failure_effect_proposals
        ):
            self._reject(
                "RULE_EFFECT_OVERRIDE",
                "规则已经拥有结果，不能同时提交 Host 结果",
            )
        self._reject_forbidden_host_effects(
            (*proposal.success_effect_proposals, *proposal.failure_effect_proposals)
        )

        runtime_refs: dict[tuple[str, str], str] = {}
        success = self._compile_effects(request, proposal.success_effect_proposals, runtime_refs)
        failure = self._compile_effects(request, proposal.failure_effect_proposals, runtime_refs)
        focus = self._resolve_focus(runtime, proposal, runtime_refs)
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
            check=proposal.check_proposal,
            rule_decision=proposal.rule_ref,
            success_effects=success,
            failure_effects=failure,
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                proposal.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return ValidatedActionCommand(
            request=request,
            proposal_fingerprint=fingerprint,
            adjudication=adjudication,
            validation=ValidationResult(
                status="accepted",
                authority_level=None,
                code="OK",
                player_safe_reason="动作提议已通过权威编译",
            ),
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
                location_id = self._declare_runtime_ref(request, effect.runtime_ref, runtime_refs)
                compiled.append(
                    EnsureRuntimeLocationEffect(
                        location_id=location_id,
                        name=effect.name,
                        parent_location_id=(
                            self._resolve_ref(effect.parent_ref, runtime_refs)
                            if effect.parent_ref is not None
                            else None
                        ),
                        connected_location_id=self._resolve_ref(effect.connected_ref, runtime_refs),
                    )
                )
            elif isinstance(effect, EnsureRuntimeEntityEffectProposal):
                entity_id = self._declare_runtime_ref(request, effect.runtime_ref, runtime_refs)
                compiled.append(
                    EnsureRuntimeEntityEffect(
                        entity_id=entity_id,
                        entity_kind=effect.entity_kind,
                        name=effect.name,
                        location_id=self._resolve_ref(effect.location_ref, runtime_refs),
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
                location_id=self._resolve_ref(effect.destination.location_ref, runtime_refs),
            )
        if isinstance(effect, ChangeEntityStateEffectProposal):
            return ChangeEntityStateEffect(
                entity_id=self._resolve_ref(effect.entity_ref, runtime_refs),
                key=effect.key,
                value=effect.value,
            )
        if isinstance(effect, ConsumeEntityEffectProposal):
            return ConsumeEntityEffect(entity_id=self._resolve_ref(effect.entity_ref, runtime_refs))
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
        """从开放方法族和已编译 Effect 派生旧内核兼容提示。"""

        method_intents: dict[str, PersistenceIntent] = {
            "kill": "character_state",
            "knock_out": "character_state",
            "knock_down": "character_state",
            "stand_up": "character_state",
            "restrain": "character_state",
            "release": "character_state",
            "injure_minor": "character_state",
            "injure_major": "character_state",
            "injure_critical": "character_state",
            "open": "object_state",
            "close": "object_state",
            "lock": "object_state",
            "unlock": "object_state",
            "break": "object_state",
            "repair": "object_state",
            "pick_up": "inventory",
            "drop": "inventory",
            "transfer": "inventory",
            "consume": "inventory",
            "travel": "location",
        }
        if method_family in method_intents:
            return method_intents[method_family]

        effects = (*success, *failure)
        if any(
            isinstance(effect, MoveEntityEffect) and effect.holder_actor_id is not None
            for effect in effects
        ):
            return "inventory"
        if any(isinstance(effect, EnterLocationEffect) for effect in effects):
            return "location"
        if any(isinstance(effect, ChangeEntityStateEffect) for effect in effects):
            return "object_state"
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
