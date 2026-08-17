"""验证必选规则、权威检定与规则前置条件在 Compiler 中统一绑定。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from collaboration_framework.contracts import (
    AdjudicationValidationError,
    AdvanceWorldTimeEffect,
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
                    "target_interactions": ("observe",),
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


def _runtime_at(
    scene_id: str,
    *,
    occupation: str | None = None,
    background: str = "",
) -> EngineRuntimeSnapshot:
    """切换可信地点和角色档案，用于验证通用条件而非模组文本分支。"""

    runtime = _runtime()
    actor = runtime.game_state.actors["actor-1"]
    actors = {
        **runtime.game_state.actors,
        "actor-1": actor.model_copy(
            update={
                "state": {
                    **actor.state,
                    "occupation": occupation,
                    "background": background,
                }
            },
            deep=True,
        ),
    }
    state = runtime.game_state.model_copy(
        update={"scene_id": scene_id, "actors": actors}, deep=True
    )
    return runtime.model_copy(update={"game_state": state}, deep=True)


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


def test_open_method_family_uses_structured_interaction_scope() -> None:
    """自然语言 family 不在模组词表时，仍按结构化交互绑定唯一规则。"""

    command = ProposalCompiler().compile(
        _synthetic_runtime(),
        _request(
            goal="用手边的方法处理这个装置",
            focus_id="sealed_hatch",
            family="模型自由生成的动作族",
            interaction="observe",
            check=_skill_check("mechanical-repair"),
        ),
    )

    assert command.adjudication.rule_decision == RuleDecisionRef(
        rule_id="inspect_sealed_hatch",
        option_id="diagnose",
    )
    assert command.adjudication.method.family == "inspect_device"
    assert command.adjudication.target.id == "sealed_hatch"


def test_authored_physical_rule_cannot_be_bypassed_by_free_method_family() -> None:
    """Host 使用任意中文 family 时，固定模组的物理规则仍拥有检定与结果。"""

    command = ProposalCompiler().compile(
        _runtime(slab_moved=False),
        _request(
            goal="用随手找到的办法处理入口",
            focus_id="crypt_entrance",
            family="任意未登记表达",
            interaction="physical",
        ),
    )

    assert command.adjudication.rule_decision == RuleDecisionRef(
        rule_id="move_crypt_slab",
        option_id="STR",
    )
    assert command.adjudication.method.family == "move"
    assert isinstance(command.adjudication.check, RequiredAdjudicationCheck)
    assert command.adjudication.check.candidates[0].skill_id == "STR"


def test_authored_observation_rule_accepts_open_method_family() -> None:
    """观察类规则按结构化 interaction 绑定，不依赖模型复述英文动作族。"""

    command = ProposalCompiler().compile(
        _runtime(),
        _request(
            goal="我仔细查看附近留下的痕迹，寻找它们通向哪里",
            focus_id="favorite_grave",
            family="模型生成的任意观察方式",
            interaction="observe",
            check=_skill_check("track"),
        ),
    )

    assert command.adjudication.rule_decision == RuleDecisionRef(
        rule_id="inspect_grave_area",
        option_id="track",
    )
    assert command.adjudication.method.family == "track"
    assert command.adjudication.target.id == "favorite_grave"
    assert isinstance(command.adjudication.check, RequiredAdjudicationCheck)
    assert command.adjudication.check.candidates[0].skill_id == "track"


def test_travel_effect_binds_rule_through_authored_access_point() -> None:
    """旅行目标由导航边推导入口 Rule，Host 不需要知道隐藏入口规则 ID。"""

    request = _request(
        goal="进入这个已打开的下层地点",
        focus_id="crypt",
        focus_kind="location",
        family="travel",
        interaction="physical",
    )
    enter = {
        "type": "enter_location",
        "location_ref": {"kind": "location", "id": "crypt"},
    }
    request = request.model_copy(
        update={
            "proposal": SingleActionProposal.model_validate(
                {
                    **request.proposal.to_json_dict(),
                    "success_effect_proposals": [enter],
                    "completion": {"kind": "effects", "requirements": [enter]},
                }
            )
        }
    )

    command = ProposalCompiler().compile(_runtime(), request)

    assert command.adjudication.rule_decision == RuleDecisionRef(
        rule_id="crypt_stench_on_entry",
        option_id="just_enter",
    )
    assert command.adjudication.target.kind == "entity"
    assert command.adjudication.target.id == "crypt_entrance"
    assert command.adjudication.method.family == "enter"
    assert command.adjudication.success_effects == ()


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


def test_rest_step_requires_authoritative_time_completion() -> None:
    """可信 rest 步骤同样不能用过程结果冒充已经睡到目标时刻。"""

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            _runtime(),
            _request(
                goal="休息到深夜",
                focus_id="cemetery",
                focus_kind="location",
                family="休息",
                interaction="other",
                step_kind="rest",
            ),
        )

    assert raised.value.result.code == "WAIT_REQUIRES_TIME_EFFECT"
    assert "等待或休息" in raised.value.result.player_safe_reason


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


def test_rest_step_accepts_ordered_time_effects_and_completion() -> None:
    """跨越多个离散时间点的休息目标必须完整保留有序时间 Effect。"""

    request = _request(
        goal="休息到深夜",
        focus_id="cemetery",
        focus_kind="location",
        family="休息",
        interaction="other",
        step_kind="rest",
    )
    advances = [
        {"type": "advance_world_time", "to_point_id": "hour_18"},
        {"type": "advance_world_time", "to_point_id": "hour_00"},
    ]
    request = request.model_copy(
        update={
            "proposal": SingleActionProposal.model_validate(
                {
                    **request.proposal.to_json_dict(),
                    "success_effect_proposals": advances,
                    "completion": {"kind": "effects", "requirements": [advances[-1]]},
                }
            )
        }
    )

    command = ProposalCompiler().compile(_runtime(), request)

    time_effects = command.adjudication.success_effects
    assert all(isinstance(effect, AdvanceWorldTimeEffect) for effect in time_effects)
    assert [
        effect.to_point_id
        for effect in time_effects
        if isinstance(effect, AdvanceWorldTimeEffect)
    ] == [
        "hour_18",
        "hour_00",
    ]


def test_rest_step_rejects_intermediate_points_as_final_requirements() -> None:
    """中间时间点不能同时声明为最终状态，否则目标永远只能部分完成。"""

    request = _request(
        goal="休息到深夜",
        focus_id="cemetery",
        focus_kind="location",
        family="休息",
        interaction="other",
        step_kind="rest",
    )
    advances = [
        {"type": "advance_world_time", "to_point_id": "hour_18"},
        {"type": "advance_world_time", "to_point_id": "hour_00"},
    ]
    proposal = SingleActionProposal.model_validate(
        {
            **request.proposal.to_json_dict(),
            "success_effect_proposals": advances,
            "completion": {"kind": "effects", "requirements": advances},
        }
    )

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            _runtime(),
            request.model_copy(update={"proposal": proposal}),
        )

    assert raised.value.result.code == "WAIT_REQUIRES_TIME_EFFECT"


def test_travel_step_requires_matching_location_completion() -> None:
    """远端地点不能用零 Effect 的 process 结果冒充已经到达。"""

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            _runtime(),
            _request(
                goal="回去找托马斯",
                focus_id="thomas_office",
                focus_kind="location",
                family="travel",
                interaction="other",
                step_kind="travel",
            ),
        )

    assert raised.value.result.code == "TRAVEL_REQUIRES_LOCATION_EFFECT"


def test_social_step_rejects_remote_canon_entity() -> None:
    """Keeper 可知但不在当前场景的 NPC 不能成为本轮社交目标。"""

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            _runtime(),
            _request(
                goal="向托马斯汇报发现",
                focus_id="thomas",
                family="dialogue",
                interaction="social",
                step_kind="dialogue",
            ),
        )

    assert raised.value.result.code == "SOCIAL_TARGET_NOT_PRESENT"


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


def test_multi_option_rule_uses_declared_default_without_clarification() -> None:
    """玩家未声明例外时，Engine 采用模组默认后果且不泄露隐藏选项。"""

    command = ProposalCompiler().compile(
        _runtime(),
        _request(
            goal="我想进入地穴",
            focus_id="crypt_entrance",
            family="enter",
            interaction="physical",
        ),
    )

    assert command.adjudication.rule_decision == RuleDecisionRef(
        rule_id="crypt_stench_on_entry",
        option_id="just_enter",
    )


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


def test_hidden_default_rule_is_not_projected_to_host() -> None:
    """默认后果及主动例外只供 Engine 匹配，Host 不能据此提示玩家。"""

    capabilities = keeper_capabilities_v3(_runtime(), actor_id="actor-1")

    assert "crypt_stench_on_entry" not in {
        item.rule_id for item in capabilities.rule_candidates
    }


def test_actor_profile_selects_speakeasy_route_without_check() -> None:
    """职业条件满足时使用免检 Rule，业务代码不枚举具体职业或地点。"""

    command = ProposalCompiler().compile(
        _runtime_at("arnoldsburg_streets", occupation="侦探"),
        _request(
            goal="寻找一家地下酒吧",
            focus_id="arnoldsburg_streets",
            focus_kind="location",
            family="search",
            interaction="observe",
        ),
    )

    assert command.adjudication.rule_decision == RuleDecisionRef(
        rule_id="know_speakeasy_from_profile",
        option_id="known_from_profile",
    )
    assert isinstance(command.adjudication.check, NoAdjudicationCheck)


def test_speakeasy_search_uses_rule_owned_luck_for_other_profiles() -> None:
    """普通角色寻找地下酒吧时，检定由固定 Rule 决定为幸运。"""

    command = ProposalCompiler().compile(
        _runtime_at("arnoldsburg_streets", occupation="图书管理员"),
        _request(
            goal="寻找一家地下酒吧",
            focus_id="arnoldsburg_streets",
            focus_kind="location",
            family="search",
            interaction="observe",
        ),
    )

    assert command.adjudication.rule_decision == RuleDecisionRef(
        rule_id="find_speakeasy_by_luck",
        option_id="luck",
    )
    assert isinstance(command.adjudication.check, RequiredAdjudicationCheck)
    assert command.adjudication.check.candidates[0].skill_id == "luck"


@pytest.mark.asyncio
async def test_purchasing_liquor_moves_authoritative_custody_to_actor() -> None:
    """购买模组物品必须更新 ItemInstance custody，不能只写获得标记。"""

    runtime = _runtime_at("speakeasy")
    store = InMemoryEngineStore()
    store.register_room(
        module_content=runtime.module_content,
        initial_state=runtime.game_state,
    )
    engine = AdjudicationEngineService(store)
    with engine_turn_context("turn-buy-liquor"):
        await engine.submit_proposal(
            _request(
                goal="购买一品脱酒",
                focus_id="liquor",
                family="search",
                interaction="physical",
            )
        )

    item = store.inspect_state("room-rule-binding").item_instances["liquor"]
    assert item.custody.kind == "actor_inventory"
    assert item.custody.ref_id == "actor-1"


def test_bribe_requires_liquor_in_current_actor_inventory() -> None:
    """物品仍在酒吧时不能在墓地凭空用于贿赂。"""

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            _runtime_at("cemetery"),
            _request(
                goal="用酒贿赂守墓人",
                focus_id="melodias",
                family="bribe",
                interaction="social",
            ),
        )

    assert raised.value.result.code == "RULE_PRECONDITION_UNMET"


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


def test_hidden_default_rule_never_enters_candidate_projection() -> None:
    """隐藏默认规则无论前置条件是否满足都不能进入 Host Prompt。"""

    blocked = keeper_capabilities_v3(_runtime(slab_moved=False), actor_id="actor-1")
    admitted = keeper_capabilities_v3(_runtime(slab_moved=True), actor_id="actor-1")

    assert "crypt_stench_on_entry" not in {
        candidate.rule_id for candidate in blocked.rule_candidates
    }
    assert "crypt_stench_on_entry" not in {
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
    assert agenda.status == "running"
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
async def test_default_entry_records_visit_then_wakes_at_stable_boundary() -> None:
    """默认后果必须证明曾进入地穴，并按原文在墓地醒来后恢复行动。"""

    runtime = _runtime()
    store = InMemoryEngineStore()
    store.register_room(
        module_content=runtime.module_content,
        initial_state=runtime.game_state,
    )
    engine = AdjudicationEngineService(store)
    with engine_turn_context("turn-default-entry"):
        await engine.submit_proposal(
            _request(
                goal="进入地穴",
                focus_id="crypt_entrance",
                family="enter",
                interaction="physical",
            )
        )

    executions = await RuleAgendaExecutor(store, engine=engine).drain(
        room_id="room-rule-binding",
        turn_id="turn-default-entry",
    )
    state = store.inspect_state("room-rule-binding")

    assert executions
    assert state.scene_id == "cemetery"
    assert state.entities["crypt_entrance"]["entered"] is True
    assert state.entities["cemetery_figure"]["willing_to_talk"] is True
    assert all(agenda.status == "stable" for agenda in state.rule_agendas.values())
    presentations = {
        event.payload.get("player_safe_summary")
        for event in store.inspect_domain_events("room-rule-binding")
        if event.type == "rule.presentation"
    }
    assert presentations == {
        "你越过入口，并看见了地穴中的人影；现在对方就在眼前，愿意与你交谈。"
    }


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
    assert final.scene_id == "cemetery"
    assert "unconscious" not in final.actors["actor-1"].conditions
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
    narration_evidence = [
        item
        for execution in executions
        for item in execution.result.get("narration_evidence", [])
    ]
    # 多阶段 Agenda 的具体公开结果必须按提交顺序交给 Narrator，不能只留下
    # PlotThread 摘要或最终地点，导致昏迷、醒来和被动检定在叙事中消失。
    required_evidence = [
        item for item in narration_evidence if item["required_in_narration"]
    ]
    assert [item["kind"] for item in required_evidence] == [
        "actor_condition",
        "world_time",
        "actor_condition",
        "location_transition",
        "npc_opportunity",
        "rule_presentation",
        "passive_check",
        "actor_resource_change",
    ]
    assert all(
        not item["required_in_narration"]
        for item in narration_evidence
        if item["kind"] == "plot_thread_transition"
    )
    evidence_text = "".join(item["description"] for item in required_evidence)
    assert "失去了意识" in evidence_text
    assert "18点" in evidence_text
    assert "恢复了意识" in evidence_text
    assert "墓地" in evidence_text
    assert "墓地中的人影" in evidence_text
    assert "理智检定" in evidence_text
    assert "理智值" in evidence_text
    assert (
        await executor.drain(
            room_id="room-rule-binding",
            turn_id="turn-enter",
        )
        == ()
    )
