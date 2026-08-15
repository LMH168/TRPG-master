"""把基线场景接入真实 ActionPlan、Engine 与 Narrator 内存执行链。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from collaboration_framework.contracts import (
    ActionAdjudication,
    CheckDecisionRequest,
    ModuleContentV3,
    PostRollDecisionRequest,
    ProposalRef,
    SelectCheckChoice,
)
from collaboration_framework.engine import (
    AdjudicationEngineService,
    DiceRoller,
    InMemoryEngineStore,
    RuleEngineService,
    SequenceDiceSource,
    current_turn_id,
    derive_runtime_object_id,
)
from collaboration_framework.engine.initialization import create_initial_game_state
from collaboration_framework.engine.models import ActorResources, ActorState, GameState
from collaboration_framework.host.application import ActionPlanNarrator
from collaboration_framework.host.schemas import ActionPlanNarrationContext, HostAgentContext

from app.core.action_plan_turn import (
    _proposal_from_adjudication,
    build_action_plan_turn_application,
)
from app.core.config import Settings
from app.core.turn_coordinator import (
    TurnCoordinator,
    TurnExecutionOutcome,
    TurnPhaseObserver,
)
from app.core.turn_events import TurnPhase
from app.core.turn_runtime import (
    InMemoryTurnStore,
    TurnCommitReceipt,
    TurnInputSnapshot,
    TurnWaitingReason,
)
from app.service.turn_outbox import TurnOutboxDispatcher
from app.service.ws_manager import ConnectionManager

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
        self.runtime_actual_ids: Mapping[str, str] = {}

    async def generate(self, context: HostAgentContext):  # noqa: ANN201
        raw = self.outputs.get(context.player_input.client_action_id) or {}
        resolved = _resolve_aliases(raw, self.aliases)
        declared_ids = {
            effect.get("entity_id") or effect.get("location_id")
            for effect in resolved.get("success_effects", [])
            if isinstance(effect, dict)
            and effect.get("type") in {"ensure_runtime_entity", "ensure_runtime_location"}
        }
        if not declared_ids:
            resolved = _replace_runtime_ids(resolved, self.runtime_actual_ids)
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
        adjudication = ActionAdjudication.model_validate(payload)
        return _proposal_from_adjudication(adjudication)


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
        self._runtime_id_normalization: dict[str, str] = {}
        self._turn_store = InMemoryTurnStore()
        self._coordinator = TurnCoordinator(
            self._turn_store,
            worker_id="baseline-turn-worker",
        )
        self._connection_manager = ConnectionManager()
        self._socket = _BaselineSocket()
        self._connection_manager.add(
            self.room_id,
            self._socket,
            self.player_id,
        )
        self._outbox = TurnOutboxDispatcher(
            self._turn_store,
            self._connection_manager,
            worker_id="baseline-outbox-worker",
            retry_seconds=0,
        )

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
        self._runtime_id_normalization = _runtime_id_map(
            scenario,
            room_id=self.room_id,
            aliases=aliases,
        )
        self._planner.runtime_actual_ids = {
            expected: actual for actual, expected in self._runtime_id_normalization.items()
        }
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
        application = self._application
        engine_store = self._store
        before_events = len(engine_store.inspect_domain_events(self.room_id))
        phases: list[str] = []

        async def observe_phase(phase: str) -> None:
            phases.append(phase)

        captured = None

        async def execute(observer: TurnPhaseObserver) -> TurnExecutionOutcome:
            nonlocal captured

            async def observe(phase: TurnPhase) -> None:
                await observer(phase)
                await observe_phase(phase)

            try:
                captured = await application.start(
                    room_id=self.room_id,
                    player_id=self.player_id,
                    client_action_id=turn.client_action_id,
                    utterance=turn.utterance,
                    on_phase=observe,
                )
                captured = await self._settle_check(captured, turn, observe)
            finally:
                # 内存 Engine 没有后端 receipt 表；基线侧根据已提交事件或进入叙事
                # 阶段写入等价证明，使 Coordinator 的恢复判断与 SQL 生产路径一致。
                committed_events = engine_store.inspect_domain_events(self.room_id)[before_events:]
                if committed_events or "generating_narration" in phases:
                    await self._ensure_baseline_receipt(turn.client_action_id)
            return _turn_outcome(captured)

        coordinated = await self._coordinator.start(
            TurnInputSnapshot(
                room_id=self.room_id,
                player_id=self.player_id,
                actor_id=self.actor_id,
                client_action_id=turn.client_action_id,
                utterance=turn.utterance,
            ),
            executor=execute,
            after_publish=lambda: application.mark_narration_persisted(
                room_id=self.room_id,
                parent_action_id=turn.client_action_id,
            ),
        )
        if coordinated.last_error is not None:
            # 故障适配器会根据 FaultController 决定是否显式重试；基线执行器不能
            # 把一个已持久化但尚未完成的回合误报成成功终态。
            raise RuntimeError(coordinated.last_error.public_message)
        await self._outbox.dispatch_due()
        events = engine_store.inspect_domain_events(self.room_id)
        new_events = events[before_events:]
        state = engine_store.inspect_state(self.room_id)
        execution = captured.execution if captured is not None else None
        check_run = execution.check_run if execution is not None else None
        narration = captured.narration if captured is not None else None
        if narration is None and coordinated.result is not None:
            narration_evidence = tuple(coordinated.result.narration.get("claimedFactIds", ()))
        else:
            narration_evidence = narration.claimed_evidence_refs if narration is not None else ()
        return BaselineTurnResult(
            client_action_id=turn.client_action_id,
            status=coordinated.status.value,
            phases=tuple(phases),
            event_types=tuple(event.type for event in new_events),
            state=_flatten_state(state, runtime_id_normalization=self._runtime_id_normalization),
            narration_evidence=narration_evidence,
            narration_claims=narration_evidence,
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

    async def _ensure_baseline_receipt(self, action_id: str) -> None:
        """为当前内存 Turn 幂等补一条提交证明，不伪造额外 DomainEvent。"""

        turn_id = current_turn_id()
        if turn_id is None or await self._turn_store.list_receipts(turn_id):
            return
        if self._store is None:
            raise RuntimeError("adapter 尚未 prepare")
        state = self._store.inspect_state(self.room_id)
        events = tuple(
            event
            for event in self._store.inspect_domain_events(self.room_id)
            if event.client_action_id.startswith(action_id)
        )
        sequences = tuple(event.sequence for event in events)
        await self._turn_store.append_receipt(
            TurnCommitReceipt(
                turn_id=turn_id,
                room_id=self.room_id,
                engine_request_id=f"baseline:{action_id}",
                action_request_id=action_id,
                committed_state_version=state.event_sequence,
                first_event_sequence=min(sequences) if sequences else None,
                last_event_sequence=max(sequences) if sequences else None,
                created_at=datetime.now(UTC),
            )
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


class _BaselineSocket:
    """记录 Outbox 实际投递帧，避免基线依赖真实网络。"""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.frames.append(message)


def _turn_outcome(result) -> TurnExecutionOutcome:  # noqa: ANN001
    """把 ActionPlan 结果压缩成玩家安全的可靠回合输出。"""

    if result is None:
        raise RuntimeError("基线执行没有产生 ActionPlan 结果")
    waiting_reason = TurnWaitingReason.NONE
    if result.waiting_for_player:
        execution = result.execution
        if execution is None:
            raise RuntimeError("等待玩家的基线回合缺少裁决结果")
        waiting_reason = (
            TurnWaitingReason.SKILL_CHOICE
            if execution.status == "awaiting_skill_choice"
            else TurnWaitingReason.POST_ROLL_DECISION
        )
    narration = None
    if result.narration is not None:
        narration = {
            "kind": result.narration.kind,
            "text": result.narration.text,
            "claimedFactIds": list(result.narration.claimed_evidence_refs),
            "suggestedActions": list(result.narration.suggested_actions),
        }
    return TurnExecutionOutcome(
        status=result.status,
        player_view=result.player_view.to_json_dict(),
        view_revision=result.player_view.revision,
        scene_id=result.player_view.scene_id,
        narration=narration,
        waiting_reason=waiting_reason,
    )


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


def _flatten_state(
    state: GameState,
    *,
    runtime_id_normalization: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """把关键权威状态压平为稳定键，供 JSON 场景直接断言。"""

    normalization = runtime_id_normalization or {}

    def stable(object_id: str | None) -> str | None:
        return normalization.get(object_id, object_id) if object_id is not None else None

    flattened: dict[str, object] = {
        "scene_id": stable(state.scene_id),
        "event_sequence": state.event_sequence,
        "discovered_facts": sorted(state.discovered_facts),
    }
    for entity_id, values in sorted(state.entities.items()):
        for key, value in sorted(values.items()):
            flattened[f"entity.{entity_id}.{key}"] = value
    for location_id, values in sorted(state.runtime_locations.items()):
        flattened[f"runtime_location.{stable(location_id)}.name"] = values.get("name")
    for entity_id, values in sorted(state.runtime_entities.items()):
        stable_entity_id = stable(entity_id)
        flattened[f"runtime_entity.{stable_entity_id}.name"] = values.get("name")
        flattened[f"runtime_entity.{stable_entity_id}.location_id"] = stable(
            values.get("location_id")
        )
    for item_id, item in sorted(state.item_instances.items()):
        stable_item_id = stable(item_id)
        flattened[f"item.{stable_item_id}.custody"] = item.custody.kind
        flattened[f"item.{stable_item_id}.holder"] = stable(item.custody.ref_id)
    return flattened


def _replace_runtime_ids(value: Any, replacements: Mapping[str, str]) -> Any:
    """后续回合把场景逻辑 ID 解析为 Engine 已派生的真实 ID。"""

    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, dict):
        return {key: _replace_runtime_ids(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_runtime_ids(item, replacements) for item in value]
    return value


def _runtime_id_map(
    scenario: BaselineScenario,
    *,
    room_id: str,
    aliases: Mapping[str, str],
) -> dict[str, str]:
    """预计算动态逻辑别名与稳定 Engine ID 的映射，避免场景硬编码散列值。"""

    mapping: dict[str, str] = {}
    for turn in scenario.turns:
        output = turn.host_output or {}
        for effect in output.get("success_effects", []):
            if not isinstance(effect, dict):
                continue
            effect_type = effect.get("type")
            if effect_type == "ensure_runtime_entity":
                alias = effect.get("entity_id")
                kind = "runtime_entity"
            elif effect_type == "ensure_runtime_location":
                alias = effect.get("location_id")
                kind = "runtime_location"
            else:
                continue
            if not isinstance(alias, str):
                continue
            logical_id = aliases.get(alias, alias)
            actual_id = derive_runtime_object_id(
                room_id=room_id,
                request_id=turn.client_action_id,
                ref=ProposalRef(kind=kind, id=logical_id),  # ty: ignore[invalid-argument-type]
            )
            mapping[actual_id] = logical_id
    return mapping
