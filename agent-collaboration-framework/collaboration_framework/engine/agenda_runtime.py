"""执行持久 RuleAgenda，并把每个确定性步骤提交为可恢复的权威段。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import uuid4

from pydantic import JsonValue

from collaboration_framework.contracts import (
    AdjudicatedCheckStep,
    AdvanceWorldTimeEffect,
    AgendaContinuationCandidate,
    AgendaContinuationOptionView,
    AgendaContinuationProposal,
    AwaitPlayerInputStep,
    CheckDegree,
    CheckStep,
    CommittedResult,
    ContractError,
    CreateNpcActionOpportunityStep,
    EffectStep,
    FinishStep,
    InvokeRulesetActionStep,
    NarrationEvidence,
    PresentationStep,
)

from .adjudication import AdjudicationEngineService
from .dice import DiceRoller, coc7_success_level, outcome_name, passes_difficulty
from .models import AgendaStepExecution, DomainEvent, GameState, RuleAgenda
from .persistent_results import committed_results_from_events
from .ports import EngineStore, RevisionConflictError
from .rules_v3 import (
    agenda_item_for_event,
    agenda_status_for_walk,
    agenda_step_execution_id,
    entity_state,
    matching_event_rules,
    ordered_agenda_items,
    walk_rule_from,
)

AgendaExecutionKind = Literal[
    "passive_check",
    "adjudicated_check",
    "ruleset_action",
    "npc_opportunity",
    "presentation",
    "effect_segment",
]


class AgendaRetryScheduledError(RuntimeError):
    """Agenda 已保存下一次重试点，可靠 Turn 必须保留原提交链。"""

    code = "AGENDA_RETRY_SCHEDULED"
    retryable = True
    allows_committed_retry = True
    manages_own_retry_budget = True


class RuleCheckProfileRegistry:
    """Engine 内部被动检定注册表；模组不能调用未注册 profile。"""

    def __init__(
        self, profiles: dict[str, Literal["skill", "sanity"]] | None = None
    ) -> None:
        self._profiles = profiles or {
            "coc7.skill": "skill",
            "coc7.sanity": "sanity",
        }

    def require(self, profile_id: str) -> Literal["skill", "sanity"]:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise ContractError(f"未注册的 Rule check profile: {profile_id}") from exc


class RulesetActionRegistry:
    """Engine 内部确定性规则动作注册表，不接受任意函数或状态路径。"""

    def __init__(
        self,
        actions: dict[
            str, Literal["apply_condition", "advance_to_condition_expiry"]
        ]
        | None = None,
    ) -> None:
        self._actions = actions or {
            "coc7.apply_condition": "apply_condition",
            "coc7.advance_to_condition_expiry": "advance_to_condition_expiry",
        }

    def require(
        self, action_id: str
    ) -> Literal["apply_condition", "advance_to_condition_expiry"]:
        try:
            return self._actions[action_id]
        except KeyError as exc:
            raise ContractError(f"未注册的 Ruleset Action: {action_id}") from exc


class RuleAgendaExecutor:
    """按 lease claim Agenda，并在 Engine 事务中推进一个稳定提交段。"""

    def __init__(
        self,
        store: EngineStore,
        *,
        engine: AdjudicationEngineService,
        dice: DiceRoller | None = None,
        check_profiles: RuleCheckProfileRegistry | None = None,
        ruleset_actions: RulesetActionRegistry | None = None,
        lease_seconds: int = 30,
    ) -> None:
        self._store = store
        self._engine = engine
        self._dice = dice or DiceRoller()
        self._check_profiles = check_profiles or RuleCheckProfileRegistry()
        self._ruleset_actions = ruleset_actions or RulesetActionRegistry()
        self._lease_seconds = lease_seconds

    async def continuation_candidates(
        self,
        *,
        room_id: str,
        player_id: str,
        actor_id: str,
    ) -> tuple[AgendaContinuationCandidate, ...]:
        """仅发布当前玩家拥有的安全等待候选，不暴露下一 Rule 游标。"""

        async with self._store.transaction(room_id) as tx:
            runtime = await tx.load_runtime()
            candidates: list[AgendaContinuationCandidate] = []
            for agenda in sorted(
                runtime.game_state.rule_agendas.values(),
                key=lambda item: item.agenda_id,
            ):
                if (
                    agenda.schema_version != 2
                    or agenda.status != "awaiting_player_input"
                    or agenda.player_id != player_id
                    or agenda.actor_id != actor_id
                    or agenda.current_rule_id is None
                    or agenda.current_step_id is None
                ):
                    continue
                rule = next(
                    (
                        item
                        for item in runtime.v3.rules
                        if item.id == agenda.current_rule_id
                    ),
                    None,
                )
                step = (
                    next(
                        (
                            item
                            for item in rule.execution.steps
                            if item.id == agenda.current_step_id
                        ),
                        None,
                    )
                    if rule is not None
                    else None
                )
                if (
                    not isinstance(step, AwaitPlayerInputStep)
                    or step.schema_version != 2
                ):
                    continue
                candidates.append(
                    AgendaContinuationCandidate(
                        agenda_id=agenda.agenda_id,
                        boundary_id=step.boundary_id or step.id,
                        player_safe_prompt=step.player_safe_prompt or "请选择如何继续",
                        options=tuple(
                            AgendaContinuationOptionView(
                                option_id=option.id,
                                semantic_hints=option.semantic_hints,
                            )
                            for option in step.options
                        ),
                    )
                )
            return tuple(candidates)

    async def resume_continuation(
        self,
        proposal: AgendaContinuationProposal,
        *,
        room_id: str,
        player_id: str,
        actor_id: str,
        turn_id: str,
        source_revision: str,
    ) -> RuleAgenda:
        """在 Engine 侧校验 owner、revision、boundary 和有限 option 后恢复。"""

        async with self._store.transaction(room_id) as tx:
            runtime = await tx.load_runtime()
            if runtime.revision != source_revision:
                raise ContractError("Agenda continuation 使用了过期 PlayerView")
            agenda = runtime.game_state.rule_agendas.get(proposal.agenda_id)
            if (
                agenda is None
                or agenda.schema_version != 2
                or agenda.status != "awaiting_player_input"
                or agenda.player_id != player_id
                or agenda.actor_id != actor_id
                or agenda.active_turn_id is not None
            ):
                raise ContractError("Agenda continuation 不属于当前玩家或已被接管")
            if agenda.pending_boundary_id != proposal.boundary_id:
                raise ContractError("Agenda continuation boundary 已变化")
            rule = next(
                (
                    item
                    for item in runtime.v3.rules
                    if item.id == agenda.current_rule_id
                ),
                None,
            )
            step = (
                next(
                    (
                        item
                        for item in rule.execution.steps
                        if item.id == agenda.current_step_id
                    ),
                    None,
                )
                if rule is not None
                else None
            )
            if not isinstance(step, AwaitPlayerInputStep) or step.schema_version != 2:
                raise ContractError("Agenda continuation 没有可恢复的 v2 等待步骤")
            option = next(
                (item for item in step.options if item.id == proposal.option_id), None
            )
            if option is None:
                raise ContractError("Agenda continuation option 不在服务端候选中")
            resumed = agenda.model_copy(
                update={
                    "active_turn_id": turn_id,
                    "status": "running",
                    "current_step_id": option.next_step_id,
                    "pending_boundary_id": None,
                    "pending_rule_input_id": proposal.option_id,
                    "revision": runtime.revision,
                },
                deep=True,
            )
        return await self._store.resume_rule_agenda_input(
            agenda=resumed,
            expected_lease_version=agenda.lease_version,
        )

    async def drain(
        self,
        *,
        room_id: str,
        turn_id: str,
        worker_id: str | None = None,
        max_segments: int = 64,
    ) -> tuple[AgendaStepExecution, ...]:
        """推进当前 Turn 可自动执行的 Agenda，直到稳定边界或玩家输入。"""

        owner = worker_id or f"agenda-{uuid4().hex}"
        committed: list[AgendaStepExecution] = []
        for _ in range(max_segments):
            now = datetime.now(UTC)
            agenda = await self._store.claim_rule_agenda(
                room_id=room_id,
                worker_id=owner,
                now=now,
                lease_expires_at=now + timedelta(seconds=self._lease_seconds),
            )
            if agenda is None:
                break
            if agenda.active_turn_id != turn_id:
                # 当前 PR 只允许负责该 Agenda 的 Turn 推进 gameplay；Supervisor
                # 必须先通过 TurnCoordinator 恢复并重新绑定，不能借后台身份越权。
                await self._fail_claimed_agenda(
                    agenda,
                    worker_id=owner,
                    code="agenda_turn_mismatch",
                )
                continue
            try:
                committed.append(await self._execute_claimed(agenda, turn_id=turn_id))
            except RevisionConflictError as exc:
                recovered = await self._find_committed_execution(agenda)
                if recovered is not None:
                    committed.append(recovered)
                    continue
                retrying = await self._record_execution_failure(
                    agenda,
                    worker_id=owner,
                    code=type(exc).__name__,
                    retryable=True,
                )
                if retrying:
                    raise AgendaRetryScheduledError() from exc
                break
            except ContractError as exc:
                # 固定模组、参数或权限错误重试不会改变结果，立即转为可审计失败。
                await self._record_execution_failure(
                    agenda,
                    worker_id=owner,
                    code=type(exc).__name__,
                    retryable=False,
                )
                break
            except Exception as exc:
                # 数据库已经提交、调用方却在 after-commit 边界收到异常时，必须按
                # execution 证明前移，不能拿旧 lease 再写失败状态或重新掷骰。
                recovered = await self._find_committed_execution(agenda)
                if recovered is not None:
                    committed.append(recovered)
                    continue
                retrying = await self._record_execution_failure(
                    agenda,
                    worker_id=owner,
                    code=type(exc).__name__,
                    retryable=True,
                )
                if retrying:
                    raise AgendaRetryScheduledError() from exc
                break
        else:
            raise ContractError("RuleAgenda 单次 drain 超出确定性段预算")
        return tuple(committed)

    async def executions_for_turn(
        self,
        *,
        room_id: str,
        turn_id: str,
    ) -> tuple[AgendaStepExecution, ...]:
        """读取已提交证明，确保叙事恢复不会丢失先前 drain 的结果。"""

        return await self._store.list_agenda_step_executions_for_turn(
            room_id=room_id,
            turn_id=turn_id,
        )

    async def boundary_for_turn(
        self,
        *,
        room_id: str,
        turn_id: str,
    ) -> Literal["awaiting_player_input", "failed"] | None:
        """从持久状态读取当前 Turn 的阻塞边界，恢复时不依赖瞬时返回值。"""

        async with self._store.transaction(room_id) as tx:
            runtime = await tx.load_runtime()
            statuses = {
                agenda.status
                for agenda in runtime.game_state.rule_agendas.values()
                if agenda.schema_version == 2
                and (
                    agenda.active_turn_id == turn_id or agenda.origin_turn_id == turn_id
                )
            }
        if "failed" in statuses:
            return "failed"
        if "awaiting_player_input" in statuses:
            return "awaiting_player_input"
        return None

    async def _find_committed_execution(
        self,
        agenda: RuleAgenda,
    ) -> AgendaStepExecution | None:
        """按 claim 时冻结的 cursor 查询提交证明，用于收束模糊提交边界。"""

        if agenda.current_rule_id is None or agenda.current_step_id is None:
            return None
        execution_id = agenda_step_execution_id(
            schema_version=agenda.schema_version,
            module_id=agenda.module_id,
            module_version=agenda.module_version,
            agenda_id=agenda.agenda_id,
            source_event_id=agenda.current_source_event_id or agenda.root_source.id,
            rule_id=agenda.current_rule_id,
            branch_id=agenda.current_branch_id or "default",
            step_id=agenda.current_step_id,
        )
        return await self._store.find_agenda_step_execution(
            room_id=agenda.room_id,
            execution_id=execution_id,
        )

    async def _record_execution_failure(
        self,
        agenda: RuleAgenda,
        *,
        worker_id: str,
        code: str,
        retryable: bool,
    ) -> bool:
        """记录有限重试；五次后进入 failed 并释放 lease。"""

        attempt = agenda.attempt_count + 1
        retrying = retryable and attempt < 5
        now = datetime.now(UTC)
        updated = agenda.model_copy(
            update={
                "status": agenda.status if retrying else "failed",
                "attempt_count": attempt,
                "next_attempt_at": (
                    now + timedelta(seconds=min(60, 2**attempt)) if retrying else None
                ),
                "failure_code": f"agenda_execution_{code}",
            },
            deep=True,
        )
        await self._store.checkpoint_rule_agenda(
            agenda=updated,
            worker_id=worker_id,
            expected_lease_version=agenda.lease_version,
            now=now,
        )
        return retrying

    async def _fail_claimed_agenda(
        self,
        agenda: RuleAgenda,
        *,
        worker_id: str,
        code: str,
    ) -> None:
        """只更新协调失败状态，不通过 checkpoint 偷写 gameplay Effect。"""

        failed = agenda.model_copy(
            update={"status": "failed", "failure_code": code}, deep=True
        )
        await self._store.checkpoint_rule_agenda(
            agenda=failed,
            worker_id=worker_id,
            expected_lease_version=agenda.lease_version,
            now=datetime.now(UTC),
        )

    async def _execute_claimed(
        self,
        claimed: RuleAgenda,
        *,
        turn_id: str,
    ) -> AgendaStepExecution:
        """重新加载锁内状态，执行一次，并越过唯一的原子提交点。"""

        async with self._store.transaction(claimed.room_id, turn_id=turn_id) as tx:
            runtime = await tx.load_runtime()
            agenda = runtime.game_state.rule_agendas.get(claimed.agenda_id)
            if agenda is None or agenda.lease_version != claimed.lease_version:
                raise ContractError("RuleAgenda lease 在提交前已经变化")
            if agenda.schema_version != 2 or agenda.origin_turn_id is None:
                raise ContractError("旧 RuleAgenda 不允许自动执行")
            rule = next(
                (
                    item
                    for item in runtime.v3.rules
                    if item.id == agenda.current_rule_id
                ),
                None,
            )
            if rule is None or agenda.current_step_id is None:
                raise ContractError("RuleAgenda 缺少固定 Rule cursor")
            step = next(
                (
                    item
                    for item in rule.execution.steps
                    if item.id == agenda.current_step_id
                ),
                None,
            )
            if step is None:
                raise ContractError("RuleAgenda 当前步骤不存在于固定 ModuleVersion")

            source_event_id = agenda.current_source_event_id or agenda.root_source.id
            branch_id = agenda.current_branch_id or "default"

            execution_id = agenda_step_execution_id(
                schema_version=agenda.schema_version,
                module_id=agenda.module_id,
                module_version=agenda.module_version,
                agenda_id=agenda.agenda_id,
                source_event_id=source_event_id,
                rule_id=rule.id,
                branch_id=branch_id,
                step_id=step.id,
            )
            state = runtime.game_state.model_copy(deep=True)
            events: list[DomainEvent] = []
            request: dict[str, JsonValue] = {"step_kind": step.kind}
            result: dict[str, JsonValue] = {}
            kind: AgendaExecutionKind

            if isinstance(step, CheckStep):
                if step.check.initiation_kind != "passive_rule":
                    raise ContractError("主动 Rule 检定必须由玩家 Turn 决策恢复")
                kind = "passive_check"
                state, check_events, degree, check_result = self._run_passive_check(
                    state,
                    agenda=agenda,
                    step=step,
                    execution_id=execution_id,
                )
                events.extend(check_events)
                request.update(
                    {
                        "profile_id": step.check.profile_id,
                        "parameters": deepcopy(step.check.parameters),
                    }
                )
                result.update(check_result)
                next_step_id = step.result_routes[cast(CheckDegree, degree)]
            elif isinstance(step, AdjudicatedCheckStep):
                kind = "adjudicated_check"
                if agenda.pending_check_id is None:
                    raise ContractError("AdjudicatedCheckStep 缺少已提交检定引用")
                check_run = await tx.load_check_run(agenda.pending_check_id)
                if check_run is None or check_run.status != "resolved":
                    raise ContractError("AdjudicatedCheckStep 的检定尚未形成权威结果")
                degree = (check_run.final_result or check_run.roll).degree
                result.update({"check_id": check_run.check_id, "degree": degree})
                next_step_id = step.result_routes[degree]
            elif isinstance(step, InvokeRulesetActionStep):
                kind = "ruleset_action"
                state, action_events, action_result = self._invoke_ruleset_action(
                    runtime,
                    state,
                    agenda=agenda,
                    step=step,
                    execution_id=execution_id,
                )
                events.extend(action_events)
                request.update(
                    {
                        "action_id": step.action_id,
                        "parameters": deepcopy(step.parameters),
                    }
                )
                result.update(action_result)
                next_step_id = step.next_step_id
            elif isinstance(step, CreateNpcActionOpportunityStep):
                kind = "npc_opportunity"
                state, npc_events, npc_result = self._create_npc_opportunity(
                    state,
                    agenda=agenda,
                    step=step,
                    execution_id=execution_id,
                )
                events.extend(npc_events)
                result.update(npc_result)
                next_step_id = step.next_step_id
            elif isinstance(step, PresentationStep):
                kind = "presentation"
                summary = (
                    rule.presentation.player_safe_summary
                    if rule.presentation is not None
                    and rule.presentation.id == step.presentation_id
                    else ""
                )
                if not summary:
                    raise ContractError("PresentationStep 未引用本 Rule 的安全展示证据")
                events.append(
                    self._event(
                        state,
                        agenda=agenda,
                        execution_id=execution_id,
                        offset=1,
                        event_type="rule.presentation",
                        payload={
                            "presentation_id": step.presentation_id,
                            "player_safe_summary": summary,
                        },
                    )
                )
                result["presentation_id"] = step.presentation_id
                next_step_id = step.next_step_id
            elif isinstance(step, EffectStep):
                kind = "effect_segment"
                next_step_id = step.id
            elif isinstance(step, FinishStep):
                kind = "effect_segment"
                events.append(
                    self._event(
                        state,
                        agenda=agenda,
                        execution_id=execution_id,
                        offset=1,
                        event_type="rule.agenda_completed",
                        payload={"rule_id": rule.id},
                        visibility="hidden",
                    )
                )
                next_step_id = step.id
            else:
                raise ContractError(f"当前 Agenda 步骤不能自动执行: {step.kind}")

            state, events, agenda, reached = self._advance_effects(
                runtime,
                state=state,
                events=events,
                agenda=agenda,
                rule=rule,
                start_step_id=next_step_id,
                execution_id=execution_id,
            )
            agenda = self._enqueue_new_event_rules(
                runtime.v3,
                state=state,
                agenda=agenda,
                events=tuple(events),
            )
            agenda = self._finalize_cursor(runtime.v3, agenda=agenda, reached=reached)
            # 每个自动段释放 lease；若仍有工作，下次 claim 使用新的 lease_version。
            agenda = agenda.model_copy(
                update={
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "lease_version": agenda.lease_version + 1,
                    "attempt_count": 0,
                    "next_attempt_at": None,
                    "revision": str(runtime.game_state.event_sequence + len(events)),
                },
                deep=True,
            )
            agendas = dict(state.rule_agendas)
            agendas[agenda.agenda_id] = agenda
            state = state.model_copy(
                update={
                    "rule_agendas": agendas,
                    "event_sequence": runtime.game_state.event_sequence + len(events),
                },
                deep=True,
            )
            if (
                agenda.origin_turn_id is None
            ):  # pragma: no cover - v2 已在事务入口校验。
                raise ContractError("RuleAgenda 缺少 origin_turn_id")
            execution = AgendaStepExecution(
                execution_id=execution_id,
                room_id=agenda.room_id,
                origin_turn_id=agenda.origin_turn_id,
                execution_turn_id=turn_id,
                agenda_id=agenda.agenda_id,
                source_event_id=source_event_id,
                rule_id=rule.id,
                branch_id=branch_id,
                step_id=step.id,
                execution_kind=kind,
                request=request,
                result={
                    **result,
                    "agenda_status": agenda.status,
                    "public_event_refs": [
                        event.event_id
                        for event in events
                        if event.visibility == "public"
                    ],
                    "committed_results": [
                        item.to_json_dict()
                        for item in self._committed_results_for_narration(
                            tuple(events), actor_id=agenda.actor_id or ""
                        )
                    ],
                    "narration_evidence": [
                        item.to_json_dict()
                        for item in self._narration_evidence(tuple(events))
                    ],
                },
                committed_state_version=state.event_sequence,
                created_at=datetime.now(UTC),
            )
            await tx.commit_agenda_segment(
                expected_revision=runtime.revision,
                new_state=state,
                events=tuple(events),
                agenda=agenda,
                execution=execution,
            )
            return execution

    @staticmethod
    def _committed_results_for_narration(
        events: tuple[DomainEvent, ...],
        *,
        actor_id: str,
    ) -> tuple[CommittedResult, ...]:
        """把 Agenda 的公开权威事件转换成 Narrator 可消费的安全结果。"""

        results = list(committed_results_from_events(events))
        for event in events:
            if event.visibility != "public" or event.type != "actor.condition_applied":
                continue
            condition = event.payload.get("condition")
            if not isinstance(condition, str):
                continue
            # Ruleset registry 定义了 condition 的权威语义；这里规范化为现有
            # 持久状态词汇，避免 Narrator 只能看到内部 condition 标识。
            if condition in {"unconscious", "unconscious_until_night"}:
                state_key = "consciousness"
                state_value = "unconscious"
            else:
                state_key = "condition"
                state_value = condition
            results.append(
                CommittedResult(
                    kind="character_state",
                    target_id=actor_id,
                    state_key=state_key,
                    state_value=state_value,
                    event_ref=event.event_id,
                )
            )
        return tuple(results)

    @staticmethod
    def _narration_evidence(
        events: tuple[DomainEvent, ...],
    ) -> tuple[NarrationEvidence, ...]:
        """只把模组显式发布的安全 Presentation 交给 Narrator。"""

        evidence: list[NarrationEvidence] = []
        for event in events:
            if event.visibility != "public" or event.type != "rule.presentation":
                continue
            presentation_id = event.payload.get("presentation_id")
            summary = event.payload.get("player_safe_summary")
            if not isinstance(presentation_id, str) or not isinstance(summary, str):
                continue
            evidence.append(
                NarrationEvidence(
                    ref=event.event_id,
                    kind="rule_presentation",
                    subject_id=presentation_id,
                    subject_name=summary,
                    description=summary,
                    required_in_narration=True,
                )
            )
        return tuple(evidence)

    def _run_passive_check(
        self,
        state: GameState,
        *,
        agenda: RuleAgenda,
        step: CheckStep,
        execution_id: str,
    ) -> tuple[GameState, tuple[DomainEvent, ...], str, dict[str, JsonValue]]:
        """执行已注册的 COC7 被动检定；不提供 Luck、Push 或玩家后置选择。"""

        actor = state.actors[agenda.actor_id or ""]
        profile = step.check.profile_id
        profile_kind = self._check_profiles.require(profile)
        if profile_kind == "skill":
            skill_id = step.check.parameters.get("skill_id")
            if not isinstance(skill_id, str):
                raise ContractError("coc7.skill 缺少可信 skill_id")
            skills = actor.state.get("skills")
            attributes = actor.state.get("attributes")
            target = (
                skills.get(skill_id)
                if isinstance(skills, dict) and skill_id in skills
                else attributes.get(skill_id)
                if isinstance(attributes, dict)
                else None
            )
        else:
            target = actor.resources.san
        if (
            not isinstance(target, int)
            or isinstance(target, bool)
            or not 0 <= target <= 100
        ):
            raise ContractError("被动检定目标值不属于当前 Actor")

        roll = self._dice.percentile()
        level = coc7_success_level(target, roll)
        difficulty = step.check.difficulty or "regular"
        passed = passes_difficulty(level, difficulty)
        degree = outcome_name(level, passed=passed)
        events = [
            self._event(
                state,
                agenda=agenda,
                execution_id=execution_id,
                offset=1,
                event_type="rule.check_resolved",
                payload={
                    "profile_id": profile,
                    "roll": roll,
                    "target": target,
                    "degree": degree,
                    "passed": passed,
                },
            )
        ]
        result: dict[str, JsonValue] = {
            "roll": roll,
            "target": target,
            "degree": degree,
            "passed": passed,
        }
        if profile_kind == "sanity":
            expression = step.check.parameters.get(
                "success_loss" if passed else "failure_loss", "0"
            )
            if not isinstance(expression, str):
                raise ContractError("coc7.sanity 损失表达式必须是字符串")
            loss = max(0, self._dice.roll(expression))
            habit_cap = step.check.parameters.get("habit_cap")
            if isinstance(habit_cap, int) and not isinstance(habit_cap, bool):
                loss = min(loss, max(0, habit_cap))
            san = max(0, target - loss)
            resources = actor.resources.model_copy(update={"san": san}, deep=True)
            conditions = set(actor.conditions)
            if loss >= 5:
                conditions.add("temporary_insanity")
            actors = dict(state.actors)
            actors[agenda.actor_id or ""] = actor.model_copy(
                update={
                    "resources": resources,
                    "conditions": tuple(sorted(conditions)),
                },
                deep=True,
            )
            state = state.model_copy(update={"actors": actors}, deep=True)
            events.append(
                self._event(
                    state,
                    agenda=agenda,
                    execution_id=execution_id,
                    offset=len(events) + 1,
                    event_type="actor.sanity_changed",
                    payload={"from": target, "to": san, "loss": loss},
                )
            )
            if loss >= 5:
                events.append(
                    self._event(
                        state,
                        agenda=agenda,
                        execution_id=execution_id,
                        offset=len(events) + 1,
                        event_type="actor.temporary_insanity",
                        payload={"condition": "temporary_insanity"},
                    )
                )
            result.update({"san_loss": loss, "san_after": san})
        return state, tuple(events), degree, result

    def _invoke_ruleset_action(
        self,
        runtime,
        state: GameState,
        *,
        agenda: RuleAgenda,
        step: InvokeRulesetActionStep,
        execution_id: str,
    ) -> tuple[GameState, tuple[DomainEvent, ...], dict[str, JsonValue]]:
        """执行白名单 Ruleset Action；参数不能选择任意状态路径。"""

        action_kind = self._ruleset_actions.require(step.action_id)
        if step.actor_binding != "actor":
            raise ContractError("Ruleset Action actor binding 不受支持")
        condition = step.parameters.get("condition")
        if not isinstance(condition, str) or not condition.strip():
            raise ContractError(f"{step.action_id} 缺少 condition")
        if action_kind == "advance_to_condition_expiry":
            return self._advance_to_condition_expiry(
                runtime,
                state,
                agenda=agenda,
                condition=condition,
                execution_id=execution_id,
            )
        allowed = {"unconscious", "unconscious_until_night", "temporary_insanity"}
        if condition not in allowed:
            raise ContractError("coc7.apply_condition condition 未注册")
        actor_id = agenda.actor_id or ""
        actor = state.actors[actor_id]
        conditions = set(actor.conditions)
        conditions.add(condition)
        actor_state = deepcopy(actor.state)
        if condition == "unconscious_until_night":
            current = state.world_time.current
            target_day = (
                current.day_index if current.hour_of_day < 18 else current.day_index + 1
            )
            expirations = deepcopy(actor_state.get("condition_expirations", {}))
            if not isinstance(expirations, dict):
                expirations = {}
            expirations[condition] = target_day * 24 + 18
            actor_state["condition_expirations"] = expirations
        actors = dict(state.actors)
        actors[actor_id] = actor.model_copy(
            update={"conditions": tuple(sorted(conditions)), "state": actor_state},
            deep=True,
        )
        state = state.model_copy(update={"actors": actors}, deep=True)
        event = self._event(
            state,
            agenda=agenda,
            execution_id=execution_id,
            offset=1,
            event_type="actor.condition_applied",
            payload={"condition": condition},
        )
        return state, (event,), {"condition": condition}

    def _advance_to_condition_expiry(
        self,
        runtime,
        state: GameState,
        *,
        agenda: RuleAgenda,
        condition: str,
        execution_id: str,
    ) -> tuple[GameState, tuple[DomainEvent, ...], dict[str, JsonValue]]:
        """按权威到期元数据推进时间，禁止模组任意指定目标时刻。"""

        actor_id = agenda.actor_id or ""
        actor = state.actors.get(actor_id)
        if actor is None or condition not in actor.conditions:
            raise ContractError("待恢复的临时 condition 不存在")
        expirations = actor.state.get("condition_expirations")
        target_hour = expirations.get(condition) if isinstance(expirations, dict) else None
        if not isinstance(target_hour, int) or isinstance(target_hour, bool):
            raise ContractError("临时 condition 缺少权威到期时间")
        if target_hour <= state.world_time.current.absolute_hour:
            raise ContractError("临时 condition 到期时间没有晚于当前时间")
        target_hour_of_day = target_hour % 24
        target_points = tuple(
            point
            for point in runtime.v3.time_policy.default_points
            if point.hour_of_day == target_hour_of_day
        )
        if len(target_points) != 1:
            raise ContractError("临时 condition 到期时间无法映射到唯一时间点")
        # 复用 Engine 的时间 Effect，使逐点事件与 condition 清理保持同一事务。
        advanced, events = self._engine._apply_effect(
            runtime,
            state,
            AdvanceWorldTimeEffect(to_point_id=target_points[0].id),
            room_id=agenda.room_id,
            request_id=execution_id,
            actor_id=actor_id,
            offset=1,
        )
        if advanced.world_time.current.absolute_hour != target_hour:
            raise ContractError("时间推进未到达临时 condition 的权威到期点")
        if condition in advanced.actors[actor_id].conditions:
            raise ContractError("临时 condition 到期后未被清除")
        return advanced, events, {
            "condition": condition,
            "expired_at": target_hour,
            "current_point_id": advanced.world_time.current_point_id,
        }

    def _create_npc_opportunity(
        self,
        state: GameState,
        *,
        agenda: RuleAgenda,
        step: CreateNpcActionOpportunityStep,
        execution_id: str,
    ) -> tuple[GameState, tuple[DomainEvent, ...], dict[str, JsonValue]]:
        """只为仍在场且可行动的模组 NPC 发布一次确定性行动机会。"""

        npc = entity_state(state, step.entity_id)
        inactive = (
            npc.get("location_id") not in {None, state.scene_id}
            or npc.get("consciousness") in {"dead", "unconscious"}
            or npc.get("dead") is True
        )
        event_type = "npc.action_skipped" if inactive else "npc.action_opportunity"
        event = self._event(
            state,
            agenda=agenda,
            execution_id=execution_id,
            offset=1,
            event_type=event_type,
            payload={"entity_id": step.entity_id, "action_id": step.action_id},
            visibility="hidden" if inactive else "public",
        )
        return state, (event,), {"skipped": inactive, "action_id": step.action_id}

    def _advance_effects(
        self,
        runtime,
        *,
        state: GameState,
        events: list[DomainEvent],
        agenda: RuleAgenda,
        rule,
        start_step_id: str,
        execution_id: str,
    ) -> tuple[GameState, list[DomainEvent], RuleAgenda, object]:
        """提交当前步骤后的连续 Effect，停在下一个阻塞点或 finish。"""

        steps = {item.id: item for item in rule.execution.steps}
        cursor = start_step_id
        visited: set[str] = set()
        while True:
            if cursor in visited:
                raise ContractError("RuleAgenda 步骤图出现循环")
            visited.add(cursor)
            step = steps.get(cursor)
            if step is None:
                raise ContractError("RuleAgenda 后续步骤不存在")
            if isinstance(step, EffectStep):
                state, emitted = self._engine._apply_effect(
                    runtime,
                    state,
                    step.effect,
                    room_id=agenda.room_id,
                    request_id=execution_id,
                    actor_id=agenda.actor_id or "",
                    offset=len(events) + 1,
                )
                events.extend(emitted)
                cursor = step.next_step_id
                continue
            return state, events, agenda, step

    def _enqueue_new_event_rules(
        self,
        module,
        *,
        state: GameState,
        agenda: RuleAgenda,
        events: tuple[DomainEvent, ...],
    ) -> RuleAgenda:
        """把本段新 Event 匹配到同一 Agenda，避免链式规则永远不再扫描。"""

        queue = list(agenda.queue)
        known = {(item.source_event_id, item.rule_id) for item in queue}
        sources = list(agenda.source_event_ids)
        for event in events:
            if event.type in {"rule.triggered", "rule.check_resolved"}:
                continue
            matched = matching_event_rules(
                module,
                event_type=event.type,
                state=state,
                actor_id=agenda.actor_id or "",
            )
            for rule in matched:
                key = (event.event_id, rule.id)
                if key in known:
                    continue
                queue.append(agenda_item_for_event(rule, event))
                known.add(key)
                if event.event_id not in sources:
                    sources.append(event.event_id)
        return agenda.model_copy(
            update={
                "queue": ordered_agenda_items(tuple(queue)),
                "source_event_ids": tuple(sources),
            },
            deep=True,
        )

    def _finalize_cursor(self, module, *, agenda: RuleAgenda, reached) -> RuleAgenda:
        """根据到达的不可变步骤更新当前 Rule，并选择下一个队列项。"""

        queue = list(agenda.queue)
        current_index = next(
            (
                index
                for index, item in enumerate(queue)
                if item.rule_id == agenda.current_rule_id
                and item.source_event_id == agenda.current_source_event_id
                and item.status == "running"
            ),
            None,
        )
        if isinstance(reached, FinishStep):
            if current_index is not None:
                queue[current_index] = queue[current_index].model_copy(
                    update={"status": "completed"}
                )
            pending = next(
                (
                    item
                    for item in ordered_agenda_items(tuple(queue))
                    if item.status == "queued"
                ),
                None,
            )
            if pending is None:
                return agenda.model_copy(
                    update={
                        "queue": tuple(queue),
                        "status": "stable",
                        "current_rule_id": None,
                        "current_branch_id": None,
                        "current_step_id": None,
                        "pending_check_id": None,
                        "pending_boundary_id": None,
                        "active_turn_id": None,
                    },
                    deep=True,
                )
            pending_index = queue.index(pending)
            queue[pending_index] = pending.model_copy(update={"status": "running"})
            next_rule = next(
                item for item in module.rules if item.id == pending.rule_id
            )
            branch = next(
                item
                for item in next_rule.execution.branches
                if item.id == pending.branch_id
            )
            return agenda.model_copy(
                update={
                    "queue": tuple(queue),
                    "status": "running",
                    "current_source_event_id": pending.source_event_id,
                    "current_rule_id": pending.rule_id,
                    "current_branch_id": pending.branch_id,
                    "current_step_id": branch.entry_step_id,
                },
                deep=True,
            )
        if isinstance(reached, AwaitPlayerInputStep):
            return agenda.model_copy(
                update={
                    "queue": tuple(queue),
                    "status": "awaiting_player_input",
                    "current_step_id": reached.id,
                    "pending_boundary_id": reached.boundary_id or reached.id,
                    "active_turn_id": None,
                },
                deep=True,
            )
        walk = walk_rule_from(
            next(item for item in module.rules if item.id == agenda.current_rule_id),
            reached.id,
        )
        return agenda.model_copy(
            update={
                "queue": tuple(queue),
                "status": agenda_status_for_walk(
                    next(
                        item
                        for item in module.rules
                        if item.id == agenda.current_rule_id
                    ),
                    walk,
                ),
                "current_step_id": reached.id,
            },
            deep=True,
        )

    @staticmethod
    def _event(
        state: GameState,
        *,
        agenda: RuleAgenda,
        execution_id: str,
        offset: int,
        event_type: str,
        payload: dict[str, JsonValue],
        visibility: Literal["public", "private", "hidden"] = "public",
    ) -> DomainEvent:
        """为 Agenda 段生成连续事件；随机 ID 不参与幂等身份。"""

        return DomainEvent(
            event_id=f"evt_{uuid4().hex}",
            sequence=state.event_sequence + offset,
            type=event_type,
            room_id=agenda.room_id,
            actor_id=agenda.actor_id or "",
            client_action_id=execution_id,
            cause=f"agenda:{agenda.agenda_id}",
            visibility=visibility,
            payload=payload,
        )


__all__ = ["RuleAgendaExecutor", "RuleCheckProfileRegistry", "RulesetActionRegistry"]
