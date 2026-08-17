from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock

import pytest
from collaboration_framework.contracts import (
    ActionPlanPolicy,
    AgendaContinuationProposal,
    CommittedResult,
    NarrationEvidence,
    NarrationPlotThread,
    PlayerInput,
    PlayerView,
    PostRollDecisionRequest,
)
from collaboration_framework.engine import (
    ActorState,
    AgendaStepExecution,
    InMemoryEngineStore,
    PlotThreadState,
    create_initial_game_state,
    engine_turn_context,
)
from collaboration_framework.host.application import (
    NarrationValidationError,
)
from collaboration_framework.host.application.narrator import (
    unsupported_focus_shift_claim,
)
from collaboration_framework.host.schemas import (
    CompletedPlanStepSummary,
    NarrationContext,
    NarrationOutput,
    RecentTurnContext,
)
from collaboration_framework.memory import MemoryContext

from app.core.action_plan_turn import ActionPlanTurnApplication
from app.service.paper_chase_loader import PAPER_CHASE_SOURCE_PATH


def _narration_store(
    room_id: str,
    *,
    crypt_status: Literal["locked", "available", "in_progress", "resolved", "failed"] = "locked",
) -> InMemoryEngineStore:
    """构造 Narrator 读取最终 PlotThread snapshot 所需的最小真实 Store。"""

    from collaboration_framework.contracts import ModuleContentV3

    module = ModuleContentV3.model_validate_json(
        PAPER_CHASE_SOURCE_PATH.read_text(encoding="utf-8")
    )
    state = create_initial_game_state(
        module,
        room_id=room_id,
        actors={
            "actor-281": ActorState(
                player_id="player-281",
                name="调查员",
                source_character_id="character-281",
                source_character_version=1,
            )
        },
    )
    if crypt_status != "locked":
        current = state.plot_threads["crypt_entry_investigation"]
        state = state.model_copy(
            update={
                "plot_threads": {
                    **state.plot_threads,
                    "crypt_entry_investigation": PlotThreadState(
                        thread_id=current.thread_id,
                        status=crypt_status,
                        version=current.version + 1,
                        last_transition_event_id="evt-final-thread",
                    ),
                }
            },
            deep=True,
        )
    store = InMemoryEngineStore()
    store.register_room(module_content=module, initial_state=state)
    return store


def _run(*, cancel_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        room_id="room-281",
        player_id="player-281",
        actor_id="actor-281",
        parent_action_id="plan-281",
        parent_utterance="完成连续行动",
        plan=SimpleNamespace(goal="完成连续行动"),
        plan_id="plan-281",
        status="waiting_for_player",
        current_step_index=0,
        pending_cancel_request_id=cancel_id,
        steps=(
            SimpleNamespace(
                step_request_id="step-281",
                status="waiting_for_player",
                adjudication_execution=None,
            ),
        ),
    )


def _status(status: str) -> SimpleNamespace:
    check_run = SimpleNamespace(
        check_id="check-281",
        version=1,
        post_roll_options=(SimpleNamespace(kind="accept_result", option_id="accept-current"),),
    )
    execution = SimpleNamespace(
        status=status,
        view_revision="revision-7",
        check_run=check_run if status == "awaiting_post_roll_decision" else None,
    )
    return SimpleNamespace(status=status, execution=execution)


class _Engine:
    def __init__(self, status: SimpleNamespace) -> None:
        self.status = status
        self.status_requests = []
        self.post_roll_requests: list[PostRollDecisionRequest] = []

    async def get_status(self, request):
        self.status_requests.append(request)
        return self.status

    async def decide_post_roll(self, request: PostRollDecisionRequest):
        self.post_roll_requests.append(request)
        self.status = _status("resolved")
        return self.status.execution


class _Orchestrator:
    def __init__(self, run: SimpleNamespace) -> None:
        self.run = run
        self.resume_calls = []
        self.adjudicator = object()
        self.policy = ActionPlanPolicy(max_repair_attempts=3)

    async def get_run(self, room_id: str, parent_action_id: str):
        assert room_id == self.run.room_id
        assert parent_action_id == self.run.parent_action_id
        return self.run

    async def resume_owned(self, **kwargs):
        self.resume_calls.append(kwargs)
        return SimpleNamespace(run=self.run)


class _NarrationContextStub:
    def __init__(self, evidence: NarrationEvidence, termination_status: str) -> None:
        self.narration_evidence = (evidence,)
        self.termination_status = termination_status
        self.narration_retry_hint: str | None = None
        self.player_input = SimpleNamespace(
            room_id="room-281",
            client_action_id="action-narration-test",
            utterance="",
        )
        self.player_view = SimpleNamespace(
            scene=SimpleNamespace(visible_entities=()),
        )
        self.completed_steps: tuple[object, ...] = ()
        self.plot_threads: tuple[NarrationPlotThread, ...] = ()

    def model_copy(self, *, update: dict[str, object]):
        copied = _NarrationContextStub(
            self.narration_evidence[0],
            self.termination_status,
        )
        for key, value in update.items():
            setattr(copied, key, value)
        return copied


class _FailingNarrationStore:
    """模拟 Engine 已提交后，Narrator 的只读 snapshot 瞬态不可用。"""

    class _Transaction:
        async def __aenter__(self):
            raise OSError("snapshot unavailable")

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback

    def transaction(self, room_id: str) -> _Transaction:
        assert room_id == "room-281"
        return self._Transaction()


def _application(run: SimpleNamespace, engine: _Engine, orchestrator: _Orchestrator):
    application = object.__new__(ActionPlanTurnApplication)
    application._adjudication_engine = engine
    application._orchestrator = orchestrator
    application._resolve_actor_id = AsyncMock(return_value=run.actor_id)
    application._finish_plan_with_phases = AsyncMock(return_value="recovered")
    return application


def test_application_injects_plan_repair_dependencies_into_single_action_path() -> None:
    run = _run(cancel_id=None)
    engine = _Engine(_status("resolved"))
    orchestrator = _Orchestrator(run)

    application = ActionPlanTurnApplication(
        store=cast(Any, object()),
        engine=cast(Any, object()),
        adjudication_engine=cast(Any, engine),
        planner=cast(Any, object()),
        orchestrator=cast(Any, orchestrator),
        narrator=cast(Any, object()),
        recent_history_source=cast(Any, object()),
        recent_history_budget=cast(Any, object()),
        recent_history_enabled=False,
    )

    assert application._dispatcher._repair_adjudicator is orchestrator.adjudicator
    assert application._dispatcher._policy is orchestrator.policy


@pytest.mark.parametrize(
    ("outcomes", "goal_outcomes", "termination_status", "expected"),
    (
        (
            ("failure",),
            ("legacy_unknown",),
            "resolved",
            "这次检定或行动未能成功，当前局面没有产生新的确认变化。",
        ),
        (
            ("success", "failure"),
            ("legacy_unknown", "legacy_unknown"),
            "stopped",
            "当前检定或行动未能成功；此前已经确认的结果仍然保留。",
        ),
        (("cancelled",), ("cancelled",), "cancelled", "这次行动已经取消。"),
        (
            ("success",),
            ("legacy_unknown",),
            "resolved",
            "这次行动已经完成，当前状态已按确认结果更新。",
        ),
        (
            ("success",),
            ("not_achieved",),
            "stopped",
            "检定或过程已经完成，但完整目标尚未形成结果。",
        ),
        (
            ("failure",),
            ("not_achieved",),
            "resolved",
            "这次检定或行动未能成功，当前局面没有产生新的确认变化。",
        ),
    ),
)
def test_deterministic_narration_fallback_preserves_action_outcome(
    outcomes: tuple[str, ...],
    goal_outcomes: tuple[str, ...],
    termination_status: str,
    expected: str,
) -> None:
    """模型叙事被拒绝后，兜底文案仍必须忠实表达权威行动结果。"""

    context = SimpleNamespace(
        termination_status=termination_status,
        player_input=SimpleNamespace(client_action_id="action-fallback"),
        completed_steps=tuple(
            SimpleNamespace(
                outcome=outcome,
                goal_outcome=goal_outcome,
                committed_results=(),
            )
            for outcome, goal_outcome in zip(outcomes, goal_outcomes, strict=True)
        ),
        player_view=SimpleNamespace(
            scene=SimpleNamespace(visible_entities=()),
        ),
    )

    output = ActionPlanTurnApplication._deterministic_narration_fallback(cast(Any, context))

    assert output.text == expected


def test_success_fallback_includes_authoritative_scene_and_time() -> None:
    """无提交结果时，兜底也必须反馈最终 PlayerView，而不是只返回模板。"""

    context = SimpleNamespace(
        termination_status="resolved",
        player_input=SimpleNamespace(client_action_id="action-context-fallback"),
        completed_steps=(
            SimpleNamespace(
                outcome="success",
                goal_outcome="achieved",
                committed_results=(),
            ),
        ),
        player_view=SimpleNamespace(
            scene=SimpleNamespace(
                name="阿诺兹堡公共墓地",
                visible_entities=(),
                visible_actors=(SimpleNamespace(name="守墓人"),),
            ),
            world=SimpleNamespace(day_index=1, hour_of_day=6, time_of_day="day"),
        ),
    )

    output = ActionPlanTurnApplication._deterministic_narration_fallback(cast(Any, context))

    assert "这次行动已经完成" not in output.text
    assert "阿诺兹堡公共墓地" in output.text
    assert "第2天06:00" in output.text
    assert "守墓人" in output.text


def test_narration_fallback_failure_still_returns_safe_result(monkeypatch) -> None:
    """历史上下文异常时，叙事兜底失败也不能让已提交回合进入内部错误。"""

    def fail(_context):
        raise ValueError("malformed historical context")

    monkeypatch.setattr(
        ActionPlanTurnApplication,
        "_deterministic_narration_fallback",
        staticmethod(fail),
    )

    output = ActionPlanTurnApplication._safe_narration_fallback(cast(Any, object()))

    assert output.text == "行动结果已经保存，当前状态已更新。"


def test_stopped_plan_clarification_requires_a_new_action() -> None:
    """计划停止后应明确后续未执行，并要求玩家重新提交新行动。"""

    context = SimpleNamespace(
        termination_status="needs_clarification",
        player_input=SimpleNamespace(
            client_action_id="stopped-plan",
            utterance="射击守墓人然后丢下手枪",
        ),
        completed_steps=(),
        blocked_step_goal="射击守墓人",
        remaining_step_goals=("丢下手枪",),
        player_safe_failure_reason="规则只确认了命中，未确认死亡",
        player_view=SimpleNamespace(scene=SimpleNamespace(visible_entities=())),
    )

    output = ActionPlanTurnApplication._deterministic_narration_fallback(cast(Any, context))

    assert output.kind == "clarification"
    assert "后续步骤尚未执行：丢下手枪" in output.text
    assert "重新提交新的行动" in output.text


def test_stopped_plan_clarification_keeps_completed_steps_and_lists_pending() -> None:
    """已有成功步骤时仍要报告阻塞点，不能把后续步骤含糊成一个整体。"""

    context = SimpleNamespace(
        termination_status="needs_clarification",
        player_input=SimpleNamespace(
            client_action_id="partially-stopped-plan",
            utterance="观察墓碑然后射击守墓人再丢下手枪",
        ),
        completed_steps=(
            SimpleNamespace(
                outcome="success",
                semantic_goal="观察墓碑",
                committed_results=(),
            ),
        ),
        blocked_step_goal="射击守墓人",
        remaining_step_goals=("丢下手枪",),
        player_safe_failure_reason=None,
        player_view=SimpleNamespace(scene=SimpleNamespace(visible_entities=())),
    )

    output = ActionPlanTurnApplication._deterministic_narration_fallback(cast(Any, context))

    assert output.text.startswith("此前已经完成的步骤仍然有效")
    assert "射击守墓人" in output.text
    assert "后续步骤尚未执行：丢下手枪" in output.text


def test_real_model_focus_shift_wording_is_rejected_without_name_hardcoding() -> None:
    """真实模型写成“道格拉斯墓碑”时也必须被通用实体焦点门禁拒绝。"""

    visible = (
        SimpleNamespace(id="melodias", name="守墓人梅洛迪亚斯", aliases=("守墓人",)),
        SimpleNamespace(
            id="douglas_grave",
            name="道格拉斯的墓碑",
            aliases=("墓碑",),
        ),
    )
    text = "守墓人承接了三声敲击的话题，随后却说今天在道格拉斯墓碑旁看到了一些新土迹。"

    shifted = unsupported_focus_shift_claim(
        text,
        focus_entity_ids=("melodias",),
        visible_entities=visible,
        evidence_subject_ids=set(),
    )

    assert shifted == "douglas_grave"


def test_recent_history_rebinds_to_post_commit_player_view_revision() -> None:
    """单动作提交推进 revision 后，叙事历史应只重绑定截止点而不改写内容。"""

    history = SimpleNamespace(as_of_revision="before", model_copy=lambda *, update: update)
    player_view = SimpleNamespace(revision="after")

    rebound = ActionPlanTurnApplication._rebind_recent_history(
        cast(RecentTurnContext, history),
        player_view=cast(PlayerView, player_view),
    )

    assert rebound == {"as_of_revision": "after"}


def test_clarification_fallback_points_to_visible_dead_body() -> None:
    """模型连续忽略可见尸体时，兜底应给出权威位置而不是继续要求寻找。"""

    context = SimpleNamespace(
        termination_status="needs_clarification",
        player_input=SimpleNamespace(
            client_action_id="find-body",
            utterance="去找他的尸体",
        ),
        completed_steps=(),
        player_view=SimpleNamespace(
            scene=SimpleNamespace(
                visible_entities=(
                    SimpleNamespace(
                        name="梅洛迪亚斯·杰弗逊",
                        observable_state=(SimpleNamespace(key="consciousness", value="dead"),),
                    ),
                ),
            ),
        ),
    )

    output = ActionPlanTurnApplication._deterministic_narration_fallback(cast(Any, context))

    assert output.kind == "clarification"
    assert output.text.startswith("梅洛迪亚斯·杰弗逊的尸体就在当前场景中")


def test_unresolved_travel_fallback_narrates_not_found_without_substitution() -> None:
    """无法创建的明确地点应说没找到，不应返回通用表单式澄清。"""

    context = SimpleNamespace(
        termination_status="needs_clarification",
        player_input=SimpleNamespace(
            client_action_id="unresolved-clinic",
            utterance="去一个与当前背景冲突的诊所",
        ),
        completed_steps=(),
        player_view=SimpleNamespace(scene=SimpleNamespace(visible_entities=())),
    )

    output = ActionPlanTurnApplication._deterministic_narration_fallback(cast(Any, context))

    assert "没有" in output.text
    assert "找到" in output.text
    assert "仍停留在原处" in output.text
    assert "作用于谁或什么" not in output.text
    assert "具体变化" not in output.text


def test_partial_travel_success_fallback_keeps_the_arrival() -> None:
    """旅行已提交、后续步骤失败时，保底叙事不能把玩家送回原处。"""

    context = SimpleNamespace(
        termination_status="needs_clarification",
        player_input=SimpleNamespace(
            client_action_id="partial-inn",
            utterance="去旅馆，开一间房休息",
        ),
        completed_steps=(
            SimpleNamespace(
                outcome="success",
                semantic_goal="前往旅馆",
                committed_results=(),
            ),
        ),
        player_view=SimpleNamespace(
            scene=SimpleNamespace(
                name="镇上的旅店",
                visible_entities=(),
            ),
        ),
    )

    output = ActionPlanTurnApplication._deterministic_narration_fallback(cast(Any, context))

    assert output.kind == "clarification"
    assert "已经抵达镇上的旅店" in output.text
    assert "后续行动" in output.text
    assert "没有" not in output.text
    assert "仍停留在原处" not in output.text


def test_planning_failure_returns_host_reply_without_execution() -> None:
    """规划结构连续失败时必须有主持人回复，并保持零权威写入。"""

    player_input = PlayerInput(
        room_id="room-315",
        player_id="player-315",
        actor_id="actor-315",
        client_action_id="76664d06-3ac2-411b-8986-1ff12ed53cbf",
        utterance="去墓地",
    )
    result = ActionPlanTurnApplication._planning_failure_clarification(
        player_input=player_input,
        player_view=cast(Any, SimpleNamespace()),
    )

    assert result.status == "needs_clarification"
    assert result.execution is None
    assert result.narration is not None
    assert result.narration.kind == "clarification"
    assert "行动的对象或地点" in result.narration.text


@pytest.mark.asyncio
async def test_narration_falls_back_to_required_player_safe_evidence() -> None:
    application = object.__new__(ActionPlanTurnApplication)
    application._store = _narration_store("room-281")
    narrate = AsyncMock(side_effect=NarrationValidationError("required_evidence_missing"))
    application._narrator = SimpleNamespace(narrate=narrate)
    evidence = NarrationEvidence(
        ref="evt-crypt-discovered",
        kind="entity_discovered",
        subject_id="crypt_entrance",
        subject_name="石板下的地穴入口",
        description="一块沉重石板遮住了向下的通道。",
        required_in_narration=True,
    )
    context = cast(
        NarrationContext,
        _NarrationContextStub(evidence, "resolved"),
    )

    narration = await application._narrate(context)

    assert narrate.await_count == 2
    retry_context = narrate.await_args_list[1].args[0]
    assert "石板下的地穴入口" in retry_context.narration_retry_hint
    assert "claim" in retry_context.narration_retry_hint
    assert narration.claimed_evidence_refs == (evidence.ref,)
    assert "石板下的地穴入口" in narration.text
    assert "沉重石板" in narration.text
    assert "。。" not in narration.text


@pytest.mark.asyncio
async def test_not_achieved_goal_still_narrates_committed_rule_result() -> None:
    """完整目标未满足时仍应调用 Narrator 表达实际结果，不能直接返回地点模板。"""

    application = object.__new__(ActionPlanTurnApplication)
    application._store = _narration_store("room-281")
    expected = NarrationOutput(text="沉重的石板被推到一旁，向下的通道显露出来。")
    narrate = AsyncMock(return_value=expected)
    application._narrator = SimpleNamespace(narrate=narrate)
    context = _NarrationContextStub(
        NarrationEvidence(
            ref="evt-slab-moved",
            kind="rule_presentation",
            subject_id="slab-moved",
            subject_name="沉重的入口石板已经被你推开，通往下方的通道显露出来。",
            description="沉重的入口石板已经被你推开，通往下方的通道显露出来。",
        ),
        "resolved",
    )
    context.completed_steps = (SimpleNamespace(outcome="success", goal_outcome="not_achieved"),)

    narration = await application._narrate(cast(NarrationContext, context))

    assert narration == expected
    narrate.assert_awaited_once()


def test_merge_agenda_results_restores_persisted_player_safe_evidence() -> None:
    """叙事前恢复必须按 Turn execution 合并 Agenda 公开结果与 Presentation。"""

    event_ref = "evt-agenda-condition"
    presentation_ref = "evt-agenda-presentation"
    step = CompletedPlanStepSummary(
        step_index=0,
        semantic_goal="进入地穴",
        outcome="success",
        goal_outcome="not_achieved",
        view_revision="4",
    )
    context = NarrationContext.model_construct(
        background="测试背景",
        player_input=SimpleNamespace(),
        plan_goal="进入地穴",
        termination_status="resolved",
        completed_steps=(step,),
        player_view=SimpleNamespace(revision="7"),
        memory_context=SimpleNamespace(),
        allowed_evidence_refs=(),
        narration_evidence=(),
    )
    execution = AgendaStepExecution(
        execution_id="a" * 64,
        room_id="room-1",
        origin_turn_id="turn-1",
        execution_turn_id="turn-1",
        agenda_id="agenda-1",
        source_event_id="evt-source",
        rule_id="enter-crypt",
        branch_id="just-enter",
        step_id="faint",
        execution_kind="ruleset_action",
        result={
            "public_event_refs": [event_ref, presentation_ref],
            "committed_results": [
                CommittedResult(
                    kind="character_state",
                    target_id="actor-1",
                    state_key="consciousness",
                    state_value="unconscious",
                    event_ref=event_ref,
                ).to_json_dict()
            ],
            "narration_evidence": [
                NarrationEvidence(
                    ref=presentation_ref,
                    kind="rule_presentation",
                    subject_id="crypt-faint",
                    subject_name="地穴恶臭令你失去意识。",
                    description="地穴恶臭令你失去意识。",
                    required_in_narration=True,
                ).to_json_dict()
            ],
        },
        committed_state_version=7,
        created_at=datetime.now(UTC),
    )

    merged = ActionPlanTurnApplication._merge_agenda_results(context, (execution,))

    assert merged.completed_steps[0].view_revision == "7"
    assert merged.allowed_evidence_refs == (event_ref, presentation_ref)
    assert merged.completed_steps[0].committed_results[0].state_value == "unconscious"
    assert merged.narration_evidence[0].ref == presentation_ref


@pytest.mark.asyncio
async def test_agenda_continuation_narrates_persisted_execution_evidence() -> None:
    """有限选择稳定后必须进入统一 Narrator，不能返回固定占位句。"""

    player_input = PlayerInput(
        room_id="room-agenda",
        player_id="player-agenda",
        actor_id="actor-agenda",
        client_action_id="continue-agenda",
        utterance="屏住呼吸",
    )
    world = SimpleNamespace(day_index=0, hour_of_day=18, time_of_day="night")
    initial_view = PlayerView.model_construct(
        room_id=player_input.room_id,
        player_id=player_input.player_id,
        actor_id=player_input.actor_id,
        background="测试背景",
        scene_id="crypt",
        phase="playing",
        revision="4",
        self_actor=SimpleNamespace(id=player_input.actor_id),
        scene=SimpleNamespace(id="crypt", visible_actors=(), visible_entities=()),
        world=world,
    )
    final_view = initial_view.model_copy(update={"revision": "5"})
    event_ref = "evt-entered"
    execution = AgendaStepExecution(
        execution_id="b" * 64,
        room_id=player_input.room_id,
        origin_turn_id="turn-origin",
        execution_turn_id="turn-continuation",
        agenda_id="agenda-1",
        source_event_id="evt-source",
        rule_id="rule-1",
        branch_id="hold-breath",
        step_id="enter",
        execution_kind="effect_segment",
        result={
            "public_event_refs": [event_ref],
            "committed_results": [
                CommittedResult(
                    kind="location",
                    target_id="crypt",
                    event_ref=event_ref,
                ).to_json_dict()
            ],
        },
        committed_state_version=5,
        created_at=datetime.now(UTC),
    )
    agenda_executor = SimpleNamespace(
        resume_continuation=AsyncMock(),
        drain=AsyncMock(),
        executions_for_turn=AsyncMock(return_value=(execution,)),
        continuation_status=AsyncMock(return_value="stable"),
    )
    narrator = SimpleNamespace(
        narrate=AsyncMock(return_value=NarrationOutput(text="你屏住呼吸进入地穴。"))
    )
    application = object.__new__(ActionPlanTurnApplication)
    application._store = _narration_store(player_input.room_id)
    application._agenda_executor = agenda_executor
    application._projector = SimpleNamespace(project=AsyncMock(return_value=final_view))
    application._narrator = narrator
    application._read_memory_context = AsyncMock(
        return_value=MemoryContext(
            room_id=player_input.room_id,
            viewer_player_id=player_input.player_id,
            viewer_actor_id=player_input.actor_id,
            as_of_revision=final_view.revision,
        )
    )

    with engine_turn_context("turn-continuation"):
        result = await application._from_agenda_continuation(
            player_input=player_input,
            decision=AgendaContinuationProposal(
                agenda_id="agenda-1",
                boundary_id="door-choice",
                option_id="hold-breath",
            ),
            initial_view=initial_view,
            recent_history=None,
            on_phase=None,
        )

    assert result.narration is not None
    assert result.narration.text == "你屏住呼吸进入地穴。"
    context = narrator.narrate.await_args.args[0]
    assert context.allowed_evidence_refs == (event_ref,)
    assert context.completed_steps[0].committed_results[0].target_id == "crypt"


@pytest.mark.asyncio
async def test_required_evidence_fallback_never_changes_clarification_scope() -> None:
    application = object.__new__(ActionPlanTurnApplication)
    application._store = _narration_store("room-281")
    narrate = AsyncMock(side_effect=NarrationValidationError("required_evidence_missing"))
    application._narrator = SimpleNamespace(narrate=narrate)
    evidence = NarrationEvidence(
        ref="evt-crypt-discovered",
        kind="entity_discovered",
        subject_id="crypt_entrance",
        subject_name="石板下的地穴入口",
        required_in_narration=True,
    )
    context = cast(
        NarrationContext,
        _NarrationContextStub(evidence, "needs_clarification"),
    )

    narration = await application._narrate(context)

    assert narration.kind == "clarification"
    assert evidence.subject_name not in narration.text
    assert narration.claimed_evidence_refs == ()
    assert narrate.await_count == 2


@pytest.mark.asyncio
async def test_narration_reloads_final_plot_thread_state_before_model_call() -> None:
    """Narrator 必须看到最终 Engine 状态，不能沿用提交前的剧情线程快照。"""

    application = object.__new__(ActionPlanTurnApplication)
    application._store = _narration_store("room-281", crypt_status="in_progress")
    narrate = AsyncMock(return_value=NarrationOutput(text="地穴入口的调查仍在推进。"))
    application._narrator = SimpleNamespace(narrate=narrate)
    context = _NarrationContextStub(
        NarrationEvidence(
            ref="evt-thread",
            kind="plot_thread_transition",
            subject_id="crypt_entry_investigation",
            subject_name="地穴入口调查",
            description="地穴入口调查正在推进。",
        ),
        "resolved",
    )
    context.plot_threads = (
        NarrationPlotThread(
            thread_id="crypt_entry_investigation",
            status="resolved",
            player_safe_summary="这是提交前的过时摘要。",
        ),
    )

    await application._narrate(cast(NarrationContext, context))

    assert narrate.await_args is not None
    final_context = narrate.await_args.args[0]
    assert len(final_context.plot_threads) == 1
    assert final_context.plot_threads[0].status == "in_progress"
    assert "过时摘要" not in final_context.plot_threads[0].player_safe_summary


@pytest.mark.asyncio
async def test_plot_thread_read_failure_does_not_fail_committed_narration() -> None:
    """提交后的附加只读失败必须降级，不能重新制造 TURN_INTERNAL_ERROR。"""

    application = object.__new__(ActionPlanTurnApplication)
    application._store = _FailingNarrationStore()
    expected = NarrationOutput(text="已提交的结果仍然有效。")
    narrate = AsyncMock(return_value=expected)
    application._narrator = SimpleNamespace(narrate=narrate)
    context = _NarrationContextStub(
        NarrationEvidence(
            ref="evt-committed",
            kind="rule_presentation",
            subject_id="committed-result",
            subject_name="已提交的结果",
            description="已提交的结果仍然有效。",
        ),
        "resolved",
    )
    context.plot_threads = (
        NarrationPlotThread(
            thread_id="stale-thread",
            status="resolved",
            player_safe_summary="不能继续使用的旧摘要。",
        ),
    )

    result = await application._narrate(cast(NarrationContext, context))

    assert result == expected
    assert narrate.await_args is not None
    assert narrate.await_args.args[0].plot_threads == ()


@pytest.mark.asyncio
async def test_resume_owned_recovers_intent_after_crash_before_engine_write() -> None:
    run = _run(cancel_id="cancel-original")
    engine = _Engine(_status("awaiting_post_roll_decision"))
    orchestrator = _Orchestrator(run)
    application = _application(run, engine, orchestrator)

    result = await application.resume_owned(
        room_id=run.room_id,
        player_id=run.player_id,
        parent_action_id=run.parent_action_id,
    )

    assert result == "recovered"
    assert len(engine.post_roll_requests) == 1
    request = engine.post_roll_requests[0]
    assert request.request_id == "cancel-original:accept-current"
    assert request.source_revision == "revision-7"
    assert request.check_id == "check-281"
    assert len(orchestrator.resume_calls) == 1


@pytest.mark.asyncio
async def test_cancel_retry_reconciles_resolved_engine_after_crash_before_plan_write() -> None:
    run = _run(cancel_id="cancel-original")
    engine = _Engine(_status("resolved"))
    orchestrator = _Orchestrator(run)
    application = _application(run, engine, orchestrator)

    result = await application.cancel_remaining(
        room_id=run.room_id,
        player_id=run.player_id,
        parent_action_id=run.parent_action_id,
        request_id="cancel-retry-with-new-id",
    )

    assert result == "recovered"
    assert engine.post_roll_requests == []
    assert len(orchestrator.resume_calls) == 1
    assert orchestrator.resume_calls[0]["parent_action_id"] == run.parent_action_id
