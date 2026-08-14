"""把基线场景接入真实 ActionPlan、Engine 与 Narrator 内存执行链。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from collaboration_framework.contracts import (
    ActionAdjudication,
    CheckDecisionRequest,
    ModuleContentV3,
    PostRollDecisionRequest,
    SelectCheckChoice,
    SingleActionDecision,
)
from collaboration_framework.engine import (
    AdjudicationEngineService,
    DiceRoller,
    InMemoryEngineStore,
    RuleEngineService,
    SequenceDiceSource,
)
from collaboration_framework.engine.initialization import create_initial_game_state
from collaboration_framework.engine.models import ActorResources, ActorState, GameState
from collaboration_framework.host.application import ActionPlanNarrator
from collaboration_framework.host.schemas import ActionPlanNarrationContext, HostAgentContext

from app.core.action_plan_turn import build_action_plan_turn_application
from app.core.config import Settings

from .contracts import BaselineScenario, BaselineTurn, BaselineTurnResult

MODULE_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "agent-collaboration-framework"
    / "docs"
    / "module-parser"
    / "examples"
    / "module-content-validation"
    / "追书人"
    / "module-content-v3.json"
)


class _ScriptedPlanner:
    """将场景中的受控输出补全为正式 HostTurnDecision 契约。"""

    def __init__(self) -> None:
        self.outputs: dict[str, dict[str, object]] = {}
        self.aliases: Mapping[str, str] = {}

    async def generate(self, context: HostAgentContext) -> SingleActionDecision:
        raw = self.outputs.get(context.player_input.client_action_id) or {}
        resolved = _resolve_aliases(raw, self.aliases)
        target = resolved.get(
            "target",
            {"kind": "location", "id": context.player_view.scene.id},
        )
        method = resolved.get(
            "method",
            {"family": "interact", "description": context.player_input.utterance},
        )
        payload = {
            "request_id": context.player_input.client_action_id,
            "source_revision": context.player_view.revision,
            "actor_id": context.player_input.actor_id,
            "summary": context.player_input.utterance,
            "target": target,
            "method": method,
            "persistence_intent": resolved.get("persistence_intent", "none"),
            "check": resolved.get("check", {"mode": "none", "candidates": []}),
            "success_effects": resolved.get("success_effects", [{"type": "narrative_only"}]),
            "failure_effects": resolved.get("failure_effects", []),
        }
        return SingleActionDecision(adjudication=ActionAdjudication.model_validate(payload))


class _EvidenceNarrationModel:
    """只复述提交证据的确定性 Narrator，避免自然语言快照。"""

    def __init__(self) -> None:
        self.outputs: dict[str, dict[str, object]] = {}

    async def generate(self, context: ActionPlanNarrationContext) -> dict[str, object]:
        scripted = self.outputs.get(context.player_input.client_action_id)
        if scripted is not None:
            return dict(scripted)
        required_names = [
            item.subject_name for item in context.narration_evidence if item.required_in_narration
        ]
        text = "、".join(required_names) if required_names else "这次行动已经完成。"
        return {
            "kind": "narration",
            "text": text,
            "claimed_evidence_refs": [item.ref for item in context.narration_evidence],
            "suggested_actions": [],
        }


class InMemoryRuntimeAdapter:
    """使用真实内存 Engine 执行场景，并输出去随机化的结构结果。"""

    room_id = "baseline-room"
    player_id = "baseline-player"
    actor_id = "baseline-actor"

    def __init__(self) -> None:
        self._store: InMemoryEngineStore | None = None
        self._adjudication_engine: AdjudicationEngineService | None = None
        self._application = None
        self._planner = _ScriptedPlanner()
        self._narration_model = _EvidenceNarrationModel()
        self._scenario: BaselineScenario | None = None

    async def prepare(self, scenario: BaselineScenario) -> Mapping[str, str]:
        """从发布模组重建权威状态，并安装场景专属的受控模型输出。"""

        content = ModuleContentV3.model_validate_json(MODULE_FIXTURE.read_text(encoding="utf-8"))
        actor = ActorState(
            player_id=self.player_id,
            name="基线调查员",
            source_character_id="baseline-character",
            source_character_version=1,
            state={
                "skills": {
                    "library-use": 60,
                    "spot-hidden": 60,
                    "luck": 60,
                    "fighting-brawl": 60,
                },
                "skill_labels": {
                    "library-use": "图书馆使用",
                    "spot-hidden": "侦查",
                    "luck": "幸运",
                    "fighting-brawl": "斗殴",
                },
            },
            resources=ActorResources(luck=60),
        )
        state = create_initial_game_state(
            content,
            room_id=self.room_id,
            actors={self.actor_id: actor},
        )
        state = _apply_initial_state(state, scenario.initial_state.state)
        self._store = InMemoryEngineStore()
        self._store.register_room(module_content=content, initial_state=state)
        engine = RuleEngineService(self._store)
        self._adjudication_engine = AdjudicationEngineService(
            self._store,
            dice=DiceRoller(SequenceDiceSource([64, 24, 10, 42])),
        )
        self._compose_application(engine)
        aliases = {
            "@player": self.actor_id,
            "@scene": state.scene_id,
            "@world": content.world_ref,
            **scenario.initial_state.aliases,
        }
        self._install_scripted_models(scenario, aliases)
        self._scenario = scenario
        return aliases

    def _compose_application(self, engine: RuleEngineService | None = None) -> None:
        """使用当前 Store 重建应用对象，供进程边界恢复场景复用。"""

        if self._store is None or self._adjudication_engine is None:
            raise RuntimeError("adapter 尚未 prepare")
        resolved_engine = engine or RuleEngineService(self._store)
        self._application = build_action_plan_turn_application(
            store=self._store,
            engine=resolved_engine,
            adjudication_engine=self._adjudication_engine,
            settings=Settings(
                host_model_provider="fake",
                opening_narration_mode="template",
                recent_history_enabled=False,
            ),
        )

    def _install_scripted_models(
        self,
        scenario: BaselineScenario,
        aliases: Mapping[str, str],
    ) -> None:
        """在重建后的 Application 上恢复同一组确定性 Host/Narrator。"""

        if self._application is None:
            raise RuntimeError("application 尚未创建")
        self._planner.aliases = aliases
        self._planner.outputs = {
            turn.client_action_id: turn.host_output or {} for turn in scenario.turns
        }
        self._narration_model.outputs = {
            turn.client_action_id: turn.narrator_output
            for turn in scenario.turns
            if turn.narrator_output is not None
        }
        self._application._planner = self._planner
        self._application._narrator = ActionPlanNarrator(self._narration_model)

    def rebuild_application(self) -> None:
        """模拟进程重启后的依赖重组，同时保留已提交的权威 Store。"""

        if self._scenario is None:
            raise RuntimeError("adapter 尚未 prepare")
        aliases = self._planner.aliases
        self._compose_application()
        self._install_scripted_models(self._scenario, aliases)

    async def execute_turn(
        self,
        turn: BaselineTurn,
        *,
        aliases: Mapping[str, str],
    ) -> BaselineTurnResult:
        """执行输入并自动完成确定性技能选择和掷骰后确认。"""

        del aliases
        if self._application is None or self._store is None:
            raise RuntimeError("adapter 尚未 prepare")
        before_events = len(self._store.inspect_domain_events(self.room_id))
        phases: list[str] = []

        async def observe_phase(phase: str) -> None:
            phases.append(phase)

        result = await self._application.start(
            room_id=self.room_id,
            player_id=self.player_id,
            client_action_id=turn.client_action_id,
            utterance=turn.utterance,
            on_phase=observe_phase,
        )
        result = await self._settle_check(result, turn, observe_phase)
        events = self._store.inspect_domain_events(self.room_id)
        new_events = events[before_events:]
        state = self._store.inspect_state(self.room_id)
        execution = result.execution
        check_run = execution.check_run if execution is not None else None
        narration = result.narration
        return BaselineTurnResult(
            client_action_id=turn.client_action_id,
            status=result.status,
            phases=tuple(phases),
            event_types=tuple(event.type for event in new_events),
            state=_flatten_state(state),
            narration_evidence=(narration.claimed_evidence_refs if narration is not None else ()),
            narration_claims=(narration.claimed_evidence_refs if narration is not None else ()),
            roll_ids=(
                (f"{turn.client_action_id}:roll:{check_run.roll_count}",)
                if check_run is not None
                else ()
            ),
            event_ids=tuple(
                f"{event.client_action_id}:{event.type}:{event.sequence}" for event in new_events
            ),
            state_versions=tuple(event.sequence for event in new_events),
        )

    async def _settle_check(self, result, turn: BaselineTurn, observe_phase):
        """基线玩家总是选择首个技能，并优先使用可用幸运修正。"""

        if self._adjudication_engine is None or self._application is None:
            raise RuntimeError("adapter 尚未 prepare")
        execution = result.execution
        if execution is None or execution.status != "awaiting_skill_choice":
            return result
        pending = execution.pending_decision
        if pending is None:
            raise AssertionError("awaiting_skill_choice 必须包含 pending_decision")
        execution = await self._adjudication_engine.decide(
            CheckDecisionRequest(
                request_id=f"{turn.client_action_id}:select",
                room_id=self.room_id,
                player_id=self.player_id,
                source_revision=execution.view_revision,
                decision_id=pending.decision_id,
                decision_version=pending.decision_version,
                choice=SelectCheckChoice(candidate_id=pending.options[0].candidate_id),
            )
        )
        if execution.status == "awaiting_post_roll_decision" and execution.check_run:
            options = execution.check_run.post_roll_options
            chosen = next(
                (option for option in options if option.kind == "spend_resource"),
                next(option for option in options if option.kind == "accept_result"),
            )
            await self._adjudication_engine.decide_post_roll(
                PostRollDecisionRequest(
                    request_id=f"{turn.client_action_id}:post-roll",
                    room_id=self.room_id,
                    player_id=self.player_id,
                    source_revision=execution.view_revision,
                    check_id=execution.check_run.check_id,
                    check_version=execution.check_run.version,
                    option_id=chosen.option_id,
                )
            )
        return await self._application.resume_single(
            room_id=self.room_id,
            player_id=self.player_id,
            parent_action_id=turn.client_action_id,
            on_phase=observe_phase,
        )

    async def close(self) -> None:
        """释放场景引用，确保下一场景从全新内存状态开始。"""

        self._application = None
        self._adjudication_engine = None
        self._store = None
        self._scenario = None


def _resolve_aliases(value: Any, aliases: Mapping[str, str]) -> Any:
    """递归替换逻辑别名，不允许别名泄漏到 Engine 契约。"""

    if isinstance(value, str) and value.startswith("@"):
        try:
            return aliases[value]
        except KeyError as exc:
            raise ValueError(f"场景引用了未知别名: {value}") from exc
    if isinstance(value, dict):
        return {key: _resolve_aliases(item, aliases) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_aliases(item, aliases) for item in value]
    return value


def _apply_initial_state(state: GameState, patch: Mapping[str, object]) -> GameState:
    """只允许场景调整已声明的安全起点字段。"""

    unknown = set(patch) - {"scene_id", "discovered_facts"}
    if unknown:
        raise ValueError(f"不支持的 initial_state.state 字段: {sorted(unknown)!r}")
    updates: dict[str, object] = {}
    if "scene_id" in patch:
        updates["scene_id"] = patch["scene_id"]
    if "discovered_facts" in patch:
        discovered = patch["discovered_facts"]
        if not isinstance(discovered, list) or not all(
            isinstance(item, str) for item in discovered
        ):
            raise ValueError("initial_state.state.discovered_facts 必须是字符串列表")
        updates["discovered_facts"] = tuple(discovered)
    return state.model_copy(update=updates, deep=True)


def _flatten_state(state: GameState) -> dict[str, object]:
    """把关键权威状态压平为稳定键，供 JSON 场景直接断言。"""

    flattened: dict[str, object] = {
        "scene_id": state.scene_id,
        "event_sequence": state.event_sequence,
        "discovered_facts": sorted(state.discovered_facts),
    }
    for entity_id, values in sorted(state.entities.items()):
        for key, value in sorted(values.items()):
            flattened[f"entity.{entity_id}.{key}"] = value
    for location_id, values in sorted(state.runtime_locations.items()):
        flattened[f"runtime_location.{location_id}.name"] = values.get("name")
    for entity_id, values in sorted(state.runtime_entities.items()):
        flattened[f"runtime_entity.{entity_id}.name"] = values.get("name")
        flattened[f"runtime_entity.{entity_id}.location_id"] = values.get("location_id")
    for item_id, item in sorted(state.item_instances.items()):
        flattened[f"item.{item_id}.custody"] = item.custody.kind
        flattened[f"item.{item_id}.holder"] = item.custody.ref_id
    return flattened
