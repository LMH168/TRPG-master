"""Production composition for issue #225 finite ActionPlan turns."""

from __future__ import annotations

import hashlib
import re
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import structlog
from collaboration_framework.contracts import (
    ActionAdjudication,
    ActionMethod,
    ActionPlan,
    ActionPlanPolicy,
    ActionPlanProposal,
    ActionPlanProposalStep,
    ActionPlanStep,
    ActionTarget,
    AdjudicationExecution,
    AdvanceWorldTimeEffect,
    CancelActionPlanRequest,
    CancelCheckChoice,
    ChangeEntityStateEffect,
    CheckDecisionRequest,
    ConsumeEntityEffect,
    ContractError,
    EnsureRuntimeEntityEffect,
    EnsureRuntimeLocationEffect,
    EnterLocationEffect,
    GetAdjudicationStatusRequest,
    HideInformationEffect,
    HostDecisionProposal,
    JsonObject,
    KeeperCapabilityView,
    MarkCoreResolvedEffect,
    MoveEntityEffect,
    NarrativeOnlyEffect,
    NoAdjudicationCheck,
    PlayerInput,
    PlayerView,
    PostRollDecisionRequest,
    RequiredAdjudicationCheck,
    RevealInformationEffect,
    RuleDecisionRef,
    SetEndingAvailabilityEffect,
    SetVisibilityEffect,
    SingleActionProposal,
    SkillCheckCandidate,
    WorldClockView,
)
from collaboration_framework.engine import AdjudicationEngineService, EngineStore, RuleEngineService
from collaboration_framework.host.adapters import InMemoryActionPlanRunStore
from collaboration_framework.host.application import (
    ActionPlanNarrationValidationError,
    ActionPlanNarrator,
    ActionPlanOrchestrator,
    HostTurnDecisionExecutor,
    PlayerViewProjector,
    TurnExecutionError,
)
from collaboration_framework.host.ports import (
    ActionPlanStepAdjudicator,
    ActionPlanStepFailure,
    RecentHistorySource,
)
from collaboration_framework.host.schemas import (
    ActionPlanAdvanceResult,
    ActionPlanNarrationContext,
    ActionPlanNarrationOutput,
    ActionPlanRun,
    ActionPlanStepContext,
    CompletedPlanStepSummary,
    HostAgentContext,
    RecentHistoryBudget,
    RecentTurnContext,
    SingleActionClarificationResult,
    SingleActionTurnResult,
)
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.core.turn_events import TurnPhase

logger = structlog.get_logger()

TurnPhaseObserver = Callable[[TurnPhase], Awaitable[None]]


async def _emit_phase(observer: TurnPhaseObserver | None, phase: TurnPhase) -> None:
    if observer is not None:
        await observer(phase)


async def _log_step_adjudication_failure(failure: ActionPlanStepFailure) -> None:
    """记录玩家不可见的步骤诊断，不接触 Prompt、模型正文或 GM-only 上下文。"""

    fields = {
        "action": failure.correlation_id,
        "stage": "步骤裁决",
        "plan": failure.plan_id,
        "step": failure.step_id,
        "step_index": failure.step_index,
        "attempt": failure.attempt,
        "duration_ms": failure.duration_ms,
        "code": failure.code,
        "error_type": type(failure.error).__name__,
        "completed_steps": failure.completed_steps,
        "authoritative_submitted": failure.authoritative_submitted,
    }
    if failure.code == "STEP_ADJUDICATOR_FAILED":
        # 未分类错误必须留下完整堆栈，定位号才能从回合日志追到真正失败点；堆栈只在
        # 服务端输出，ActionPlanRun 和 WebSocket 协议都不会持有这个字段。
        logger.error(
            "action_plan_step_adjudication_unclassified",
            **fields,
            stack="".join(traceback.format_exception(failure.error)),
        )
        return
    logger.warning("action_plan_step_adjudication_failed", **fields)


class HostTurnDecisionModel(Protocol):
    async def generate(self, context: HostAgentContext) -> HostDecisionProposal: ...


@dataclass(frozen=True)
class ActionPlanTurnResult:
    player_input: PlayerInput
    player_view: PlayerView
    status: str
    execution: AdjudicationExecution | None = None
    narration: ActionPlanNarrationOutput | None = None
    plan_id: str | None = None

    @property
    def waiting_for_player(self) -> bool:
        return self.status == "waiting_for_player"


@dataclass(frozen=True)
class _TravelTarget:
    id: str
    name: str


def _proposal_ref(kind: str, object_id: str, runtime_ids: set[str]) -> dict[str, str]:
    """把 legacy 逻辑对象转换为 Proposal 引用，不携带任何可信身份字段。"""

    if object_id in runtime_ids:
        return {
            "kind": "runtime_location" if kind == "location" else "runtime_entity",
            "id": object_id,
        }
    return {"kind": kind, "id": object_id}


def _proposal_from_adjudication(adjudication: ActionAdjudication) -> SingleActionProposal:
    """将确定性裁决降为 v2 无授权 Proposal，并显式声明目标完成条件。"""

    effects = (*adjudication.success_effects, *adjudication.failure_effects)
    runtime_locations = {
        effect.location_id for effect in effects if isinstance(effect, EnsureRuntimeLocationEffect)
    }
    runtime_entities = {
        effect.entity_id for effect in effects if isinstance(effect, EnsureRuntimeEntityEffect)
    }

    def convert(effect: object) -> dict[str, object]:
        if isinstance(effect, EnsureRuntimeLocationEffect):
            return {
                "type": "ensure_runtime_location",
                "runtime_ref": _proposal_ref("location", effect.location_id, runtime_locations),
                "name": effect.name,
                "parent_ref": (
                    _proposal_ref("location", effect.parent_location_id, runtime_locations)
                    if effect.parent_location_id is not None
                    else None
                ),
                "connected_ref": _proposal_ref(
                    "location", effect.connected_location_id, runtime_locations
                ),
            }
        if isinstance(effect, EnsureRuntimeEntityEffect):
            return {
                "type": "ensure_runtime_entity",
                "runtime_ref": _proposal_ref("entity", effect.entity_id, runtime_entities),
                "entity_kind": effect.entity_kind,
                "name": effect.name,
                "location_ref": _proposal_ref("location", effect.location_id, runtime_locations),
            }
        if isinstance(effect, EnterLocationEffect):
            return {
                "type": "enter_location",
                "location_ref": _proposal_ref("location", effect.location_id, runtime_locations),
            }
        if isinstance(effect, MoveEntityEffect):
            destination: dict[str, object] = (
                {"kind": "self_inventory"}
                if effect.holder_actor_id == adjudication.actor_id
                else {
                    "kind": "location",
                    "location_ref": _proposal_ref(
                        "location", effect.location_id or "", runtime_locations
                    ),
                }
            )
            return {
                "type": "move_entity",
                "entity_ref": _proposal_ref("entity", effect.entity_id, runtime_entities),
                "destination": destination,
            }
        if isinstance(effect, ChangeEntityStateEffect):
            return {
                "type": "change_entity_state",
                "entity_ref": _proposal_ref("entity", effect.entity_id, runtime_entities),
                "key": effect.key,
                "value": effect.value,
            }
        if isinstance(effect, ConsumeEntityEffect):
            return {
                "type": "consume_entity",
                "entity_ref": _proposal_ref("entity", effect.entity_id, runtime_entities),
            }
        if isinstance(effect, RevealInformationEffect):
            return {
                "type": "reveal_information",
                "information_ref": {"kind": "information", "id": effect.information_id},
                "scope": "self" if effect.scope == "actor" else "party",
            }
        if isinstance(effect, HideInformationEffect):
            return {
                "type": "hide_information",
                "information_ref": {"kind": "information", "id": effect.information_id},
                "scope": "self" if effect.scope == "actor" else "party",
            }
        if isinstance(effect, SetVisibilityEffect):
            return {
                "type": "set_visibility",
                "target_ref": {"kind": effect.target_kind, "id": effect.target_id},
                "visible": effect.visible,
                "scope": "self" if effect.scope == "actor" else "party",
            }
        if isinstance(effect, AdvanceWorldTimeEffect):
            return {"type": "advance_world_time", "to_point_id": effect.to_point_id}
        if isinstance(effect, MarkCoreResolvedEffect):
            return {"type": "mark_core_resolved"}
        if isinstance(effect, SetEndingAvailabilityEffect):
            return {"type": "set_ending_availability", "available": effect.available}
        if isinstance(effect, NarrativeOnlyEffect):
            return {"type": "narrative_only"}
        # 规则专属 L4/L5 Effect 不允许从 Host Proposal 兼容转换。
        raise ValueError(f"不支持转换为 Host Proposal 的效果: {type(effect).__name__}")

    target = adjudication.target
    runtime_ids = runtime_locations | runtime_entities
    semantic_focus = _proposal_ref(target.kind, target.id, runtime_ids)
    anchor_ref: dict[str, str] | None = None
    if target.id in runtime_entities:
        created = next(
            effect
            for effect in effects
            if isinstance(effect, EnsureRuntimeEntityEffect) and effect.entity_id == target.id
        )
        anchor_ref = _proposal_ref("location", created.location_id, runtime_locations)
    elif target.id in runtime_locations:
        created_location = next(
            effect
            for effect in effects
            if isinstance(effect, EnsureRuntimeLocationEffect) and effect.location_id == target.id
        )
        anchor_ref = _proposal_ref(
            "location", created_location.connected_location_id, runtime_locations
        )
    success_effect_proposals = [convert(item) for item in adjudication.success_effects]
    failure_effect_proposals = [convert(item) for item in adjudication.failure_effects]
    # 创建辅助 Effect 和 narrative_only 不是最终事实；其余成功 Effect 可以由
    # Engine 在提交后直接验证。没有持久后置条件时，目标只声明一次过程交互。
    completion_requirements = [
        item
        for item in success_effect_proposals
        if item["type"]
        not in {"ensure_runtime_location", "ensure_runtime_entity", "narrative_only"}
    ]
    family = adjudication.method.family.lower()
    if completion_requirements:
        completion: dict[str, object] = {
            "kind": "effects",
            "requirements": completion_requirements,
        }
    else:
        interaction = (
            "social"
            if family in {"dialogue", "social", "talk"}
            else (
                "observe"
                if family in {"observe", "search", "investigate", "spot_hidden"}
                else "physical"
                if family in {"physical", "pick_up", "drop", "use"}
                else "other"
            )
        )
        completion = {"kind": "process", "interaction": interaction}

    return SingleActionProposal.model_validate(
        {
            "schema_version": 2,
            "semantic_goal": adjudication.summary,
            "semantic_focus": semantic_focus,
            "anchor_ref": anchor_ref,
            "method_family": adjudication.method.family,
            "method_description": adjudication.method.description,
            # Legacy ActionAdjudication 没有结构化实施手段；确定性兼容路径只声明
            # intrinsic，涉及具体物品的生产 Proposal 必须由 Host 明确给出 item。
            "execution_means": {"kind": "intrinsic"},
            "check_proposal": adjudication.check.model_dump(mode="json"),
            "rule_ref": (
                adjudication.rule_decision.model_dump(mode="json")
                if adjudication.rule_decision is not None
                else None
            ),
            "success_effect_proposals": success_effect_proposals,
            "failure_effect_proposals": failure_effect_proposals,
            "completion": completion,
        }
    )


class DeterministicHostTurnDecisionModel:
    """Offline-safe model used only by fake/test composition."""

    @staticmethod
    def _as_proposal(
        decision: ActionPlan | ActionAdjudication,
    ) -> HostDecisionProposal:
        """离线 Fake 也只交付 Proposal，避免测试构造第二条生产写入口。"""

        if isinstance(decision, ActionPlan):
            return ActionPlanProposal(
                semantic_goal=decision.goal,
                steps=tuple(
                    ActionPlanProposalStep(
                        semantic_goal=step.semantic_goal,
                        public_progress_label=step.public_progress_label,
                    )
                    for step in decision.steps
                ),
            )
        return _proposal_from_adjudication(decision)

    async def generate(self, context: HostAgentContext) -> HostDecisionProposal:
        utterance = context.player_input.utterance
        separators = ("然后", "接着", "随后", "再去", "，再", ";", "；")
        pieces = [utterance]
        for separator in separators:
            if separator in utterance:
                pieces = [part.strip(" ，,。") for part in utterance.split(separator)]
                pieces = [part for part in pieces if part]
                break
        if len(pieces) >= 2:
            return self._as_proposal(
                ActionPlan(
                    goal=utterance,
                    steps=tuple(
                        ActionPlanStep(
                            kind=(
                                "travel"
                                if any(word in part for word in ("去", "前往", "进入"))
                                else "action"
                            ),
                            semantic_goal=part,
                        )
                        for part in pieces
                    ),
                )
            )

        compact = _compact_travel_plan(context.player_view, utterance)
        if compact is not None:
            return self._as_proposal(compact)

        destination = _match_travel_target(context.player_view, utterance)
        if destination is not None:
            return self._as_proposal(
                ActionAdjudication(
                    request_id="application-owned",
                    source_revision=context.player_view.revision,
                    actor_id=context.player_input.actor_id,
                    summary=utterance,
                    target=ActionTarget(
                        kind="location",
                        id=destination.id,
                    ),
                    method=ActionMethod(family="travel", description=utterance),
                    check=NoAdjudicationCheck(),
                    success_effects=(EnterLocationEffect(location_id=destination.id),),
                )
            )

        # A single action uses the same player-safe Rule Match View as a plan
        # step.  Without this bridge, the Fake planner returned narrative_only
        # for every non-travel utterance, so CI could exercise v3 rules only by
        # artificially wrapping one action in a multi-step plan.
        deterministic = _deterministic_step_adjudication(
            ActionPlanStepContext(
                player_input=context.player_input,
                plan_id="single-action",
                plan_goal=utterance,
                step_index=0,
                step_request_id="application-owned",
                step=ActionPlanStep(
                    kind=(
                        "dialogue"
                        if any(word in utterance for word in ("问", "交谈", "聊天"))
                        else "action"
                    ),
                    semantic_goal=utterance,
                ),
                player_view=context.player_view,
                keeper_capabilities=context.keeper_capabilities,
            )
        )
        if deterministic is not None:
            return self._as_proposal(deterministic)

        return self._as_proposal(
            ActionAdjudication(
                request_id="application-owned",
                source_revision=context.player_view.revision,
                actor_id=context.player_input.actor_id,
                summary=utterance,
                target=ActionTarget(kind="location", id=context.player_view.scene.id),
                method=ActionMethod(family="action", description=utterance),
                check=NoAdjudicationCheck(),
                success_effects=(NarrativeOnlyEffect(),),
            )
        )


def _compact_travel_plan(view: PlayerView, utterance: str) -> ActionPlan | None:
    """Split compact fake-provider phrases without consulting hidden ModuleContent."""

    destination = _match_travel_target(view, utterance)
    if destination is None:
        return None
    anchor = _best_label_overlap(
        utterance,
        (
            destination.name,
            destination.id,
        ),
    )
    if anchor is None:
        return None
    anchor_end = utterance.find(anchor) + len(anchor)
    remainder = utterance[anchor_end:].strip(" ，,。")
    action_markers = (
        "搜索",
        "调查",
        "查阅",
        "查找",
        "研究",
        "询问",
        "交谈",
        "找",
        "查",
        "问",
    )
    marker = next((item for item in action_markers if item in remainder), None)
    if marker is None:
        return None
    # Keep method qualifiers that precede the verb (for example “用侦查搜索”
    # or “用信用评级询问”).  Rule options are selected from those player-safe
    # words; slicing from the verb silently discarded the only discriminating
    # evidence and made the step fall back to narrative_only.
    follow_up = remainder.strip(" ，,。")
    if not follow_up:
        return None
    destination_name = destination.name
    return ActionPlan(
        goal=utterance,
        steps=(
            ActionPlanStep(kind="travel", semantic_goal=f"前往{destination_name}"),
            ActionPlanStep(
                kind=(
                    "dialogue" if any(word in follow_up for word in ("问", "交谈")) else "action"
                ),
                semantic_goal=f"在{destination_name}{follow_up}",
            ),
        ),
    )


def _match_visible_exit(view: PlayerView, text: str):
    if not any(word in text for word in ("去", "前往", "进入", "到", "抵达")):
        return None
    matches = []
    for exit_view in view.scene.available_exits:
        destination_labels = (
            (exit_view.destination.name, exit_view.destination.scene_id)
            if exit_view.destination
            else ()
        )
        labels = (
            exit_view.name,
            exit_view.id,
            *exit_view.aliases,
            *destination_labels,
        )
        overlap = _best_label_overlap(text, labels)
        if overlap is not None:
            matches.append((len(overlap), exit_view.id, exit_view))
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], item[1]))
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        return None
    return matches[0][2]


"""部分常见环境地点的确定性快捷路径。

这张表只用于减少明确简单请求的模型调用，不是 Runtime 地点的类别白名单。
未出现在表里的地点必须交由 Agent 按 WorldProfile / background 和 Canon 冲突
门禁判断；不得仅因类别未收录就拒绝创建，也不得映射成其他已知地点。
"""
_AMBIENT_VENUE_LABELS: tuple[tuple[str, str, str], ...] = (
    ("旅店", "ambient_inn", "镇上的旅店"),
    ("旅馆", "ambient_inn", "镇上的旅店"),
    ("客栈", "ambient_inn", "镇上的旅店"),
    ("寄宿屋", "ambient_boarding_house", "出租床位的寄宿屋"),
    ("住处", "ambient_boarding_house", "出租床位的寄宿屋"),
    ("餐馆", "ambient_diner", "街边的餐馆"),
    ("饭馆", "ambient_diner", "街边的餐馆"),
    ("餐厅", "ambient_diner", "街边的餐馆"),
    ("咖啡馆", "ambient_cafe", "街角的咖啡馆"),
    ("杂货店", "ambient_general_store", "街边的杂货店"),
    ("商店", "ambient_general_store", "街边的杂货店"),
)

_AMBIENT_VENUE_INTENT_WORDS = ("找", "去", "前往", "进入", "到", "住", "休息", "睡")


def _ambient_venue_aliases(location_id: str) -> tuple[str, ...]:
    """返回同一类普通地点的自然语言名称。

    Runtime 只保存一个稳定 id 和展示名，但玩家后续回到该地点时
    可能使用同义词。同义词来自通用场所表，不从单个测试语句猜测。
    """

    return tuple(
        label for label, id_stem, _display_name in _AMBIENT_VENUE_LABELS if id_stem == location_id
    )


def _ambient_venue_adjudication(
    context: ActionPlanStepContext,
) -> ActionAdjudication | None:
    """把「找一家旅店」这类泛指去处，确定性地落成一个运行时地点。

    只在三件事同时成立时才动手：说的是一个普通场所、玩家确实想去、而且它
    和模组写过的任何地点都不重名。第三条是关键——重名意味着这可能是一个
    隐藏的 Canon 地点（比如地下酒吧），那就必须留给模组自己揭示，绝不能由
    这里凭空造一个同名替身出来。
    """

    goal = context.step.semantic_goal
    if not any(word in goal for word in _AMBIENT_VENUE_INTENT_WORDS):
        return None
    # semantic_goal 是模型对原话的改写，不能把模型自行补出的“住处”等地点
    # 当成玩家授权创建新地点；地点类别必须在玩家原话中也明确出现。
    utterance = context.player_input.utterance
    matched = next(
        (
            (label, id_stem, name)
            for label, id_stem, name in _AMBIENT_VENUE_LABELS
            if label in goal and label in utterance
        ),
        None,
    )
    if matched is None:
        return None
    label, id_stem, display_name = matched

    capabilities = context.keeper_capabilities
    if capabilities is None:
        return None
    # 和任何已写地点（含隐藏地点）重名就退出，交给模型在完整上下文里判断。
    for location in capabilities.locations:
        if label in location.name or location.name in goal:
            return None
    if any(location.id == id_stem for location in capabilities.locations):
        return None

    anchor_id = _ambient_venue_anchor(context.player_view)
    if anchor_id is None:
        return None

    return ActionAdjudication(
        request_id=context.step_request_id,
        source_revision=context.player_view.revision,
        actor_id=context.player_input.actor_id,
        summary=goal,
        # 目标仍是作为连接锚点的既有地点：新地点这一刻还不存在。
        target=ActionTarget(kind="location", id=anchor_id),
        method=ActionMethod(family="travel", description=goal),
        check=NoAdjudicationCheck(),
        success_effects=(
            EnsureRuntimeLocationEffect(
                location_id=id_stem,
                name=display_name,
                connected_location_id=anchor_id,
            ),
            EnterLocationEffect(location_id=id_stem),
        ),
    )


def _ambient_venue_anchor(view: PlayerView) -> str | None:
    """普通去处应当挂在公共路网上，而不是你此刻站着的那间私人书房。"""

    for location in view.known_locations:
        if (
            location.kind == "connector"
            and location.existence == "known"
            and location.localization == "located"
            and location.access != "blocked"
        ):
            return location.id
    return view.scene.id or None


def _match_travel_target(view: PlayerView, text: str) -> _TravelTarget | None:
    if not any(word in text for word in ("去", "前往", "进入", "到", "抵达")):
        return None
    # 只在旅行动词之后识别目的地，避免“带托马斯去墓地”中的“托马斯”
    # 模糊命中“托马斯的会客室”，从而把旅行方向完全反转。
    explicit_markers = tuple(re.finditer(r"前往|进入|抵达|去", text))
    if explicit_markers:
        match_text = text[explicit_markers[-1].end() :]
    else:
        arrival_marker = text.rfind("到")
        match_text = text[arrival_marker + 1 :]
    matches: list[tuple[tuple[int, int], str, _TravelTarget]] = []
    for location in view.known_locations:
        if location.existence != "known" or location.localization != "located":
            continue
        score = _best_travel_label_score(
            match_text,
            (location.name, location.id, *_ambient_venue_aliases(location.id)),
        )
        if score is not None:
            matches.append((score, location.id, _TravelTarget(location.id, location.name)))
    for exit_view in view.scene.available_exits:
        if exit_view.destination is None:
            continue
        labels = (
            exit_view.name,
            exit_view.id,
            *exit_view.aliases,
            exit_view.destination.name,
            exit_view.destination.scene_id,
        )
        score = _best_travel_label_score(match_text, labels)
        if score is not None:
            target = _TravelTarget(
                exit_view.destination.scene_id,
                exit_view.destination.name,
            )
            matches.append((score, target.id, target))
    if not matches:
        return None
    # The same location can be present in both known_locations and immediate exits.
    deduplicated = {
        target.id: (score, target_id, target)
        for score, target_id, target in matches
        if score == max(item[0] for item in matches if item[2].id == target.id)
    }
    ranked = sorted(
        deduplicated.values(),
        key=lambda item: (-item[0][0], -item[0][1], item[1]),
    )
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][2]


def _best_travel_label_score(
    text: str,
    labels: tuple[str, ...],
) -> tuple[int, int] | None:
    """Score only destination-bearing location-name matches.

    A full label or stable alias is always meaningful.  For longer display
    names, a suffix can also be the identifying place name.  A shared prefix is
    deliberately excluded: locality and directional wording commonly lives
    there and cannot distinguish two venues in the same area.
    """

    scores: list[tuple[int, int]] = []
    for label in labels:
        normalized = label.strip()
        if not normalized:
            continue
        if normalized in text:
            scores.append((len(normalized), 2))
            continue
        overlap = _best_label_overlap(text, (normalized,))
        if overlap is not None and normalized.endswith(overlap):
            scores.append((len(overlap), 1))
    return max(scores) if scores else None


def _explicit_travel_phrase(text: str) -> str | None:
    """Return a directly named destination phrase, not a goal that implies one.

    ``去教堂看看`` names a destination and therefore must never be repaired to
    another known location. ``去找守墓人`` only names the person being sought, so the
    Agent may still infer a destination from player-safe capabilities.
    """

    markers = tuple(re.finditer(r"前往|进入|抵达|去", text))
    if not markers:
        return None
    remainder = text[markers[-1].end() :].strip(" \t，,。；;！!？?")
    if not remainder:
        return None
    phrase = re.split(r"，|,|。|；|;|！|!|？|\?|\b然后\b|接着|随后|再去", remainder, maxsplit=1)[
        0
    ].strip()
    if not phrase or phrase.startswith(("找", "寻找", "寻访", "拜访", "询问", "问", "会合")):
        return None
    return phrase


def _has_unmatched_explicit_travel_destination(view: PlayerView, text: str) -> bool:
    return _explicit_travel_phrase(text) is not None and _match_travel_target(view, text) is None


def _deterministic_clarification_text(context: ActionPlanNarrationContext) -> str:
    """根据已提交步骤生成不会推翻权威状态的澄清文案。"""

    successful_steps = tuple(
        step for step in context.completed_steps if getattr(step, "outcome", None) == "success"
    )
    completed_travel = any(
        _explicit_travel_phrase(getattr(step, "semantic_goal", "")) is not None
        for step in successful_steps
    )
    blocked_step_goal = getattr(context, "blocked_step_goal", None)
    if blocked_step_goal is not None:
        remaining_step_goals = getattr(context, "remaining_step_goals", ())
        completed = "此前已经完成的步骤仍然有效；" if successful_steps else ""
        remaining = (
            "；后续步骤尚未执行：" + "、".join(remaining_step_goals) if remaining_step_goals else ""
        )
        reason = (
            getattr(context, "player_safe_failure_reason", None) or "当前步骤无法形成可确认结果"
        )
        return (
            f"{completed}{reason}：「{blocked_step_goal}」{remaining}。"
            "如需继续，请重新提交新的行动。"
        )
    if completed_travel:
        scene_name = getattr(context.player_view.scene, "name", "") or "当前地点"
        return f"你已经抵达{scene_name}，但后续行动尚未形成可确认的结果。"
    if successful_steps:
        return "此前已经完成的行动仍然有效，但后续行动尚未形成可确认的结果。"
    if _explicit_travel_phrase(context.player_input.utterance) is not None:
        return "你没有在当前能够确认的道路和周边找到与描述相符的地点，因此仍停留在原处。"
    return "你暂时无法确认这次行动的具体对象或结果。"


def _best_label_overlap(text: str, labels: tuple[str, ...]) -> str | None:
    candidates: set[str] = set()
    for label in labels:
        normalized = label.strip()
        if not normalized:
            continue
        if normalized in text:
            candidates.add(normalized)
        if any("一" <= character <= "鿿" for character in normalized):
            for width in range(len(normalized), 1, -1):
                for start in range(len(normalized) - width + 1):
                    candidate = normalized[start : start + width]
                    if candidate in text:
                        candidates.add(candidate)
                if candidates:
                    break
    return max(candidates, key=lambda item: (len(item), item)) if candidates else None


class DeterministicActionPlanNarrationModel:
    async def generate(self, context: ActionPlanNarrationContext) -> JsonObject:
        completed = "；".join(
            _quote_action_summary(step.semantic_goal) for step in context.completed_steps
        )
        if context.termination_status == "needs_clarification":
            text = _deterministic_clarification_text(context)
            kind = "clarification"
        elif context.termination_status in {"cancelled", "stopped"}:
            text = f"已经发生的行动是：{completed or '当前没有已完成步骤'}。后续行动已停止。"
            kind = "narration"
        else:
            goal = completed or _quote_action_summary(context.plan_goal)
            text = f"你依次完成了：{goal}。"
            kind = "narration"
        required_refs = tuple(
            item.ref for item in context.narration_evidence if item.required_in_narration
        )
        required_text = "；".join(
            f"你发现了{item.subject_name}" + (f"：{item.description}" if item.description else "")
            for item in context.narration_evidence
            if item.required_in_narration
        )
        if required_text:
            text = f"{text}{required_text}。"
        return {
            "kind": kind,
            "text": text,
            "claimed_evidence_refs": required_refs,
            "suggested_actions": [],
        }


def _quote_action_summary(summary: str) -> str:
    """Keep player-authored first person inside an explicit quotation."""

    return f"「{summary.replace('「', '“').replace('」', '”')}」"


class ActionPlanTurnApplication:
    def __init__(
        self,
        *,
        store: EngineStore,
        engine: RuleEngineService,
        adjudication_engine: AdjudicationEngineService,
        planner: HostTurnDecisionModel,
        orchestrator: ActionPlanOrchestrator,
        narrator: ActionPlanNarrator,
        recent_history_source: RecentHistorySource,
        recent_history_budget: RecentHistoryBudget,
        recent_history_enabled: bool,
    ) -> None:
        self._store = store
        self._engine = engine
        self._adjudication_engine = adjudication_engine
        self._planner = planner
        self._orchestrator = orchestrator
        self._recent_history_source = recent_history_source
        self._recent_history_budget = recent_history_budget
        self._recent_history_enabled = recent_history_enabled
        self._narrator = narrator
        self._projector = PlayerViewProjector(engine)
        self._dispatcher = HostTurnDecisionExecutor(
            plan_orchestrator=orchestrator,
            executor=adjudication_engine,
            player_view_projector=self._projector,
            repair_adjudicator=orchestrator.adjudicator,
            policy=orchestrator.policy,
        )

    async def start(
        self,
        *,
        room_id: str,
        player_id: str,
        client_action_id: str,
        utterance: str,
        on_progress: Callable[[object], Awaitable[None]] | None = None,
        on_phase: TurnPhaseObserver | None = None,
        on_input_accepted: (Callable[[PlayerInput, PlayerView], Awaitable[None]] | None) = None,
    ) -> ActionPlanTurnResult:
        await _emit_phase(on_phase, "reading_player_view")
        actor_id = await self._resolve_actor_id(room_id, player_id)
        player_input = PlayerInput(
            room_id=room_id,
            player_id=player_id,
            actor_id=actor_id,
            client_action_id=client_action_id,
            utterance=utterance,
        )
        existing = await self._orchestrator.get_run(room_id, client_action_id)
        if existing is not None:
            await _emit_phase(on_phase, "executing_action")
            advanced = await self._orchestrator.start_or_resume(
                player_input,
                plan=None,
                on_progress=on_progress,
            )
            return await self._finish_plan_with_phases(
                player_input,
                advanced,
                on_phase=on_phase,
            )

        # A plan stuck in needs_clarification never produced any committed step
        # effect (see ActionPlanOrchestrator.cancel_remaining's boundary check),
        # so it is always safe to fold into the next turn. The player's new
        # utterance is handed to the planner together with recent history
        # (including the clarifying question itself); the model decides whether
        # this is an answer to that question (multi-step) or unrelated fresh
        # input, instead of the transport layer blocking on a stale plan_id.
        #
        # retryable_failure 同样要让位，但理由略有不同：它的**当前**步一定停在
        # `pending`（三处 _mark_step_failure 对可重试失败都写 step_status="pending"），
        # 所以照样落在可取消边界上；只是它更早的步骤可能已经提交过效果。那正是
        # cancel_remaining 的语义——保留已提交的，放弃剩下的。玩家换了一句话说，
        # 本来就是在放弃旧计划的余下部分。不让位的话，一次瞬态失败会把这名玩家
        # 锁在「只能原样重发同一句」上，直到占用过期。
        stale_plan = await self._orchestrator.active_for_room(room_id)
        if (
            stale_plan is not None
            and stale_plan.parent_action_id != client_action_id
            and stale_plan.status in ("needs_clarification", "retryable_failure")
            and stale_plan.player_id == player_id
        ):
            await self._orchestrator.cancel_remaining(
                CancelActionPlanRequest(
                    request_id=f"auto-supersede-{client_action_id}",
                    room_id=room_id,
                    player_id=player_id,
                    actor_id=actor_id,
                    parent_action_id=stale_plan.parent_action_id,
                )
            )

        view = await self._projector.project(player_input)
        if on_input_accepted is not None:
            await on_input_accepted(player_input, view)
        await _emit_phase(on_phase, "understanding_action")
        keeper_capabilities = await self._keeper_capabilities(player_input, view)
        recent_history = await self._read_recent_history(
            player_input=player_input,
            player_view=view,
        )
        try:
            decision = await self._planner.generate(
                HostAgentContext(
                    player_input=player_input,
                    player_view=view,
                    recent_history=recent_history,
                    # A single action is adjudicated right here in the planner call,
                    # so it needs the same Keeper vocabulary a plan step gets.
                    keeper_capabilities=keeper_capabilities,
                )
            )
        except TurnExecutionError as exc:
            if exc.code != "MODEL_OUTPUT_UNREADABLE":
                raise
            # 两次结构输出都失败时，动作尚未进入规则引擎，也没有任何权威写入。
            # 用确定性主持人澄清结束回合，不能把内部契约错误直接抛给玩家。
            logger.warning(
                "host_turn_planning_fallback",
                room=room_id.split("-", 1)[0][:8],
                action=client_action_id,
                code=exc.code,
            )
            await _emit_phase(on_phase, "generating_narration")
            return self._planning_failure_clarification(
                player_input=player_input,
                player_view=view,
            )
        await _emit_phase(on_phase, "executing_action")
        result = await self._dispatcher.execute(
            player_input,
            decision,
            on_progress=on_progress,
            recent_history=recent_history,
        )
        if isinstance(result, ActionPlanAdvanceResult):
            return await self._finish_plan_with_phases(
                player_input,
                result,
                on_phase=on_phase,
            )
        if isinstance(decision, ActionPlanProposal):
            raise TypeError("single result 不得对应 ActionPlan")
        if isinstance(result, SingleActionClarificationResult):
            await _emit_phase(on_phase, "refreshing_player_view")
            await _emit_phase(on_phase, "generating_narration")
            return await self._from_single_clarification(
                player_input,
                (
                    decision.semantic_goal
                    if isinstance(decision, SingleActionProposal)
                    else result.player_safe_reason
                ),
                result,
                recent_history=recent_history,
            )
        if result.execution.status in {
            "awaiting_skill_choice",
            "awaiting_post_roll_decision",
        }:
            await _emit_phase(on_phase, "waiting_for_check")
        else:
            await _emit_phase(on_phase, "refreshing_player_view")
            await _emit_phase(on_phase, "generating_narration")
        return await self._from_single(
            player_input,
            self._decision_summary(decision, result),
            result,
            recent_history=recent_history,
            focus_entity_ids=(
                (decision.semantic_focus.id,)
                if isinstance(decision, SingleActionProposal)
                and decision.semantic_focus.kind == "entity"
                and any(
                    entity.id == decision.semantic_focus.id
                    for entity in result.player_view.scene.visible_entities
                )
                else ()
            ),
        )

    @staticmethod
    def _decision_summary(
        decision: HostDecisionProposal,
        result: SingleActionTurnResult | SingleActionClarificationResult,
    ) -> str:
        """统一提取玩家可见摘要，澄清 Proposal 使用安全问题作为回退。"""

        if isinstance(decision, SingleActionProposal):
            return decision.semantic_goal
        if isinstance(result, SingleActionClarificationResult):
            return result.player_safe_reason
        if isinstance(decision, ActionPlanProposal):
            return decision.semantic_goal
        return ""

    @staticmethod
    def _planning_failure_clarification(
        *,
        player_input: PlayerInput,
        player_view: PlayerView,
    ) -> ActionPlanTurnResult:
        """规划模型连续返回坏结构时，生成零提交的玩家可见主持人回复。"""

        return ActionPlanTurnResult(
            player_input=player_input,
            player_view=player_view,
            status="needs_clarification",
            narration=ActionPlanNarrationOutput(
                kind="clarification",
                text="我暂时没能准确理解这次行动。请再明确一下你想做什么，以及行动的对象或地点。",
            ),
        )

    async def _finish_plan_with_phases(
        self,
        player_input: PlayerInput,
        result: ActionPlanAdvanceResult,
        *,
        on_phase: TurnPhaseObserver | None,
        verify_fingerprint: bool = True,
    ) -> ActionPlanTurnResult:
        if result.run.status == "waiting_for_player":
            await _emit_phase(on_phase, "waiting_for_check")
        elif result.run.status in {
            "awaiting_narration",
            "completed",
            "needs_clarification",
            "cancelled",
            "stopped",
        }:
            await _emit_phase(on_phase, "refreshing_player_view")
            await _emit_phase(on_phase, "generating_narration")
        return await self._from_plan(
            player_input,
            result,
            verify_fingerprint=verify_fingerprint,
        )

    async def resume_plan(
        self,
        player_input: PlayerInput,
        *,
        on_progress: Callable[[object], Awaitable[None]] | None = None,
        on_phase: TurnPhaseObserver | None = None,
    ) -> ActionPlanTurnResult:
        advanced = await self._orchestrator.start_or_resume(
            player_input,
            plan=None,
            on_progress=on_progress,
        )
        return await self._finish_plan_with_phases(
            player_input,
            advanced,
            on_phase=on_phase,
        )

    async def resume_owned(
        self,
        *,
        room_id: str,
        player_id: str,
        parent_action_id: str,
        on_progress: Callable[[object], Awaitable[None]] | None = None,
        on_phase: TurnPhaseObserver | None = None,
    ) -> ActionPlanTurnResult:
        actor_id = await self._resolve_actor_id(room_id, player_id)
        run = await self._orchestrator.get_run(room_id, parent_action_id)
        if (
            run is not None
            and run.player_id == player_id
            and run.actor_id == actor_id
            and run.parent_action_id == parent_action_id
            and run.pending_cancel_request_id is not None
        ):
            await self._recover_pending_post_roll_cancel(run)
        advanced = await self._orchestrator.resume_owned(
            room_id=room_id,
            player_id=player_id,
            actor_id=actor_id,
            parent_action_id=parent_action_id,
            on_progress=on_progress,
        )
        run = advanced.run
        player_input = PlayerInput(
            room_id=room_id,
            player_id=player_id,
            actor_id=actor_id,
            client_action_id=parent_action_id,
            utterance=run.parent_utterance or run.plan.goal,
        )
        return await self._finish_plan_with_phases(
            player_input,
            advanced,
            on_phase=on_phase,
            verify_fingerprint=False,
        )

    async def resume_pending(
        self,
        *,
        room_id: str,
        player_id: str,
        parent_action_id: str,
        on_progress: Callable[[object], Awaitable[None]] | None = None,
        on_phase: TurnPhaseObserver | None = None,
    ) -> ActionPlanTurnResult:
        """Resume either a durable ActionPlan or a persisted single action."""

        if await self._orchestrator.get_run(room_id, parent_action_id) is not None:
            return await self.resume_owned(
                room_id=room_id,
                player_id=player_id,
                parent_action_id=parent_action_id,
                on_progress=on_progress,
                on_phase=on_phase,
            )
        return await self.resume_single(
            room_id=room_id,
            player_id=player_id,
            parent_action_id=parent_action_id,
            on_phase=on_phase,
        )

    async def resume_single(
        self,
        *,
        room_id: str,
        player_id: str,
        parent_action_id: str,
        on_phase: TurnPhaseObserver | None = None,
    ) -> ActionPlanTurnResult:
        """Finish a single ActionAdjudication without creating a PlanRun."""

        recovery = await self._adjudication_engine.recover_action(
            GetAdjudicationStatusRequest(
                room_id=room_id,
                player_id=player_id,
                action_request_id=parent_action_id,
            )
        )
        if recovery is None:
            raise TurnExecutionError(
                "ACTION_NOT_FOUND",
                "没有找到可恢复的单动作裁决",
                retryable=True,
            )
        player_input = PlayerInput(
            room_id=room_id,
            player_id=player_id,
            actor_id=recovery.actor_id,
            client_action_id=parent_action_id,
            # The original player utterance is not part of the Engine contract;
            # the frozen adjudication summary is the safe recovery label.
            utterance=recovery.summary,
        )
        result = SingleActionTurnResult(
            execution=recovery.execution,
            player_view=await self._projector.refresh_adjudication(
                player_input,
                recovery.execution,
            ),
        )
        if recovery.execution.status in {
            "awaiting_skill_choice",
            "awaiting_post_roll_decision",
        }:
            await _emit_phase(on_phase, "waiting_for_check")
        else:
            await _emit_phase(on_phase, "refreshing_player_view")
            await _emit_phase(on_phase, "generating_narration")
        return await self._from_single(player_input, recovery.summary, result)

    async def active_for_room(self, room_id: str):
        return await self._orchestrator.active_for_room(room_id)

    async def get_plan(self, room_id: str, parent_action_id: str):
        return await self._orchestrator.get_run(room_id, parent_action_id)

    async def abandon_uncommitted_plan(
        self,
        *,
        room_id: str,
        parent_action_id: str,
        code: str,
    ) -> ActionPlanRun | None:
        """可靠回合确认未提交终态失败后，收束对应的步骤级计划。"""

        return await self._orchestrator.abandon_uncommitted(
            room_id=room_id,
            parent_action_id=parent_action_id,
            code=code,
        )

    async def release_uncommitted_plan_step(
        self,
        *,
        room_id: str,
        parent_action_id: str,
        code: str,
    ) -> ActionPlanRun | None:
        """部分提交 Turn 失败时释放当前未提交步骤，保留前序权威结果。"""

        return await self._orchestrator.release_uncommitted_step(
            room_id=room_id,
            parent_action_id=parent_action_id,
            code=code,
        )

    async def settle_failed_turn_plan(
        self,
        *,
        room_id: str,
        parent_action_id: str,
        code: str,
    ) -> ActionPlanRun | None:
        """收束终态失败 Turn 的计划占用，同时保留已经提交的步骤结果。"""

        return await self._orchestrator.settle_failed_turn(
            room_id=room_id,
            parent_action_id=parent_action_id,
            code=code,
        )

    async def cancel_remaining(
        self,
        *,
        room_id: str,
        player_id: str,
        parent_action_id: str,
        request_id: str,
    ) -> ActionPlanTurnResult:
        actor_id = await self._resolve_actor_id(room_id, player_id)
        existing = await self._orchestrator.get_run(room_id, parent_action_id)
        if (
            existing is not None
            and existing.player_id == player_id
            and existing.actor_id == actor_id
            and existing.parent_action_id == parent_action_id
            and existing.pending_cancel_request_id is not None
        ):
            # The durable intent, rather than the current client request ID,
            # owns recovery. This also handles a retry with a fresh request ID
            # after either authoritative write has already committed.
            await self._recover_pending_post_roll_cancel(existing)
            return await self.resume_owned(
                room_id=room_id,
                player_id=player_id,
                parent_action_id=parent_action_id,
            )
        execution: AdjudicationExecution | None = None
        if (
            existing is not None
            and existing.player_id == player_id
            and existing.actor_id == actor_id
            and existing.status == "waiting_for_player"
            and existing.current_step_index < len(existing.steps)
        ):
            execution = existing.steps[existing.current_step_index].adjudication_execution
            status = await self._adjudication_engine.get_status(
                GetAdjudicationStatusRequest(
                    room_id=room_id,
                    player_id=player_id,
                    action_request_id=existing.steps[existing.current_step_index].step_request_id,
                )
            )
            if status.execution is not None:
                execution = status.execution
            pending = execution.pending_decision if execution is not None else None
            if (
                execution is not None
                and execution.status == "awaiting_skill_choice"
                and pending is not None
            ):
                await self._adjudication_engine.decide(
                    CheckDecisionRequest(
                        request_id=request_id,
                        room_id=room_id,
                        player_id=player_id,
                        source_revision=execution.view_revision,
                        decision_id=pending.decision_id,
                        decision_version=pending.decision_version,
                        choice=CancelCheckChoice(),
                    )
                )
        cancel_request = CancelActionPlanRequest(
            request_id=request_id,
            room_id=room_id,
            player_id=player_id,
            actor_id=actor_id,
            parent_action_id=parent_action_id,
        )
        if (
            execution is not None
            and execution.status == "awaiting_post_roll_decision"
            and execution.check_run is not None
        ):
            # A post-roll cancel accepts the already-authoritative roll.  The
            # intent is durable before the Engine command so recovery can
            # finish the same check and stop later steps after a crash.
            intent = await self._orchestrator.request_cancel_after_current(cancel_request)
            await self._recover_pending_post_roll_cancel(intent)
            result = await self.resume_owned(
                room_id=room_id,
                player_id=player_id,
                parent_action_id=parent_action_id,
            )
            return result

        run = await self._orchestrator.cancel_remaining(cancel_request)
        player_input = PlayerInput(
            room_id=room_id,
            player_id=player_id,
            actor_id=actor_id,
            client_action_id=parent_action_id,
            utterance=run.parent_utterance or run.plan.goal,
        )
        result = ActionPlanAdvanceResult(
            run=run,
            player_view=await self._projector.project(player_input),
        )
        return await self._from_plan(
            player_input,
            result,
            verify_fingerprint=False,
        )

    async def _recover_pending_post_roll_cancel(
        self,
        run: ActionPlanRun,
    ) -> None:
        """Finish a durable post-roll cancel intent after any process restart.

        The persisted cancel ID is the idempotency key. A resolved Engine
        execution needs no second write; an awaiting execution receives the
        same derived accept-current command regardless of which client request
        triggered recovery.
        """

        cancel_id = run.pending_cancel_request_id
        if cancel_id is None:
            return
        if run.current_step_index >= len(run.steps):
            raise TurnExecutionError(
                "PLAN_CANCEL_RECOVERY_UNAVAILABLE",
                "取消请求无法从当前行动计划状态恢复，请刷新后重试",
                retryable=True,
            )
        current = run.steps[run.current_step_index]
        status = await self._adjudication_engine.get_status(
            GetAdjudicationStatusRequest(
                room_id=run.room_id,
                player_id=run.player_id,
                action_request_id=current.step_request_id,
            )
        )
        if status.status in {"resolved", "cancelled"}:
            return
        if status.status != "awaiting_post_roll_decision" or status.execution is None:
            raise TurnExecutionError(
                "PLAN_CANCEL_RECOVERY_UNAVAILABLE",
                "取消请求无法从当前检定状态恢复，请刷新后重试",
                retryable=True,
            )
        execution = status.execution
        check_run = execution.check_run
        if check_run is None:
            raise TurnExecutionError(
                "PLAN_CANCEL_RECOVERY_UNAVAILABLE",
                "取消请求无法从当前检定状态恢复，请刷新后重试",
                retryable=True,
            )
        accept_option = next(
            (option for option in check_run.post_roll_options if option.kind == "accept_result"),
            None,
        )
        if accept_option is None:
            raise TurnExecutionError(
                "POST_ROLL_ACCEPT_UNAVAILABLE",
                "当前检定没有可接受的已掷结果",
                retryable=False,
            )
        derived_request_id = self._post_roll_accept_request_id(cancel_id)
        await self._adjudication_engine.decide_post_roll(
            PostRollDecisionRequest(
                request_id=derived_request_id,
                room_id=run.room_id,
                player_id=run.player_id,
                source_revision=execution.view_revision,
                check_id=check_run.check_id,
                check_version=check_run.version,
                option_id=accept_option.option_id,
            )
        )

    @staticmethod
    def _post_roll_accept_request_id(cancel_id: str) -> str:
        derived_request_id = f"{cancel_id}:accept-current"
        if len(derived_request_id) <= 200:
            return derived_request_id
        return "post-roll-accept-" + hashlib.sha256(cancel_id.encode("utf-8")).hexdigest()

    async def mark_narration_persisted(
        self,
        *,
        room_id: str,
        parent_action_id: str,
        on_progress: Callable[[object], Awaitable[None]] | None = None,
    ) -> None:
        active = await self._orchestrator.active_for_room(room_id)
        if (
            active is not None
            and active.parent_action_id == parent_action_id
            and active.status == "awaiting_narration"
        ):
            await self._orchestrator.mark_narration_completed(
                room_id=room_id,
                parent_action_id=parent_action_id,
                on_progress=on_progress,
            )

    async def _from_plan(
        self,
        player_input: PlayerInput,
        result: ActionPlanAdvanceResult,
        *,
        verify_fingerprint: bool = True,
    ) -> ActionPlanTurnResult:
        run = result.run
        if run.status == "waiting_for_player":
            return ActionPlanTurnResult(
                player_input=player_input,
                player_view=result.player_view,
                status=run.status,
                execution=result.latest_execution,
                plan_id=run.plan_id,
            )
        if run.status == "retryable_failure":
            raise TurnExecutionError(
                run.steps[run.current_step_index].safe_failure_code or "PLAN_RETRYABLE_FAILURE",
                "前序步骤已经保存，当前步骤暂时失败；请使用原请求重试",
                retryable=True,
            )
        if run.status not in {
            "awaiting_narration",
            "completed",
            "needs_clarification",
            "cancelled",
            "stopped",
        }:
            raise TurnExecutionError(
                "PLAN_NOT_SETTLED",
                "行动计划尚未到达可返回状态",
                retryable=True,
            )
        context = await self._orchestrator.build_narration_context(
            player_input,
            verify_fingerprint=verify_fingerprint,
        )
        narration = await self._narrate(context)
        return ActionPlanTurnResult(
            player_input=player_input,
            player_view=context.player_view,
            status=run.status,
            execution=result.latest_execution,
            narration=narration,
            plan_id=run.plan_id,
        )

    async def _from_single(
        self,
        player_input: PlayerInput,
        summary: str,
        result: SingleActionTurnResult,
        *,
        recent_history: RecentTurnContext | None = None,
        focus_entity_ids: tuple[str, ...] = (),
    ) -> ActionPlanTurnResult:
        execution = result.execution
        if execution.status in {"awaiting_skill_choice", "awaiting_post_roll_decision"}:
            return ActionPlanTurnResult(
                player_input=player_input,
                player_view=result.player_view,
                status="waiting_for_player",
                execution=execution,
            )
        if execution.outcome == "success":
            completed_outcome = "success"
        elif execution.outcome == "failure":
            completed_outcome = "failure"
        elif execution.outcome == "cancelled":
            completed_outcome = "cancelled"
        else:
            raise TurnExecutionError(
                "PENDING_EXECUTION_NOT_WAITING",
                "行动状态尚未完成，请重试",
                retryable=True,
            )
        if execution.goal_outcome == "pending":
            # 完成态不能把尚未判定的目标交给 Narrator，否则会绕过目标完成门禁。
            raise TurnExecutionError(
                "PENDING_GOAL_OUTCOME_NOT_WAITING",
                "行动目标状态尚未完成，请重试",
                retryable=True,
            )
        completed_summary = CompletedPlanStepSummary(
            step_index=0,
            semantic_goal=summary,
            outcome=completed_outcome,
            goal_outcome=execution.goal_outcome,
            view_revision=execution.view_revision,
            world_time_after=WorldClockView.from_world(result.player_view.world),
            event_refs=execution.public_event_refs,
            narration_evidence=execution.narration_evidence,
            committed_results=execution.committed_results,
        )
        context = ActionPlanNarrationContext(
            background=result.player_view.background,
            player_input=player_input,
            plan_goal=summary,
            termination_status=("cancelled" if execution.status == "cancelled" else "resolved"),
            completed_steps=(completed_summary,),
            player_view=result.player_view,
            recent_history=self._rebind_recent_history(
                recent_history,
                player_view=result.player_view,
            ),
            focus_entity_ids=focus_entity_ids,
            opening_world_time=result.opening_world_time,
            allowed_evidence_refs=execution.public_event_refs,
            narration_evidence=execution.narration_evidence,
        )
        return ActionPlanTurnResult(
            player_input=player_input,
            player_view=result.player_view,
            status="completed",
            execution=execution,
            narration=await self._narrate(context),
        )

    async def _from_single_clarification(
        self,
        player_input: PlayerInput,
        summary: str,
        result: SingleActionClarificationResult,
        *,
        recent_history: RecentTurnContext | None = None,
    ) -> ActionPlanTurnResult:
        """把未发生任何权威写入的单动作失败转换成自然主持人澄清。"""

        context = ActionPlanNarrationContext(
            background=result.player_view.background,
            player_input=player_input,
            plan_goal=summary,
            termination_status="needs_clarification",
            player_view=result.player_view,
            recent_history=self._rebind_recent_history(
                recent_history,
                player_view=result.player_view,
            ),
            opening_world_time=result.opening_world_time,
            blocked_step_goal=summary,
            player_safe_failure_reason=result.player_safe_reason,
        )
        return ActionPlanTurnResult(
            player_input=player_input,
            player_view=result.player_view,
            status="needs_clarification",
            narration=await self._narrate(context),
        )

    @staticmethod
    def _rebind_recent_history(
        recent_history: RecentTurnContext | None,
        *,
        player_view: PlayerView,
    ) -> RecentTurnContext | None:
        """将提交前读取的安全历史绑定到本回合最终视图 revision。"""

        if recent_history is None or recent_history.as_of_revision == player_view.revision:
            return recent_history
        # 历史查询已排除当前 client_action_id；权威提交只会推进 revision，
        # 因此这里仅更新读取截止点，不增加或改写任何历史事实。
        return recent_history.model_copy(update={"as_of_revision": player_view.revision})

    async def _narrate(
        self,
        context: ActionPlanNarrationContext,
    ) -> ActionPlanNarrationOutput:
        if any(
            getattr(step, "goal_outcome", "legacy_unknown")
            in {"partially_achieved", "not_achieved"}
            for step in context.completed_steps
        ):
            # 未完成持久目标时不让自由文本从检定成功外推伤势、死亡或物品变化。
            return self._deterministic_narration_fallback(context)
        for attempt in range(2):
            try:
                return await self._narrator.narrate(context)
            except ActionPlanNarrationValidationError as exc:
                # 只记录校验类别和权威结果，不记录模型正文或其他敏感上下文。
                logger.warning(
                    "action_plan_narration_rejected",
                    action=context.player_input.client_action_id,
                    attempt=attempt + 1,
                    reason=exc.reason,
                    outcomes=tuple(step.outcome for step in context.completed_steps),
                    termination_status=context.termination_status,
                )
                if attempt == 0 and exc.reason == "required_evidence_missing":
                    missing = tuple(
                        item for item in context.narration_evidence if item.required_in_narration
                    )
                    context = context.model_copy(
                        update={
                            "narration_retry_hint": (
                                "上一版叙事遗漏了已提交的玩家可见结果："
                                + "、".join(item.subject_name for item in missing)
                                + "。必须在正文明确写出，并 claim 对应 evidence ref。"
                            )
                        }
                    )
                if attempt == 1:
                    if (
                        exc.reason == "required_evidence_missing"
                        and context.termination_status != "needs_clarification"
                    ):
                        logger.info(
                            "action_plan_narration_required_evidence_fallback",
                            evidence_refs=[
                                item.ref
                                for item in context.narration_evidence
                                if item.required_in_narration
                            ],
                        )
                        return self._required_evidence_fallback(context)
                    return self._deterministic_narration_fallback(context)
            except Exception as exc:
                # 传输层的瞬态失败已经由 StructuredJsonClient 自己重试过了
                # （见 adapters/structured_http.py）。在这里再整体重试一轮，两层
                # 是相乘的。规则结果已经提交时直接使用结构化证据生成确定性回复，
                # 避免 Narrator 故障长期占用房间，也绝不重新执行 Engine。
                logger.warning(
                    "action_plan_narration_model_fallback",
                    action=context.player_input.client_action_id,
                    error_type=type(exc).__name__,
                    termination_status=context.termination_status,
                )
                required = tuple(
                    item for item in context.narration_evidence if item.required_in_narration
                )
                if required and context.termination_status != "needs_clarification":
                    return self._required_evidence_fallback(context)
                return self._deterministic_narration_fallback(context)
        raise AssertionError("unreachable")

    @staticmethod
    def _required_evidence_fallback(
        context: ActionPlanNarrationContext,
    ) -> ActionPlanNarrationOutput:
        required = tuple(item for item in context.narration_evidence if item.required_in_narration)
        if not required:
            raise TurnExecutionError(
                "PLAN_NARRATION_INVALID",
                "规则结果已保存，但叙事未通过安全校验；请使用原请求重试",
                retryable=True,
            )
        sentences: list[str] = []
        for item in required:
            sentences.append(f"随着调查深入，你很快辨认出{item.subject_name}。")
            description = item.description.strip()
            if description:
                sentences.append(description.rstrip("。！？!?；;，,") + "。")
        return ActionPlanNarrationOutput(
            text="".join(sentences),
            claimed_evidence_refs=tuple(item.ref for item in required),
        )

    @staticmethod
    def _deterministic_narration_fallback(
        context: ActionPlanNarrationContext,
    ) -> ActionPlanNarrationOutput:
        """只复述结构化已提交结果，绝不从 semantic_goal 推断持久后果。"""

        if context.termination_status == "needs_clarification":
            visible_dead = tuple(
                entity
                for entity in context.player_view.scene.visible_entities
                if any(
                    state.key == "consciousness" and state.value == "dead"
                    for state in entity.observable_state
                )
            )
            if visible_dead and any(
                word in context.player_input.utterance for word in ("尸体", "遗体")
            ):
                names = "、".join(entity.name for entity in visible_dead)
                return ActionPlanNarrationOutput(
                    kind="clarification",
                    text=(
                        f"{names}的尸体就在当前场景中。你想检查尸体、搜查随身物品，还是处理现场？"
                    ),
                )
            return ActionPlanNarrationOutput(
                kind="clarification",
                text=_deterministic_clarification_text(context),
            )
        labels = {
            ("consciousness", "unconscious"): "失去了意识",
            ("consciousness", "dead"): "已经死亡",
            ("posture", "prone"): "已经倒地",
            ("restraint", "restrained"): "已被束缚",
            ("injury", "minor"): "受了轻伤",
            ("injury", "major"): "受了重伤",
            ("injury", "critical"): "伤势危重",
            ("open", True): "已经打开",
            ("locked", True): "已经锁住",
            ("broken", True): "已经损坏",
        }
        names = {entity.id: entity.name for entity in context.player_view.scene.visible_entities}
        inventory_names = {
            item.id: item.name for item in getattr(context.player_view, "inventory", ())
        }
        results = [
            (result, labels.get((result.state_key, result.state_value)))
            for step in context.completed_steps
            for result in step.committed_results
        ]
        statements = [
            f"{names.get(result.target_id, '目标')}{label}。"
            for result, label in results
            if label is not None
        ]
        inventory_results = tuple(
            result
            for result, _label in results
            if result.kind == "inventory" and result.target_id in inventory_names
        )
        statements.extend(
            f"{inventory_names[result.target_id]}已经放入你的背包。" for result in inventory_results
        )
        refs = tuple(
            result.event_ref
            for result, label in results
            if label is not None or result in inventory_results
        )
        outcomes = tuple(step.outcome for step in context.completed_steps)
        # 历史上下文没有目标完成字段，必须按未知处理，不能倒推出目标已经达成。
        goal_outcomes = tuple(
            getattr(step, "goal_outcome", "legacy_unknown") for step in context.completed_steps
        )
        if "cancelled" in outcomes or context.termination_status == "cancelled":
            status_text = "这次行动已经取消。"
        elif any(item in {"partially_achieved", "not_achieved"} for item in goal_outcomes):
            status_text = "检定或过程已经结束，但玩家声明的完整目标没有形成可确认的权威结果。"
        elif "failure" in outcomes:
            # ActionPlan 可能保留此前成功步骤，因此失败文案要区分全部失败和部分完成。
            status_text = (
                "当前步骤未能成功；此前已经完成的步骤仍然保留。"
                if "success" in outcomes
                else "这次行动未能成功，局面没有产生当前可确认的新结果。"
            )
        else:
            status_text = "这次行动已经按当前可确认的结果完成。"
        if outcomes and outcomes[-1] != "success":
            fallback_text = status_text + "".join(statements)
        else:
            fallback_text = "".join(statements) or status_text
        return ActionPlanNarrationOutput(
            # 失败或取消时即使存在失败分支效果，也必须先明确行动结果，不能让
            # 玩家把后面的状态变化误读成目标已经成功达成。
            text=fallback_text,
            claimed_evidence_refs=refs,
        )

    async def _keeper_capabilities(
        self,
        player_input: PlayerInput,
        player_view: PlayerView,
    ) -> KeeperCapabilityView | None:
        try:
            return await self._projector.keeper_capabilities(
                player_input,
                expected_revision=player_view.revision,
            )
        except (AttributeError, NotImplementedError):
            return None

    async def _read_recent_history(
        self,
        *,
        player_input: PlayerInput,
        player_view: PlayerView,
    ) -> RecentTurnContext:
        recent_history = RecentTurnContext.empty(
            player_input=player_input,
            player_view=player_view,
        )
        if not self._recent_history_enabled:
            return recent_history
        try:
            recent_history = await self._recent_history_source.read(
                player_input=player_input,
                player_view=player_view,
                exclude_correlation_id=player_input.client_action_id,
                budget=self._recent_history_budget,
            )
            recent_history.validate_for(
                player_input=player_input,
                player_view=player_view,
            )
        except (ValidationError, ContractError, ValueError) as exc:
            raise TurnExecutionError(
                "RECENT_HISTORY_INVALID",
                "近期历史未通过安全校验，本次动作未执行",
                retryable=False,
            ) from exc
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            logger.warning(
                "action_plan_recent_history_degraded",
                room_id=player_input.room_id,
                correlation_id=player_input.client_action_id,
                error_type=type(exc).__name__,
            )
            return RecentTurnContext.empty(
                player_input=player_input,
                player_view=player_view,
            )
        return recent_history

    async def _resolve_actor_id(self, room_id: str, player_id: str) -> str:
        async with self._store.transaction(room_id) as transaction:
            runtime = await transaction.load_runtime()
        actors = [
            actor_id
            for actor_id, actor in runtime.game_state.actors.items()
            if actor.player_id == player_id
        ]
        if len(actors) != 1:
            raise TurnExecutionError(
                "ACTOR_NOT_CONTROLLED",
                "当前玩家没有唯一可控制的局内角色",
                retryable=False,
            )
        return actors[0]


class _EmptyRecentHistorySource:
    async def read(
        self,
        *,
        player_input: PlayerInput,
        player_view: PlayerView,
        exclude_correlation_id: str,
        budget: RecentHistoryBudget,
    ) -> RecentTurnContext:
        del exclude_correlation_id, budget
        return RecentTurnContext.empty(player_input=player_input, player_view=player_view)


def build_action_plan_turn_application(
    *,
    store: EngineStore,
    engine: RuleEngineService,
    adjudication_engine: AdjudicationEngineService,
    plan_store=None,
    settings=None,
    client=None,
    recent_history_source: RecentHistorySource | None = None,
) -> ActionPlanTurnApplication:
    """Compose the finite-plan path without changing the single-intent Engine."""

    from app.adapters import (
        DeepSeekChatCompletionsJsonClient,
        OpenAIResponsesJsonClient,
        PromptActionPlanNarrationModel,
        PromptActionPlanStepAdjudicator,
        PromptHostTurnDecisionModel,
        QwenChatCompletionsJsonClient,
    )
    from app.core.config import get_settings, model_client_retry_policy, secret_value

    resolved = settings or get_settings()
    policy = ActionPlanPolicy(
        max_plan_steps=resolved.action_plan_max_steps,
        max_steps_per_advance=resolved.action_plan_max_steps_per_advance,
        max_repair_attempts=resolved.action_plan_max_repair_attempts,
    )
    if resolved.host_model_provider == "fake":
        planner = DeterministicHostTurnDecisionModel()
        adjudicator = _DeterministicStepAdjudicator()
        narration_model = DeterministicActionPlanNarrationModel()
    else:
        if client is None:
            if resolved.host_model_provider == "deepseek":
                client_type = DeepSeekChatCompletionsJsonClient
                api_key = resolved.deepseek_api_key
                base_url = resolved.deepseek_base_url
                model = resolved.deepseek_model
                timeout = resolved.deepseek_timeout_seconds
            elif resolved.host_model_provider == "qwen":
                client_type = QwenChatCompletionsJsonClient
                api_key = resolved.qwen_api_key
                base_url = resolved.qwen_base_url
                model = resolved.qwen_model
                timeout = resolved.qwen_timeout_seconds
            else:
                client_type = OpenAIResponsesJsonClient
                api_key = resolved.openai_api_key
                base_url = resolved.openai_base_url
                model = resolved.openai_model
                timeout = resolved.openai_timeout_seconds
            if api_key is None:
                raise ValueError("ActionPlan Host 模型缺少 API key")
            client = client_type(
                api_key=secret_value(api_key),
                base_url=base_url,
                model=model,
                timeout_seconds=timeout,
                retry_policy=model_client_retry_policy(resolved),
            )
        planner = PromptHostTurnDecisionModel(
            client,
            policy=policy,
        )
        adjudicator = _RuleFirstStepAdjudicator(
            PromptActionPlanStepAdjudicator(client),
        )
        narration_model = PromptActionPlanNarrationModel(client)

    plan_store = plan_store or InMemoryActionPlanRunStore()
    projector = PlayerViewProjector(engine)
    recent_history_budget = RecentHistoryBudget(
        max_turns=resolved.recent_history_max_turns,
        max_chars=resolved.recent_history_max_chars,
    )
    history_source = recent_history_source or _EmptyRecentHistorySource()
    orchestrator = ActionPlanOrchestrator(
        store=plan_store,
        adjudicator=adjudicator,
        executor=adjudication_engine,
        player_view_projector=projector,
        policy=policy,
        on_step_failure=_log_step_adjudication_failure,
        recent_history_source=(history_source if resolved.recent_history_enabled else None),
        recent_history_budget=recent_history_budget,
    )
    return ActionPlanTurnApplication(
        store=store,
        engine=engine,
        adjudication_engine=adjudication_engine,
        planner=planner,
        orchestrator=orchestrator,
        narrator=ActionPlanNarrator(narration_model),
        recent_history_source=history_source,
        recent_history_budget=recent_history_budget,
        recent_history_enabled=resolved.recent_history_enabled,
    )


class _DeterministicStepAdjudicator:
    # Deliberately conservative: the offline composition only resolves steps
    # fully implied by the safe view, then falls back to narrative-only.

    async def adjudicate(self, context: ActionPlanStepContext) -> SingleActionProposal:
        adjudication = _deterministic_step_adjudication(context)
        if adjudication is not None:
            return _proposal_from_adjudication(adjudication)

        action_text = context.step.semantic_goal.replace(
            context.player_view.scene.name,
            "",
        ).strip(" ，,。")
        target = _match_visible_entity(context.player_view, action_text)
        target_kind = "entity" if target is not None else "location"
        target_id = target.id if target is not None else context.player_view.scene.id
        return _proposal_from_adjudication(
            ActionAdjudication(
                request_id=context.step_request_id,
                source_revision=context.player_view.revision,
                actor_id=context.player_input.actor_id,
                summary=context.step.semantic_goal,
                target=ActionTarget(kind=target_kind, id=target_id),
                method=ActionMethod(
                    family=context.step.kind,
                    description=context.step.semantic_goal,
                ),
                check=NoAdjudicationCheck(),
                success_effects=(NarrativeOnlyEffect(),),
            )
        )


class _RuleFirstStepAdjudicator:
    """Resolve unambiguous Match View steps without a fallible model round-trip."""

    def __init__(
        self,
        fallback: ActionPlanStepAdjudicator,
    ) -> None:
        self._fallback = fallback

    async def adjudicate(self, context: ActionPlanStepContext) -> SingleActionProposal:
        # 确定性路径只处理当前 PlayerView 已完整证明的动作；无法确定时才调用
        # Host。整个异常收束由 ActionPlanOrchestrator 的步骤冻结边界统一负责。
        adjudication = _deterministic_step_adjudication(context)
        if adjudication is not None:
            return _proposal_from_adjudication(adjudication)
        return await self._fallback.adjudicate(context)


def _deterministic_step_adjudication(
    context: ActionPlanStepContext,
) -> ActionAdjudication | None:
    """Return only decisions fully implied by the current player-safe view."""

    if context.step.kind in {"wait", "rest"}:
        time = context.keeper_capabilities.time if context.keeper_capabilities else None
        if time is not None and time.blocked_reason is None and time.next_point_id:
            # 「休息到晚上」「睡到八点」要跳几个时间点，取决于玩家说的是哪个
            # 时间——这是语义问题，确定性分支答不了。把它交给模型，由它按
            # keeper_capabilities.time 数出 advance_world_time 的次数，Engine
            # 仍然逐个校验每一跳是不是时间线上的下一个点。
            return None
        return ActionAdjudication(
            request_id=context.step_request_id,
            source_revision=context.player_view.revision,
            actor_id=context.player_input.actor_id,
            summary=context.step.semantic_goal,
            target=ActionTarget(kind="location", id=context.player_view.scene.id),
            method=ActionMethod(
                family=context.step.kind,
                description=context.step.semantic_goal,
            ),
            check=NoAdjudicationCheck(),
            # 时间推不动（多人房间还没有 ready 门禁，或模组没有下一个时间点）时，
            # 等待/休息就只是一次叙事停留，不改变任何权威状态。
            success_effects=(),
        )

    if context.step.kind == "travel":
        destination = _match_travel_target(context.player_view, context.step.semantic_goal)
        if context.plan_id == "single-action":
            # 单动作修复时玩家原话优先；真正的多步计划必须逐步使用 semantic_goal，
            # 否则“先去办公室再回墓地”的第一步也会被原话最终目的地覆盖。
            destination = (
                _match_travel_target(
                    context.player_view,
                    context.player_input.utterance,
                )
                or destination
            )
        if destination is None:
            # A small set of obvious venues has a deterministic fast path. This
            # is not a creation allowlist: every other requested location still
            # falls through to the Agent's WorldProfile/Canon-aware judgement.
            return _ambient_venue_adjudication(context)
        if destination.id == context.player_view.scene.id:
            # “去旅馆”也可能是在创建旅馆后的下一轮再次指向同一地点。此时行动
            # 已经满足，不应重复提交 enter_location，更不应因零位置变化要求澄清。
            return ActionAdjudication(
                request_id=context.step_request_id,
                source_revision=context.player_view.revision,
                actor_id=context.player_input.actor_id,
                summary=f"已经位于{destination.name}",
                target=ActionTarget(kind="location", id=destination.id),
                method=ActionMethod(
                    family="action",
                    description=f"确认当前已在{destination.name}",
                ),
                persistence_intent="none",
                check=NoAdjudicationCheck(),
                success_effects=(NarrativeOnlyEffect(),),
            )
        destination_id = destination.id
        companion_moves = _companion_move_effects(
            player_input=context.player_input,
            semantic_text=context.step.semantic_goal,
            view=context.player_view,
            capabilities=context.keeper_capabilities,
            destination_id=destination_id,
        )
        return ActionAdjudication(
            request_id=context.step_request_id,
            source_revision=context.player_view.revision,
            actor_id=context.player_input.actor_id,
            summary=context.step.semantic_goal,
            target=ActionTarget(kind="location", id=destination_id),
            method=ActionMethod(
                family="travel",
                description=context.step.semantic_goal,
            ),
            check=NoAdjudicationCheck(),
            success_effects=(
                EnterLocationEffect(location_id=destination_id),
                *companion_moves,
            ),
        )

    action_text = context.step.semantic_goal.replace(
        context.player_view.scene.name,
        "",
    ).strip(" ，,。")
    target = _match_visible_entity(context.player_view, action_text)
    candidate, option = _match_rule_candidate(
        context.keeper_capabilities,
        action_text,
        target.id if target is not None else None,
    )
    if candidate is not None and option is not None:
        target_kind = (
            candidate.target_kinds[0]
            if candidate.target_kinds
            else "entity"
            if target is not None
            else "location"
        )
        # 不掷骰的分支（例如 proceed）不能为了凑格式编一个技能出来：option id
        # 不是技能名，`proceed` / `STR` 提交上去会被 Ruleset 快照拒绝。带检定的
        # 分支才沿用 option id 作技能，Engine 仍会再校验一次。
        check = (
            RequiredAdjudicationCheck(
                candidates=(
                    SkillCheckCandidate(
                        candidate_id=option.id,
                        skill_id=option.id,
                        difficulty="regular",
                        method_summary=context.step.semantic_goal,
                        player_safe_reason="使用当前地点公开的检定方式",
                    ),
                )
            )
            if option.requires_check
            else NoAdjudicationCheck()
        )
        return ActionAdjudication(
            request_id=context.step_request_id,
            source_revision=context.player_view.revision,
            actor_id=context.player_input.actor_id,
            summary=context.step.semantic_goal,
            target=ActionTarget(
                kind=target_kind,
                id=(
                    candidate.target_ids[0]
                    if candidate.target_ids
                    else target.id
                    if target is not None
                    else context.player_view.scene.id
                ),
            ),
            method=ActionMethod(
                family=(
                    candidate.action_families[0] if candidate.action_families else context.step.kind
                ),
                description=context.step.semantic_goal,
            ),
            rule_decision=RuleDecisionRef(rule_id=candidate.rule_id, option_id=option.id),
            check=check,
            # Effects belong to the rule (#226 §5), not to this stand-in.
            success_effects=(),
            failure_effects=(),
        )

    # Once the planner has identified a visible conversation partner, ordinary
    # dialogue needs no second model call to invent an adjudication.  Keeping
    # this path narrative-only is also an information boundary: authored rules
    # remain the only way to reveal facts or mutate state.
    if context.step.kind == "dialogue" and target is not None:
        return ActionAdjudication(
            request_id=context.step_request_id,
            source_revision=context.player_view.revision,
            actor_id=context.player_input.actor_id,
            summary=context.step.semantic_goal,
            target=ActionTarget(kind="entity", id=target.id),
            method=ActionMethod(
                family="talk",
                description=context.step.semantic_goal,
            ),
            check=NoAdjudicationCheck(),
            success_effects=(NarrativeOnlyEffect(),),
        )
    return None


def _match_visible_entity(view: PlayerView, text: str):
    matches = []
    for entity in view.scene.visible_entities:
        overlap = _best_label_overlap(text, (entity.id, entity.name, *entity.aliases))
        if overlap is not None:
            matches.append((len(overlap), entity.id, entity))
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], item[1]))
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        return None
    return matches[0][2]


def _companion_move_effects(
    *,
    player_input: PlayerInput,
    semantic_text: str,
    view: PlayerView,
    capabilities: KeeperCapabilityView | None,
    destination_id: str,
) -> tuple[MoveEntityEffect, ...]:
    """把玩家明确要求同行、且当前就在身边的 NPC 一并移动到目的地。"""

    effects = []
    for entity in _requested_companions(
        player_input=player_input,
        semantic_text=semantic_text,
        capabilities=capabilities,
    ):
        if entity.location_id != view.scene.id:
            continue
        effects.append(
            MoveEntityEffect(
                entity_id=entity.id,
                location_id=destination_id,
            )
        )
    return tuple(effects)


def _requested_companions(
    *,
    player_input: PlayerInput,
    semantic_text: str,
    capabilities: KeeperCapabilityView | None,
):
    """从玩家同行措辞及模型语义消解中找出明确提及的 Canon NPC。"""

    if capabilities is None or not any(
        marker in player_input.utterance for marker in ("带", "一起", "同行")
    ):
        return ()
    combined = f"{player_input.utterance} {semantic_text}"
    requested = []
    for entity in capabilities.entities:
        if entity.kind != "npc":
            continue
        # KeeperCapability 目前没有 aliases；中文音译姓名通常以间隔号分段，首段
        # 可以覆盖“托马斯”对应“托马斯·金博尔”这类玩家常用简称。
        short_name = entity.name.split("·", 1)[0]
        labels = (entity.id, entity.name, short_name)
        if any(label and label in combined for label in labels):
            requested.append(entity)
    return tuple(requested)


def _match_rule_candidate(capabilities, text: str, target_id: str | None):
    """Pick at most one v3 Rule the player's words clearly mean.

    The Fake stands in for the Agent's semantic judgement, so it only matches on
    the player-safe hints the Match View published — it never reads the module.
    Ambiguity yields nothing: guessing between two rules is exactly the mistake
    a real Agent would be asked not to make.

    Option hints have to participate in that judgement, not just candidate hints.
    In the published fixture every rule aimed at the same NPC carries that NPC's
    name as its candidate hint, and the word that actually tells them apart
    （"侦查" / "贿赂" / "威吓"）lives on the options. Scoring candidate hints alone
    therefore made all four caretaker rules tie on every utterance, and the tie
    was resolved as "no match" — the Fake could never reach a rule at all.
    """

    if capabilities is None:
        return None, None
    scored = []
    for candidate in capabilities.rule_candidates:
        if target_id is not None and candidate.target_ids and target_id not in candidate.target_ids:
            continue
        family_hits = [
            hint
            for family in candidate.action_families
            for hint in _ACTION_FAMILY_HINTS.get(family, ())
            if hint in text
        ]
        candidate_hits = [hint for hint in candidate.semantic_hints if hint and hint in text]
        best_option = None
        best_option_hit = 0
        for option in candidate.options:
            hits = [hint for hint in option.semantic_hints if hint and hint in text]
            if hits and max(len(hint) for hint in hits) > best_option_hit:
                best_option = option
                best_option_hit = max(len(hint) for hint in hits)
        if not family_hits and not candidate_hits and best_option is None:
            continue
        # Option evidence outranks candidate evidence: sibling rules share the
        # target's name, so only the option words carry discriminating power.
        score = (
            best_option_hit,
            max((len(hint) for hint in family_hits), default=0),
            max((len(hint) for hint in candidate_hits), default=0),
        )
        scored.append((score, candidate, best_option))
    if not scored:
        return None, None
    best = max(score for score, _, _ in scored)
    finalists = [(candidate, option) for score, candidate, option in scored if score == best]
    if len(finalists) != 1:
        return None, None
    candidate, option = finalists[0]
    if option is not None:
        return candidate, option
    return candidate, candidate.options[0] if candidate.options else None


# Match View action families are stable contract identifiers. These localized
# words merely recognize the player's explicit verb; they do not add a rule or
# reveal module-only facts. Ties still yield no match below.
_ACTION_FAMILY_HINTS: dict[str, tuple[str, ...]] = {
    "observe": ("仔细观察", "观察", "察看", "查看"),
    "search": ("搜索", "搜查", "查找", "找线索", "寻找"),
    "research": ("研究", "查阅", "检索", "翻阅", "查旧报"),
    "social": ("留下好印象", "博取信任", "说服"),
    "intimidate": ("恐吓", "威吓"),
    "bribe": ("贿赂", "收买"),
}


__all__ = [
    "ActionPlanTurnApplication",
    "ActionPlanTurnResult",
    "DeterministicActionPlanNarrationModel",
    "DeterministicHostTurnDecisionModel",
    "HostTurnDecisionModel",
    "build_action_plan_turn_application",
]


def _production_application() -> ActionPlanTurnApplication:
    from app.adapters import SqlAlchemyRecentHistorySource
    from app.core.db import async_session_factory
    from app.core.engine import (
        action_plan_store,
        adjudication_engine_service,
        engine_store,
        rule_engine_service,
    )

    return build_action_plan_turn_application(
        store=engine_store,
        engine=rule_engine_service,
        adjudication_engine=adjudication_engine_service,
        plan_store=action_plan_store,
        recent_history_source=SqlAlchemyRecentHistorySource(async_session_factory),
    )


action_plan_turn_application = _production_application()
__all__.append("action_plan_turn_application")
