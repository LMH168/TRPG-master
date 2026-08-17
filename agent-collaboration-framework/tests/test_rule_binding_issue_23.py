"""验证必选规则、权威检定与规则前置条件在 Compiler 中统一绑定。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from collaboration_framework.contracts import (
    AdjudicationValidationError,
    AgentMatchTriggerSpec,
    ModuleContentV3,
    NoAdjudicationCheck,
    RequiredAdjudicationCheck,
    RuleDecisionRef,
    SingleActionProposal,
    SubmitProposalRequest,
)
from collaboration_framework.engine import (
    ActorResources,
    ActorState,
    AdjudicationEngineService,
    EngineRuntimeSnapshot,
    InMemoryEngineStore,
    ProposalCompiler,
    RuleAgendaExecutor,
    create_initial_game_state,
    engine_turn_context,
)
from collaboration_framework.engine.projection_v3 import keeper_capabilities_v3

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "module-parser"
    / "examples"
    / "module-content-validation"
    / "追书人"
    / "module-content-v3.json"
)


def _runtime(*, slab_moved: bool = True) -> EngineRuntimeSnapshot:
    """构造墓地当前 revision，具体剧情 ID 只存在于模组级回归测试。"""

    module = ModuleContentV3.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    state = create_initial_game_state(
        module,
        room_id="room-rule-binding",
        actors={
            "actor-1": ActorState(
                player_id="player-1",
                name="调查员",
                source_character_id="character-1",
                source_character_version=1,
                resources=ActorResources(san=60),
                state={"skills": {"spot-hidden": 60, "dodge": 40, "luck": 50}},
            )
        },
    )
    entities = {
        **state.entities,
        "crypt_entrance": {
            **state.entities["crypt_entrance"],
            "slab_moved": slab_moved,
        },
    }
    state = state.model_copy(
        update={
            "scene_id": "cemetery",
            "entities": entities,
            "plot_threads": {
                **state.plot_threads,
                "crypt_entry_investigation": state.plot_threads[
                    "crypt_entry_investigation"
                ].model_copy(
                    update={"status": "in_progress" if slab_moved else "available"}
                ),
            },
        },
        deep=True,
    )
    return EngineRuntimeSnapshot(
        module_id=module.module_id,
        module_version=module.version,
        module_content=module,
        game_state=state,
        revision=str(state.event_sequence),
    )


def _synthetic_runtime() -> EngineRuntimeSnapshot:
    """替换规则、目标、分支和技能 ID，证明生产实现不依赖示例文本。"""

    runtime = _runtime()
    module = runtime.v3
    source_rule = next(rule for rule in module.rules if rule.id == "observe_caretaker")
    assert isinstance(source_rule.trigger, AgentMatchTriggerSpec)
    source_entity = next(
        entity for entity in module.entities if entity.id == "crypt_entrance"
    )
    option = source_rule.trigger.options[0].model_copy(
        update={"id": "diagnose", "semantic_hints": ("诊断机关",)}, deep=True
    )
    trigger = source_rule.trigger.model_copy(
        update={
            "scope": source_rule.trigger.scope.model_copy(
                update={
                    "action_families": ("inspect_device",),
                    "target_ids": ("sealed_hatch",),
                },
                deep=True,
            ),
            "options": (option,),
        },
        deep=True,
    )
    branch = source_rule.execution.branches[0].model_copy(
        update={"id": "diagnose"}, deep=True
    )
    steps = tuple(
        step.model_copy(
            update={
                "check": step.check.model_copy(
                    update={"parameters": {"skill_id": "mechanical-repair"}},
                    deep=True,
                )
            },
            deep=True,
        )
        if step.kind == "check"
        else step
        for step in source_rule.execution.steps
    )
    rule = source_rule.model_copy(
        update={
            "id": "inspect_sealed_hatch",
            "trigger": trigger,
            "execution": source_rule.execution.model_copy(
                update={"branches": (branch,), "steps": steps}, deep=True
            ),
        },
        deep=True,
    )
    entity = source_entity.model_copy(
        update={"id": "sealed_hatch", "name": "密封舱门"}, deep=True
    )
    module = module.model_copy(
        update={"rules": (rule,), "entities": (*module.entities, entity)}, deep=True
    )
    state = runtime.game_state.model_copy(
        update={
            "entities": {
                **runtime.game_state.entities,
                "sealed_hatch": {"sealed": True},
            }
        },
        deep=True,
    )
    return runtime.model_copy(
        update={"module_content": module, "game_state": state}, deep=True
    )


def _request(
    *,
    goal: str,
    focus_id: str,
    family: str,
    interaction: str,
    focus_kind: str = "entity",
    check: dict[str, object] | None = None,
    rule_ref: RuleDecisionRef | None = None,
    step_kind: Literal["travel", "wait", "rest", "action", "dialogue"] | None = None,
) -> SubmitProposalRequest:
    """构造只携带语义的 Host Proposal；可信身份始终由请求信封绑定。"""

    proposal = SingleActionProposal.model_validate(
        {
            "kind": "single_action",
            "schema_version": 2,
            "semantic_goal": goal,
            "semantic_focus": {"kind": focus_kind, "id": focus_id},
            "target_interaction": interaction,
            "method_family": family,
            "method_description": goal,
            "execution_means": {"kind": "intrinsic"},
            "check_proposal": check or {"mode": "none", "candidates": []},
            "rule_ref": rule_ref,
            "success_effect_proposals": [],
            "failure_effect_proposals": [],
            "completion": {"kind": "process", "interaction": interaction},
        }
    )
    return SubmitProposalRequest(
        request_id="request-1",
        room_id="room-rule-binding",
        player_id="player-1",
        actor_id="actor-1",
        source_revision="0",
        proposal=proposal,
        requested_goal=goal,
        requested_step_kind=step_kind,
    )


def _skill_check(skill_id: str) -> dict[str, object]:
    """candidate ID 故意不依赖 Rule option，验证 Engine 会规范化身份。"""

    return {
        "mode": "required",
        "candidates": [
            {
                "candidate_id": "host-candidate",
                "skill_id": skill_id,
                "difficulty": "regular",
                "method_summary": "执行玩家行动",
                "player_safe_reason": "Host 提议的检定",
            }
        ],
    }


def test_unique_required_rule_is_bound_and_owns_active_check() -> None:
    """Host 漏写 rule_ref 时，唯一必选 Rule 仍决定技能与 candidate 身份。"""

    command = ProposalCompiler().compile(
        _runtime(),
        _request(
            goal="侦察守墓人",
            focus_id="melodias",
            family="observe",
            interaction="observe",
            check=_skill_check("spot-hidden"),
        ),
    )

    assert command.adjudication.rule_decision == RuleDecisionRef(
        rule_id="observe_caretaker",
        option_id="spot-hidden",
    )
    assert isinstance(command.adjudication.check, RequiredAdjudicationCheck)
    assert command.adjudication.check.candidates[0].candidate_id == "spot-hidden"
    assert command.adjudication.check.candidates[0].skill_id == "spot-hidden"


def test_explicit_rule_ref_cannot_bypass_required_rule_ambiguity() -> None:
    """Host 主动选择其中一条 Rule，也不能替 Engine 消解多个必选规则。"""

    runtime = _runtime()
    source = next(rule for rule in runtime.v3.rules if rule.id == "observe_caretaker")
    assert isinstance(source.trigger, AgentMatchTriggerSpec)
    duplicate = source.model_copy(
        update={"id": "observe_caretaker_duplicate"}, deep=True
    )
    module = runtime.v3.model_copy(
        update={"rules": (*runtime.v3.rules, duplicate)}, deep=True
    )
    runtime = runtime.model_copy(update={"module_content": module}, deep=True)

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            runtime,
            _request(
                goal="侦察守墓人",
                focus_id="melodias",
                family="observe",
                interaction="observe",
                rule_ref=RuleDecisionRef(
                    rule_id=source.id,
                    option_id=source.trigger.options[0].id,
                ),
            ),
        )

    assert raised.value.result.code == "RULE_SELECTION_AMBIGUOUS"


def test_wait_step_requires_authoritative_time_completion() -> None:
    """可信 wait 步骤不能用纯过程或 narrative_only 冒充时间已经流逝。"""

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            _runtime(),
            _request(
                goal="等待到下一个时间点",
                focus_id="cemetery",
                focus_kind="location",
                family="任意开放动作族",
                interaction="other",
                step_kind="wait",
            ),
        )

    assert raised.value.result.code == "WAIT_REQUIRES_TIME_EFFECT"


def test_wait_step_accepts_matching_time_effect_and_completion() -> None:
    """等待门禁只要求结构化时间结果，不识别具体自然语言或模组地点。"""

    request = _request(
        goal="执行模组作者定义的时间等待目标",
        focus_id="cemetery",
        focus_kind="location",
        family="任意开放动作族",
        interaction="other",
        step_kind="wait",
    )
    advance = {"type": "advance_world_time", "to_point_id": "hour_18"}
    request = request.model_copy(
        update={
            "proposal": SingleActionProposal.model_validate(
                {
                    **request.proposal.to_json_dict(),
                    "success_effect_proposals": [advance],
                    "completion": {"kind": "effects", "requirements": [advance]},
                }
            )
        }
    )

    command = ProposalCompiler().compile(_runtime(), request)

    assert command.adjudication.success_effects[0].type == "advance_world_time"


def test_auto_bound_rule_may_supply_omitted_active_check() -> None:
    """Host 不知道 Rule 内部技能时可以省略，Engine 仍从 CheckStep 生成检定。"""

    command = ProposalCompiler().compile(
        _runtime(),
        _request(
            goal="侦察守墓人",
            focus_id="melodias",
            family="observe",
            interaction="observe",
        ),
    )

    assert command.adjudication.rule_decision == RuleDecisionRef(
        rule_id="observe_caretaker",
        option_id="spot-hidden",
    )
    assert isinstance(command.adjudication.check, RequiredAdjudicationCheck)
    assert command.adjudication.check.candidates[0].skill_id == "spot-hidden"


def test_host_cannot_replace_rule_owned_skill() -> None:
    """冲突技能只能触发可修复反馈，不能改变固定 ModuleVersion。"""

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            _runtime(),
            _request(
                goal="侦察守墓人",
                focus_id="melodias",
                family="observe",
                interaction="observe",
                check=_skill_check("dodge"),
            ),
        )

    assert raised.value.result.code == "RULE_CHECK_MISMATCH"


def test_synthetic_module_uses_same_required_rule_binding() -> None:
    """任意规则与技能标识都走结构化匹配，不需要业务代码增加词表。"""

    command = ProposalCompiler().compile(
        _synthetic_runtime(),
        _request(
            goal="诊断机关",
            focus_id="sealed_hatch",
            family="inspect_device",
            interaction="observe",
            check=_skill_check("mechanical-repair"),
        ),
    )

    assert command.adjudication.rule_decision == RuleDecisionRef(
        rule_id="inspect_sealed_hatch",
        option_id="diagnose",
    )
    assert isinstance(command.adjudication.check, RequiredAdjudicationCheck)
    assert command.adjudication.check.candidates[0].skill_id == "mechanical-repair"


def test_multi_option_rule_requires_explicit_semantic_evidence() -> None:
    """多个规则分支不能被投影成技能，也不能由 Engine 猜选。"""

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            _runtime(),
            _request(
                goal="我想进入地穴",
                focus_id="crypt_entrance",
                family="enter",
                interaction="physical",
            ),
        )

    assert raised.value.result.code == "RULE_OPTION_REQUIRED"


@pytest.mark.parametrize(
    ("goal", "option_id"),
    (("屏住呼吸进入地穴", "hold_breath"), ("直接进去", "just_enter")),
)
def test_multi_option_rule_uses_author_hints_without_inventing_check(
    goal: str,
    option_id: str,
) -> None:
    """作者语义提示只选择分支；无 CheckStep 的分支固定为不检定。"""

    command = ProposalCompiler().compile(
        _runtime(),
        _request(
            goal=goal,
            focus_id="crypt_entrance",
            family="enter",
            interaction="physical",
        ),
    )

    assert command.adjudication.rule_decision == RuleDecisionRef(
        rule_id="crypt_stench_on_entry",
        option_id=option_id,
    )
    assert isinstance(command.adjudication.check, NoAdjudicationCheck)


def test_rule_when_blocks_projection_equivalent_manual_submission() -> None:
    """前置条件不满足时，即使 Host 手工提交已知 Rule ID 也无法绕过。"""

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            _runtime(slab_moved=False),
            _request(
                goal="屏住呼吸进入地穴",
                focus_id="crypt_entrance",
                family="enter",
                interaction="physical",
                rule_ref=RuleDecisionRef(
                    rule_id="crypt_stench_on_entry",
                    option_id="hold_breath",
                ),
            ),
        )

    assert raised.value.result.code == "RULE_PRECONDITION_UNMET"


def test_unmet_required_rule_cannot_fall_back_to_process_success() -> None:
    """Host 省略隐藏候选时仍由 Engine 拒绝，不能写入普通成功事件。"""

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            _runtime(slab_moved=False),
            _request(
                goal="我想进入地穴",
                focus_id="crypt_entrance",
                family="enter",
                interaction="physical",
            ),
        )

    assert raised.value.result.code == "RULE_PRECONDITION_UNMET"


def test_rule_when_uses_same_state_for_candidate_projection() -> None:
    """未满足前置条件的规则不能只从 Prompt 隐藏一半，投影与提交必须一致。"""

    blocked = keeper_capabilities_v3(_runtime(slab_moved=False), actor_id="actor-1")
    admitted = keeper_capabilities_v3(_runtime(slab_moved=True), actor_id="actor-1")

    assert "crypt_stench_on_entry" not in {
        candidate.rule_id for candidate in blocked.rule_candidates
    }
    assert "crypt_stench_on_entry" in {
        candidate.rule_id for candidate in admitted.rule_candidates
    }


def test_check_without_external_goal_is_rejected_before_execution() -> None:
    """只有自我焦点和技能名的空检定不能形成可授权结果。"""

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            _runtime(),
            _request(
                goal="进行一次幸运检定",
                focus_id="actor-1",
                family="other",
                interaction="other",
                focus_kind="actor",
                check=_skill_check("luck"),
            ),
        )

    assert raised.value.result.code == "CHECK_GOAL_REQUIRED"


@pytest.mark.asyncio
async def test_hold_breath_entry_commits_once_then_runs_first_sight_agenda() -> None:
    """屏息不制造虚假主动检定，但首次见到食尸鬼仍只触发一次被动 SAN。"""

    runtime = _runtime()
    store = InMemoryEngineStore()
    store.register_room(
        module_content=runtime.module_content,
        initial_state=runtime.game_state,
    )
    engine = AdjudicationEngineService(store)
    with engine_turn_context("turn-safe-entry"):
        execution = await engine.submit_proposal(
            _request(
                goal="屏住呼吸进入地穴",
                focus_id="crypt_entrance",
                family="enter",
                interaction="physical",
            )
        )

    final = store.inspect_state("room-rule-binding")
    assert execution.status == "resolved"
    assert final.scene_id == "crypt"
    assert final.entities["crypt_entrance"]["entered"] is True
    agenda = next(iter(final.rule_agendas.values()))
    assert agenda.status == "awaiting_passive_check"
    executions = await RuleAgendaExecutor(store, engine=engine).drain(
        room_id="room-rule-binding",
        turn_id="turn-safe-entry",
    )
    assert next(
        iter(store.inspect_state("room-rule-binding").rule_agendas.values())
    ).status == ("stable")
    assert sum(item.execution_kind == "passive_check" for item in executions) == 1
    events = store.inspect_domain_events("room-rule-binding")
    assert sum(event.type == "travel.resolved" for event in events) == 1
    assert not any(event.type == "check.resolved" for event in events)


@pytest.mark.asyncio
async def test_direct_entry_reaches_awake_stable_boundary_once() -> None:
    """直接进入必须在同一 Turn 完成进入、昏迷、时间推进、醒来和安全证据。"""

    runtime = _runtime()
    store = InMemoryEngineStore()
    store.register_room(
        module_content=runtime.module_content,
        initial_state=runtime.game_state,
    )
    engine = AdjudicationEngineService(store)
    with engine_turn_context("turn-enter"):
        await engine.submit_proposal(
            _request(
                goal="直接进去",
                focus_id="crypt_entrance",
                family="enter",
                interaction="physical",
            )
        )

    after_action = store.inspect_state("room-rule-binding")
    assert after_action.scene_id == "crypt"
    agenda = next(iter(after_action.rule_agendas.values()))
    assert agenda.status == "running"

    executor = RuleAgendaExecutor(store, engine=engine)
    executions = await executor.drain(
        room_id="room-rule-binding",
        turn_id="turn-enter",
    )

    final = store.inspect_state("room-rule-binding")
    assert final.scene_id == "crypt"
    assert "unconscious_until_night" not in final.actors["actor-1"].conditions
    assert final.world_time.current.hour_of_day == 18
    assert final.entities["cemetery_figure"]["sighted"] is True
    assert final.entities["cemetery_figure"]["willing_to_talk"] is True
    assert next(iter(final.rule_agendas.values())).status == "stable"
    assert {item.execution_kind for item in executions} == {
        "effect_segment",
        "passive_check",
        "ruleset_action",
        "npc_opportunity",
        "presentation",
    }
    events = store.inspect_domain_events("room-rule-binding")
    assert sum(event.type == "actor.condition_applied" for event in events) == 1
    assert sum(event.type == "actor.condition_expired" for event in events) == 1
    assert sum(event.type == "rule.presentation" for event in events) == 1
    assert (
        await executor.drain(
            room_id="room-rule-binding",
            turn_id="turn-enter",
        )
        == ()
    )
