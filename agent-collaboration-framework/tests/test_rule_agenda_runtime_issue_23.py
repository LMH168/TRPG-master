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
    AwaitPlayerInputStep,
    ChangeEntityStateEffect,
    ContractError,
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
    AgendaSource,
    DiceRoller,
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


@pytest.mark.asyncio
async def test_player_input_boundary_only_accepts_published_option() -> None:
    """Host 只能选择安全候选，下一游标始终由 Engine 从模组恢复。"""

    original = ModuleContentV3.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    base_rule = original.rules[0]
    rule = base_rule.model_copy(
        update={
            "execution": RuleExecutionSpec(
                branches=(ExecutionBranchSpec(id="default", entry_step_id="wait"),),
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
    agenda = create_rule_agenda(
        agenda_id="agenda-wait",
        room_id="room-1",
        module=module,
        correlation_id="action-1",
        root_source=AgendaSource(kind="event", id="event-1"),
        revision="0",
        origin_turn_id="turn-1",
        active_turn_id="turn-1",
        player_id="player-1",
        actor_id="actor-1",
        current_source_event_id="event-1",
    ).model_copy(
        update={
            "active_turn_id": None,
            "status": "awaiting_player_input",
            "current_rule_id": rule.id,
            "current_branch_id": "default",
            "current_step_id": "wait",
            "pending_boundary_id": "door-choice",
            "queue": (
                AgendaItem(
                    source_event_id="event-1",
                    event_sequence=1,
                    rule_id=rule.id,
                    rule_priority=rule.priority,
                    branch_id="default",
                    status="running",
                ),
            ),
        },
        deep=True,
    )
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
    ).model_copy(update={"rule_agendas": {agenda.agenda_id: agenda}}, deep=True)
    store = InMemoryEngineStore()
    store.register_room(module_content=module, initial_state=state)
    engine = AdjudicationEngineService(store)
    executor = RuleAgendaExecutor(store, engine=engine)

    candidates = await executor.continuation_candidates(
        room_id="room-1", player_id="player-1", actor_id="actor-1"
    )
    assert candidates[0].options[0].option_id == "hold_breath"
    with pytest.raises(ContractError, match="option"):
        await executor.resume_continuation(
            AgendaContinuationProposal(
                agenda_id="agenda-wait",
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
            agenda_id="agenda-wait",
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
    assert store.inspect_state("room-1").rule_agendas["agenda-wait"].status == "stable"


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
