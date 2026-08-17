"""Issue #23 的 RuleAgenda 自动执行、原子证明和幂等恢复测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from collaboration_framework.contracts import (
    ActionAdjudication,
    ActionMethod,
    ActionTarget,
    AdvanceWorldTimeEffect,
    AgendaContinuationProposal,
    AgentMatchTriggerSpec,
    AwaitPlayerInputStep,
    ChangeEntityStateEffect,
    ContractError,
    EffectStep,
    ExecutionBranchSpec,
    FinishStep,
    ModuleContentV3,
    NoAdjudicationCheck,
    RuleExecutionSpec,
    RuleInputOption,
    SubmitAdjudicationRequest,
)
from collaboration_framework.engine import (
    ActorResources,
    ActorState,
    AdjudicationEngineService,
    AgendaItem,
    AgendaRetryScheduledError,
    AgendaSource,
    DiceRoller,
    DomainEvent,
    EngineRuntimeSnapshot,
    InMemoryEngineStore,
    RuleAgendaExecutor,
    SequenceDiceSource,
    create_initial_game_state,
    engine_turn_context,
)
from collaboration_framework.engine.rules_v3 import create_rule_agenda

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "module-parser"
    / "examples"
    / "module-content-validation"
    / "追书人"
    / "module-content-v3.json"
)


@pytest.mark.asyncio
async def test_passive_check_commits_once_and_recovery_does_not_reroll() -> None:
    """提交后重复 drain 只能读取稳定状态，不能消耗第二个骰点。"""

    module = ModuleContentV3.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    store = InMemoryEngineStore()
    state = create_initial_game_state(
        module,
        room_id="room-1",
        actors={
            "actor-1": ActorState(
                player_id="player-1",
                name="调查员",
                source_character_id="character-1",
                source_character_version=1,
                resources=ActorResources(san=60),
                state={"skills": {"spot_hidden": 50}},
            )
        },
    )
    store.register_room(module_content=module, initial_state=state)
    engine = AdjudicationEngineService(store)

    with engine_turn_context("turn-1"):
        await engine._submit_internal_adjudication(
            SubmitAdjudicationRequest(
                room_id="room-1",
                player_id="player-1",
                adjudication=ActionAdjudication(
                    request_id="see-ghoul",
                    source_revision="0",
                    actor_id="actor-1",
                    summary="看清墓地里的人影",
                    target=ActionTarget(kind="entity", id="cemetery_figure"),
                    method=ActionMethod(family="observe", description="仔细观察"),
                    check=NoAdjudicationCheck(),
                    success_effects=(
                        ChangeEntityStateEffect(
                            entity_id="cemetery_figure",
                            key="true_form_seen",
                            value=True,
                        ),
                    ),
                ),
            )
        )

    persisted = next(iter(store.inspect_state("room-1").rule_agendas.values()))
    assert persisted.schema_version == 2
    assert persisted.active_turn_id == "turn-1"
    executor = RuleAgendaExecutor(
        store,
        engine=engine,
        dice=DiceRoller(SequenceDiceSource([50])),
    )
    executions = await executor.drain(room_id="room-1", turn_id="turn-1")

    assert len(executions) == 1
    assert executions[0].execution_kind == "passive_check"
    assert executions[0].result["roll"] == 50
    final_agenda = next(iter(store.inspect_state("room-1").rule_agendas.values()))
    assert final_agenda.status == "stable"
    assert await executor.drain(room_id="room-1", turn_id="turn-1") == ()
    assert (
        len(
            await store.list_agenda_step_executions(
                room_id="room-1", agenda_id=final_agenda.agenda_id
            )
        )
        == 1
    )
    by_turn = await executor.executions_for_turn(room_id="room-1", turn_id="turn-1")
    assert tuple(item.execution_id for item in by_turn) == (executions[0].execution_id,)


def test_rule_presentation_becomes_required_player_safe_evidence() -> None:
    """Agenda 只能从显式 Presentation 生成叙事证据，不能解释普通事件载荷。"""

    evidence = RuleAgendaExecutor._narration_evidence(
        (
            DomainEvent(
                event_id="evt-presentation",
                sequence=1,
                type="rule.presentation",
                room_id="room-1",
                actor_id="actor-1",
                client_action_id="agenda-execution",
                cause="agenda:agenda-1",
                visibility="public",
                payload={
                    "presentation_id": "crypt-faint",
                    "player_safe_summary": "地穴恶臭令你失去意识。",
                },
            ),
            DomainEvent(
                event_id="evt-hidden",
                sequence=2,
                type="rule.presentation",
                room_id="room-1",
                actor_id="actor-1",
                client_action_id="agenda-execution",
                cause="agenda:agenda-1",
                visibility="hidden",
                payload={
                    "presentation_id": "keeper-only",
                    "player_safe_summary": "这段内容不能公开。",
                },
            ),
        )
    )

    assert len(evidence) == 1
    assert evidence[0].ref == "evt-presentation"
    assert evidence[0].required_in_narration is True


@pytest.mark.asyncio
async def test_player_input_boundary_only_accepts_published_option() -> None:
    """Host 只能选择安全候选，下一游标始终由 Engine 从模组恢复。"""

    original = ModuleContentV3.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    base_rule = original.rules[0]
    assert isinstance(base_rule.trigger, AgentMatchTriggerSpec)
    branch_id = base_rule.trigger.options[0].id
    rule = base_rule.model_copy(
        update={
            "execution": RuleExecutionSpec(
                branches=(ExecutionBranchSpec(id=branch_id, entry_step_id="wait"),),
                steps=(
                    AwaitPlayerInputStep(
                        id="wait",
                        schema_version=2,
                        boundary_id="door-choice",
                        player_safe_prompt="你要屏住呼吸还是直接进入？",
                        options=(
                            RuleInputOption(
                                id="hold_breath",
                                semantic_hints=("屏住呼吸",),
                                next_step_id="finish",
                            ),
                        ),
                    ),
                    FinishStep(id="finish"),
                ),
            )
        },
        deep=True,
    )
    module = original.model_copy(update={"rules": (rule,)}, deep=True)
    state = create_initial_game_state(
        module,
        room_id="room-1",
        actors={
            "actor-1": ActorState(
                player_id="player-1",
                name="调查员",
                source_character_id="character-1",
                source_character_version=1,
            )
        },
    )
    store = InMemoryEngineStore()
    engine = AdjudicationEngineService(store)
    runtime = EngineRuntimeSnapshot(
        module_id=module.module_id,
        module_version=module.version,
        module_content=module,
        game_state=state,
        revision="0",
    )
    adjudication = ActionAdjudication(
        request_id="action-1",
        source_revision="0",
        actor_id="actor-1",
        summary="选择进入方式",
        target=ActionTarget(kind="location", id=state.scene_id),
        method=ActionMethod(family="enter", description="选择进入方式"),
        check=NoAdjudicationCheck(),
        rule_decision={"rule_id": rule.id, "option_id": branch_id},
    )
    source_event = DomainEvent(
        event_id="event-1",
        sequence=1,
        type="action.succeeded",
        room_id="room-1",
        actor_id="actor-1",
        client_action_id="action-1",
        cause="action:action-1",
        visibility="public",
        payload={},
    )
    with engine_turn_context("turn-1"):
        state = engine._materialize_agent_rule_agenda(
            runtime,
            state=state,
            source_event=source_event,
            adjudication=adjudication,
            check_run=None,
        )
    agenda = next(iter(state.rule_agendas.values()))
    assert agenda.status == "awaiting_player_input"
    assert agenda.active_turn_id is None
    assert agenda.pending_boundary_id == "door-choice"
    store.register_room(module_content=module, initial_state=state)
    executor = RuleAgendaExecutor(store, engine=engine)

    candidates = await executor.continuation_candidates(
        room_id="room-1", player_id="player-1", actor_id="actor-1"
    )
    assert candidates[0].options[0].option_id == "hold_breath"
    assert (
        await executor.boundary_for_turn(room_id="room-1", turn_id="turn-1")
        == "awaiting_player_input"
    )
    with pytest.raises(ContractError, match="option"):
        await executor.resume_continuation(
            AgendaContinuationProposal(
                agenda_id=agenda.agenda_id,
                boundary_id="door-choice",
                option_id="invented",
            ),
            room_id="room-1",
            player_id="player-1",
            actor_id="actor-1",
            turn_id="turn-2",
            source_revision="0",
        )

    await executor.resume_continuation(
        AgendaContinuationProposal(
            agenda_id=agenda.agenda_id,
            boundary_id="door-choice",
            option_id="hold_breath",
        ),
        room_id="room-1",
        player_id="player-1",
        actor_id="actor-1",
        turn_id="turn-2",
        source_revision="0",
    )
    executions = await executor.drain(room_id="room-1", turn_id="turn-2")
    assert len(executions) == 1
    assert (
        store.inspect_state("room-1").rule_agendas[agenda.agenda_id].status == "stable"
    )


@pytest.mark.asyncio
async def test_agenda_step_budget_rejects_segment_without_gameplay_commit() -> None:
    """跨段累计预算越界必须失败，且当前段的 Effect、事件和 execution 全部不落库。"""

    original = ModuleContentV3.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    base_rule = original.rules[0]
    assert isinstance(base_rule.trigger, AgentMatchTriggerSpec)
    branch_id = base_rule.trigger.options[0].id
    rule = base_rule.model_copy(
        update={
            "execution": RuleExecutionSpec(
                branches=(ExecutionBranchSpec(id=branch_id, entry_step_id="mutate"),),
                steps=(
                    EffectStep(
                        id="mutate",
                        effect=ChangeEntityStateEffect(
                            entity_id="crypt_entrance",
                            key="budget_test",
                            value=True,
                        ),
                        next_step_id="finish",
                    ),
                    FinishStep(id="finish"),
                ),
            )
        },
        deep=True,
    )
    module = original.model_copy(update={"rules": (rule,)}, deep=True)
    state = create_initial_game_state(
        module,
        room_id="room-budget",
        actors={
            "actor-1": ActorState(
                player_id="player-1",
                name="调查员",
                source_character_id="character-1",
                source_character_version=1,
            )
        },
    )
    agenda = create_rule_agenda(
        agenda_id="agenda-budget",
        room_id=state.room_id,
        module=module,
        correlation_id="action-budget",
        root_source=AgendaSource(kind="event", id="event-budget"),
        revision="0",
        origin_turn_id="turn-budget",
        active_turn_id="turn-budget",
        player_id="player-1",
        actor_id="actor-1",
        current_source_event_id="event-budget",
    ).model_copy(
        update={
            "status": "running",
            "current_rule_id": rule.id,
            "current_branch_id": branch_id,
            "current_step_id": "mutate",
            "step_count": 1,
            "max_steps": 1,
            "chain_depth": 1,
            "queue": (
                AgendaItem(
                    source_event_id="event-budget",
                    event_sequence=1,
                    rule_id=rule.id,
                    rule_priority=rule.priority,
                    branch_id=branch_id,
                    status="running",
                ),
            ),
        },
        deep=True,
    )
    state = state.model_copy(
        update={"rule_agendas": {agenda.agenda_id: agenda}}, deep=True
    )
    store = InMemoryEngineStore()
    store.register_room(module_content=module, initial_state=state)
    executor = RuleAgendaExecutor(store, engine=AdjudicationEngineService(store))

    assert await executor.drain(room_id=state.room_id, turn_id="turn-budget") == ()
    final = store.inspect_state(state.room_id)
    assert "budget_test" not in final.entities["crypt_entrance"]
    assert final.rule_agendas[agenda.agenda_id].status == "failed"
    assert (
        await executor.executions_for_turn(
            room_id=state.room_id,
            turn_id="turn-budget",
        )
        == ()
    )


@pytest.mark.asyncio
async def test_transient_agenda_failure_uses_its_own_retry_budget() -> None:
    """未提交步骤保存退避点后必须通知 Turn 层保留原恢复链。"""

    module = ModuleContentV3.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    commits = 0

    def fail_first_agenda_commit(_room_id: str) -> None:
        nonlocal commits
        commits += 1
        if commits == 2:
            raise OSError("injected transient failure")

    store = InMemoryEngineStore(before_commit=fail_first_agenda_commit)
    state = create_initial_game_state(
        module,
        room_id="room-1",
        actors={
            "actor-1": ActorState(
                player_id="player-1",
                name="调查员",
                source_character_id="character-1",
                source_character_version=1,
                resources=ActorResources(san=60),
                state={"skills": {"spot_hidden": 50}},
            )
        },
    )
    store.register_room(module_content=module, initial_state=state)
    engine = AdjudicationEngineService(store)
    with engine_turn_context("turn-1"):
        await engine._submit_internal_adjudication(
            SubmitAdjudicationRequest(
                room_id="room-1",
                player_id="player-1",
                adjudication=ActionAdjudication(
                    request_id="see-ghoul",
                    source_revision="0",
                    actor_id="actor-1",
                    summary="看清墓地里的人影",
                    target=ActionTarget(kind="entity", id="cemetery_figure"),
                    method=ActionMethod(family="observe", description="仔细观察"),
                    check=NoAdjudicationCheck(),
                    success_effects=(
                        ChangeEntityStateEffect(
                            entity_id="cemetery_figure",
                            key="true_form_seen",
                            value=True,
                        ),
                    ),
                ),
            )
        )

    executor = RuleAgendaExecutor(
        store,
        engine=engine,
        dice=DiceRoller(SequenceDiceSource([50])),
    )
    with pytest.raises(AgendaRetryScheduledError):
        await executor.drain(room_id="room-1", turn_id="turn-1")

    agenda = next(iter(store.inspect_state("room-1").rule_agendas.values()))
    assert agenda.attempt_count == 1
    assert agenda.next_attempt_at is not None
    assert agenda.status == "awaiting_passive_check"


@pytest.mark.asyncio
async def test_effect_event_before_blocking_step_is_queued() -> None:
    """阻塞前 Effect 产生的事件必须进入同一 Agenda，不能在恢复时丢失。"""

    payload = ModuleContentV3.model_validate_json(
        FIXTURE.read_text(encoding="utf-8")
    ).to_json_dict()
    payload["rules"].append(
        {
            "id": "follow_first_sight_marker",
            "priority": 10,
            "trigger": {
                "kind": "event",
                "event_type": "entity.state_changed",
                "when": {
                    "op": "predicate",
                    "predicate": "entity_state_is",
                    "args": {
                        "entity_id": "case_tracker",
                        "key": "first_ghoul_sight_resolved",
                        "value": True,
                    },
                },
                "entry_branch_id": "default",
            },
            "execution": {
                "branches": [{"id": "default", "entry_step_id": "mark_followed"}],
                "steps": [
                    {
                        "id": "mark_followed",
                        "kind": "effect",
                        "effect": {
                            "type": "change_entity_state",
                            "entity_id": "case_tracker",
                            "key": "surveillance_available",
                            "value": True,
                        },
                        "next_step_id": "finish",
                    },
                    {"id": "finish", "kind": "finish"},
                ],
            },
        }
    )
    module = ModuleContentV3.model_validate(payload)
    store = InMemoryEngineStore()
    state = create_initial_game_state(
        module,
        room_id="room-1",
        actors={
            "actor-1": ActorState(
                player_id="player-1",
                name="调查员",
                source_character_id="character-1",
                source_character_version=1,
                resources=ActorResources(san=60),
            )
        },
    )
    store.register_room(module_content=module, initial_state=state)
    engine = AdjudicationEngineService(store)
    with engine_turn_context("turn-1"):
        await engine._submit_internal_adjudication(
            SubmitAdjudicationRequest(
                room_id="room-1",
                player_id="player-1",
                adjudication=ActionAdjudication(
                    request_id="see-ghoul",
                    source_revision="0",
                    actor_id="actor-1",
                    summary="看清墓地里的人影",
                    target=ActionTarget(kind="entity", id="cemetery_figure"),
                    method=ActionMethod(family="observe", description="仔细观察"),
                    check=NoAdjudicationCheck(),
                    success_effects=(
                        ChangeEntityStateEffect(
                            entity_id="cemetery_figure",
                            key="true_form_seen",
                            value=True,
                        ),
                    ),
                ),
            )
        )

    agenda = next(iter(store.inspect_state("room-1").rule_agendas.values()))
    queued_rules = {item.rule_id: item.status for item in agenda.queue}
    assert queued_rules["first_sight_of_douglas"] == "running"
    assert queued_rules["follow_first_sight_marker"] == "queued"


@pytest.mark.asyncio
async def test_world_time_clears_expired_temporary_condition() -> None:
    """到期 condition 由世界时间 Effect 清除，不依赖后台真实时钟。"""

    module = ModuleContentV3.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    actor = ActorState(
        player_id="player-1",
        name="调查员",
        source_character_id="character-1",
        source_character_version=1,
        conditions=("unconscious_until_night",),
        state={"condition_expirations": {"unconscious_until_night": 18}},
    )
    state = create_initial_game_state(
        module,
        room_id="room-1",
        actors={"actor-1": actor},
    )
    store = InMemoryEngineStore()
    store.register_room(module_content=module, initial_state=state)
    engine = AdjudicationEngineService(store)
    async with store.transaction("room-1") as transaction:
        runtime = await transaction.load_runtime()

    updated, events = engine._apply_effect(
        runtime,
        runtime.game_state,
        AdvanceWorldTimeEffect(to_point_id="hour_18"),
        room_id="room-1",
        request_id="advance-night",
        actor_id="actor-1",
        offset=1,
    )

    assert "unconscious_until_night" not in updated.actors["actor-1"].conditions
    assert [event.type for event in events] == [
        "time.point_entered",
        "actor.condition_expired",
    ]
