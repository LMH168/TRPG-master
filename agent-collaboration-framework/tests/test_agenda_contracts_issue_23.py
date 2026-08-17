"""Issue #23 的 Agenda v2、等待输入和 PlotThread 契约不变量测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from collaboration_framework.contracts import (
    AgendaContinuationProposal,
    AwaitPlayerInputStep,
    ContractError,
    EffectProposal,
    EffectStep,
    HostDecisionProposal,
    ModuleContentV3,
    NarrationPlotThread,
    PlotThreadSpec,
    PredicateCondition,
    PresentationStep,
    RuleInputOption,
    TransitionPlotThreadEffect,
)
from collaboration_framework.engine import (
    ActorState,
    AdjudicationEngineService,
    AgendaSource,
    AgendaStepExecution,
    EngineRuntimeSnapshot,
    InMemoryEngineStore,
    RuleAgenda,
    create_initial_game_state,
    project_narration_plot_threads,
    transition_plot_thread,
)
from collaboration_framework.engine.rules_v3 import (
    agenda_is_claimable,
    agenda_step_execution_id,
    create_rule_agenda,
)
from collaboration_framework.module import validate_module_v3

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "module-parser"
    / "examples"
    / "module-content-validation"
    / "追书人"
    / "module-content-v3.json"
)


def _module() -> ModuleContentV3:
    return ModuleContentV3.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def _execution(execution_id: str) -> AgendaStepExecution:
    return AgendaStepExecution(
        execution_id=execution_id,
        room_id="room-1",
        origin_turn_id="turn-origin",
        execution_turn_id="turn-current",
        agenda_id="agenda-1",
        source_event_id="event-1",
        rule_id="rule-1",
        branch_id="branch-1",
        step_id="step-1",
        execution_kind="passive_check",
        request={"profile": "coc7.skill"},
        result={"passed": True},
        committed_state_version=4,
        created_at=datetime(2026, 8, 16, tzinfo=UTC),
    )


def test_old_agenda_remains_readable_and_v2_requires_complete_identity() -> None:
    old = RuleAgenda.model_validate(
        {
            "agenda_id": "legacy-agenda",
            "room_id": "room-1",
            "module_id": "paper-chase",
            "module_version": "3.0.6",
            "correlation_id": "action-1",
            "root_source": {"kind": "action", "id": "action-1"},
            "revision": "1",
        }
    )
    assert old.schema_version == 1
    assert old.origin_turn_id is None

    with pytest.raises(ValidationError, match="必须包含 Turn"):
        RuleAgenda.model_validate(
            {
                **old.model_dump(mode="json"),
                "schema_version": 2,
                "origin_turn_id": "turn-1",
            }
        )


def test_new_agenda_writer_and_execution_identity_are_stable() -> None:
    agenda = create_rule_agenda(
        agenda_id="agenda-1",
        room_id="room-1",
        module=_module(),
        correlation_id="action-1",
        root_source=AgendaSource(kind="event", id="event-1"),
        revision="3",
        origin_turn_id="turn-1",
        active_turn_id="turn-1",
        player_id="player-1",
        actor_id="actor-1",
        current_source_event_id="event-1",
    )
    assert agenda.schema_version == 2

    identity = {
        "schema_version": agenda.schema_version,
        "module_id": agenda.module_id,
        "module_version": agenda.module_version,
        "agenda_id": agenda.agenda_id,
        "source_event_id": "event-1",
        "rule_id": "rule-1",
        "branch_id": "branch-1",
        "step_id": "step-1",
    }
    first = agenda_step_execution_id(**identity)
    assert first == agenda_step_execution_id(**identity)
    assert first != agenda_step_execution_id(
        schema_version=agenda.schema_version,
        module_id=agenda.module_id,
        module_version=agenda.module_version,
        agenda_id=agenda.agenda_id,
        source_event_id="event-1",
        rule_id="rule-1",
        branch_id="branch-1",
        step_id="step-2",
    )


def test_v2_backoff_and_owner_identity_are_recovery_boundaries() -> None:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    agenda = create_rule_agenda(
        agenda_id="agenda-1",
        room_id="room-1",
        module=_module(),
        correlation_id="action-1",
        root_source=AgendaSource(kind="event", id="event-1"),
        revision="3",
        origin_turn_id="turn-1",
        active_turn_id="turn-1",
        player_id="player-1",
        actor_id="actor-1",
        current_source_event_id="event-1",
    ).model_copy(update={"next_attempt_at": now + timedelta(seconds=5)})

    assert agenda_is_claimable(agenda, now=now) is False
    assert agenda_is_claimable(agenda, now=now + timedelta(seconds=5)) is True
    with pytest.raises(ValidationError, match="v1 不得携带"):
        RuleAgenda.model_validate(
            {
                **agenda.model_dump(mode="json"),
                "schema_version": 1,
            }
        )


@pytest.mark.asyncio
async def test_v2_checkpoint_cannot_rebind_player_identity() -> None:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    module = _module()
    agenda = create_rule_agenda(
        agenda_id="agenda-owner",
        room_id="room-1",
        module=module,
        correlation_id="action-1",
        root_source=AgendaSource(kind="event", id="event-1"),
        revision="3",
        origin_turn_id="turn-1",
        active_turn_id="turn-1",
        player_id="player-1",
        actor_id="actor-1",
        current_source_event_id="event-1",
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
    claimed = await store.claim_rule_agenda(
        room_id="room-1",
        worker_id="worker-1",
        now=now,
        lease_expires_at=now + timedelta(seconds=30),
    )
    assert claimed is not None

    with pytest.raises(ContractError, match="不可变身份"):
        await store.checkpoint_rule_agenda(
            agenda=claimed.model_copy(update={"player_id": "other-player"}),
            worker_id="worker-1",
            expected_lease_version=claimed.lease_version,
            now=now,
        )
    with pytest.raises(ContractError, match="一次性完整"):
        create_rule_agenda(
            agenda_id="agenda-2",
            room_id="room-1",
            module=_module(),
            correlation_id="action-2",
            root_source=AgendaSource(kind="event", id="event-2"),
            revision="3",
            origin_turn_id="turn-2",
        )


@pytest.mark.asyncio
async def test_in_memory_execution_registration_is_idempotent() -> None:
    module = _module()
    store = InMemoryEngineStore()
    store.register_room(
        module_content=module,
        initial_state=create_initial_game_state(
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
        ),
    )
    execution_id = agenda_step_execution_id(
        schema_version=2,
        module_id=module.module_id,
        module_version=module.version,
        agenda_id="agenda-1",
        source_event_id="event-1",
        rule_id="rule-1",
        branch_id="branch-1",
        step_id="step-1",
    )
    execution = _execution(execution_id)

    store.register_committed_agenda_step_execution(execution)
    store.register_committed_agenda_step_execution(execution)

    loaded = await store.find_agenda_step_execution(
        room_id="room-1", execution_id=execution_id
    )
    assert loaded == execution
    assert await store.list_agenda_step_executions(
        room_id="room-1", agenda_id="agenda-1"
    ) == (execution,)


def test_waiting_input_v2_has_finite_options_and_legacy_step_still_reads() -> None:
    legacy = AwaitPlayerInputStep(id="wait", schema_version=1, resume_step_id="finish")
    assert legacy.resume_step_id == "finish"

    current = AwaitPlayerInputStep(
        id="wait",
        schema_version=2,
        boundary_id="door-choice",
        player_safe_prompt="你要屏住呼吸还是直接进入？",
        options=(
            RuleInputOption(
                id="hold_breath",
                semantic_hints=("屏住呼吸", "先准备再进入"),
                next_step_id="hold_breath_check",
            ),
        ),
    )
    assert current.options[0].next_step_id == "hold_breath_check"
    continuation = AgendaContinuationProposal(
        agenda_id="agenda-1",
        boundary_id="door-choice",
        option_id="hold_breath",
    )
    assert "next_step_id" not in continuation.model_dump(mode="json")

    # PR2 允许 Host 选择服务端发布的有限 option，但仍不暴露 next_step_id。
    parsed = TypeAdapter(HostDecisionProposal).validate_python(
        continuation.model_dump(mode="json")
    )
    assert parsed == continuation


def test_plot_thread_initialization_and_terminal_transition_are_authoritative() -> None:
    module = _module().model_copy(
        update={
            "plot_threads": (
                PlotThreadSpec(
                    id="crypt_entry",
                    initial_status="available",
                    visibility="player",
                    player_safe_summary="调查地穴入口",
                ),
            )
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
    )
    started = transition_plot_thread(
        module,
        state,
        thread_id="crypt_entry",
        to_status="in_progress",
        event_id="event-start",
    )
    state = state.model_copy(
        update={"plot_threads": {"crypt_entry": started}}, deep=True
    )
    resolved = transition_plot_thread(
        module,
        state,
        thread_id="crypt_entry",
        to_status="resolved",
        event_id="event-resolved",
    )
    state = state.model_copy(
        update={"plot_threads": {"crypt_entry": resolved}}, deep=True
    )
    assert (
        transition_plot_thread(
            module,
            state,
            thread_id="crypt_entry",
            to_status="resolved",
            event_id="event-resolved",
        )
        == resolved
    )
    with pytest.raises(ValueError, match="非法迁移"):
        transition_plot_thread(
            module,
            state,
            thread_id="crypt_entry",
            to_status="in_progress",
            event_id="event-reopen",
        )


def test_plot_thread_dependencies_gate_initialization_and_unlock() -> None:
    with pytest.raises(ValidationError, match="必须以 locked 状态开始"):
        PlotThreadSpec(
            id="dependent",
            initial_status="available",
            dependency_thread_ids=("foundation",),
        )

    module = _module().model_copy(
        update={
            "plot_threads": (
                PlotThreadSpec(id="foundation", initial_status="available"),
                PlotThreadSpec(
                    id="dependent",
                    dependency_thread_ids=("foundation",),
                ),
            )
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
    )
    with pytest.raises(ContractError, match="依赖尚未完成"):
        transition_plot_thread(
            module,
            state,
            thread_id="dependent",
            to_status="available",
            event_id="event-unlock-early",
        )

    foundation = transition_plot_thread(
        module,
        state,
        thread_id="foundation",
        to_status="in_progress",
        event_id="event-foundation-start",
    )
    state = state.model_copy(
        update={"plot_threads": {**state.plot_threads, "foundation": foundation}},
        deep=True,
    )
    foundation = transition_plot_thread(
        module,
        state,
        thread_id="foundation",
        to_status="resolved",
        event_id="event-foundation-resolved",
    )
    state = state.model_copy(
        update={"plot_threads": {**state.plot_threads, "foundation": foundation}},
        deep=True,
    )
    unlocked = transition_plot_thread(
        module,
        state,
        thread_id="dependent",
        to_status="available",
        event_id="event-unlock",
    )
    assert unlocked.status == "available"


def test_plot_thread_visibility_controls_events_and_narration_projection() -> None:
    """隐藏线程即使发生转换，也不能通过 Event 或 NarrationContext 泄露。"""

    module = _module().model_copy(
        update={
            "plot_threads": (
                PlotThreadSpec(
                    id="public-thread",
                    initial_status="available",
                    visibility="player",
                    player_safe_summary="公开调查方向",
                ),
                PlotThreadSpec(
                    id="keeper-thread",
                    initial_status="available",
                    visibility="hidden",
                ),
            )
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
    )
    store = InMemoryEngineStore()
    engine = AdjudicationEngineService(store)
    runtime = EngineRuntimeSnapshot(
        module_id=module.module_id,
        module_version=module.version,
        module_content=module,
        game_state=state,
        revision=str(state.event_sequence),
    )

    state, public_events = engine._apply_effect(
        runtime,
        state,
        TransitionPlotThreadEffect(
            thread_id="public-thread",
            to_status="in_progress",
        ),
        room_id="room-1",
        request_id="request-public",
        actor_id="actor-1",
        offset=1,
    )
    state, hidden_events = engine._apply_effect(
        runtime,
        state,
        TransitionPlotThreadEffect(
            thread_id="keeper-thread",
            to_status="in_progress",
        ),
        room_id="room-1",
        request_id="request-hidden",
        actor_id="actor-1",
        offset=2,
    )

    assert public_events[0].visibility == "public"
    assert "player_safe_summary" in public_events[0].payload
    assert hidden_events[0].visibility == "hidden"
    assert "player_safe_summary" not in hidden_events[0].payload
    assert project_narration_plot_threads(module, state) == (
        NarrationPlotThread(
            thread_id="public-thread",
            status="in_progress",
            player_safe_summary="公开调查方向；调查正在推进。",
            last_transition_event_ref=public_events[0].event_id,
        ),
    )


def test_host_effect_union_cannot_construct_plot_thread_transition() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(EffectProposal).validate_python(
            {
                "type": "transition_plot_thread",
                "thread_id": "crypt_entry",
                "to_status": "resolved",
            }
        )


def test_blocking_agent_rule_requires_player_safe_presentation() -> None:
    """换用其他模组时，阻塞 Agenda 也不能在没有安全结果的情况下发布。"""

    module = _module()
    rule = next(item for item in module.rules if item.id == "crypt_stench_on_entry")
    steps = tuple(
        step.model_copy(update={"next_step_id": "crypt_finish"}, deep=True)
        if step.id == "faint_willing"
        else step
        for step in rule.execution.steps
        if not isinstance(step, PresentationStep)
    )
    changed_rule = rule.model_copy(
        update={
            "presentation": None,
            "execution": rule.execution.model_copy(update={"steps": steps}, deep=True),
        },
        deep=True,
    )
    changed = module.model_copy(
        update={
            "rules": tuple(
                changed_rule if item.id == changed_rule.id else item
                for item in module.rules
            )
        },
        deep=True,
    )

    report = validate_module_v3(changed)

    assert "MODULE_V3_AGENDA_PRESENTATION_REQUIRED" in {
        issue.code for issue in report.errors
    }


def test_blocking_agent_rule_rejects_presentation_on_unrelated_branch() -> None:
    """其他分支存在 Presentation，也不能掩盖阻塞结果路径缺少安全证据。"""

    module = _module()
    rule = next(item for item in module.rules if item.id == "crypt_stench_on_entry")
    steps = tuple(
        step.model_copy(update={"next_step_id": "present_faint"}, deep=True)
        if step.id == "enter_safely"
        else step.model_copy(update={"next_step_id": "crypt_finish"}, deep=True)
        if step.id == "faint_willing"
        else step
        for step in rule.execution.steps
    )
    changed_rule = rule.model_copy(
        update={
            "execution": rule.execution.model_copy(update={"steps": steps}, deep=True)
        },
        deep=True,
    )
    changed = module.model_copy(
        update={
            "rules": tuple(
                changed_rule if item.id == changed_rule.id else item
                for item in module.rules
            )
        },
        deep=True,
    )

    report = validate_module_v3(changed)

    assert "MODULE_V3_AGENDA_PRESENTATION_REQUIRED" in {
        issue.code for issue in report.errors
    }


def test_module_validation_checks_plot_dependencies_and_transition_targets() -> None:
    module = _module()
    missing_dependency = module.model_copy(
        update={
            "plot_threads": (
                PlotThreadSpec(
                    id="crypt_entry",
                    dependency_thread_ids=("missing_thread",),
                ),
            )
        },
        deep=True,
    )
    report = validate_module_v3(missing_dependency)
    assert "MODULE_V3_PLOT_THREAD_NOT_FOUND" in {issue.code for issue in report.errors}

    rule = next(
        item
        for item in module.rules
        if any(isinstance(step, EffectStep) for step in item.execution.steps)
    )
    replaced = False
    steps = []
    for step in rule.execution.steps:
        if not replaced and isinstance(step, EffectStep):
            steps.append(
                step.model_copy(
                    update={
                        "effect": TransitionPlotThreadEffect(
                            thread_id="missing_thread",
                            to_status="available",
                        )
                    },
                    deep=True,
                )
            )
            replaced = True
        else:
            steps.append(step)
    changed_rule = rule.model_copy(
        update={
            "execution": rule.execution.model_copy(
                update={"steps": tuple(steps)}, deep=True
            )
        },
        deep=True,
    )
    changed_rules = tuple(
        changed_rule if item.id == rule.id else item for item in module.rules
    )
    report = validate_module_v3(
        module.model_copy(update={"rules": changed_rules}, deep=True)
    )
    assert "MODULE_V3_PLOT_THREAD_NOT_FOUND" in {issue.code for issue in report.errors}

    event_rule = next(
        item for item in module.rules if getattr(item.trigger, "kind", None) == "event"
    )
    changed_event_rule = event_rule.model_copy(
        update={
            "trigger": event_rule.trigger.model_copy(
                update={
                    "when": PredicateCondition(
                        predicate="plot_thread_status_is",
                        args={"thread_id": "missing_thread", "status": "reopened"},
                    )
                },
                deep=True,
            )
        },
        deep=True,
    )
    report = validate_module_v3(
        module.model_copy(
            update={
                "rules": tuple(
                    changed_event_rule if item.id == event_rule.id else item
                    for item in module.rules
                )
            },
            deep=True,
        )
    )
    codes = {issue.code for issue in report.errors}
    assert "MODULE_V3_PLOT_THREAD_NOT_FOUND" in codes
    assert "MODULE_V3_PLOT_THREAD_STATUS_INVALID" in codes
