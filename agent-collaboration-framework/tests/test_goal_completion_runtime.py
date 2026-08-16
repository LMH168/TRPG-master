"""验证目标完成条件在真实 v3 Engine 中形成跨回合权威状态。"""

from __future__ import annotations

from pathlib import Path

import pytest

from collaboration_framework.contracts import (
    AdjudicationValidationError,
    CheckDecisionRequest,
    ContractError,
    GetAdjudicationStatusRequest,
    ItemCustody,
    ModuleContentV3,
    NarrativeOnlyEffect,
    PlayerViewScope,
    PostRollDecisionRequest,
    SelectCheckChoice,
    SingleActionProposal,
    SubmitProposalRequest,
)
from collaboration_framework.engine import (
    ActorState,
    AdjudicationEngineService,
    DiceRoller,
    InMemoryEngineStore,
    RuleEngineService,
    SequenceDiceSource,
)
from collaboration_framework.engine.adjudication import GoalCompletionEvaluationError
from collaboration_framework.engine.initialization import create_initial_game_state
from collaboration_framework.engine.timeline import advance_to_target

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT
    / "docs"
    / "module-parser"
    / "examples"
    / "module-content-validation"
    / "追书人"
    / "module-content-v3.json"
)
ROOM = "goal-runtime-room"
PLAYER = "goal-runtime-player"
ACTOR = "goal-runtime-actor"
HANDGUN = f"{ACTOR}:equipment:0"


def _runtime_store(*, handgun_location_id: str | None = None) -> InMemoryEngineStore:
    """加载真实模组，并给调查员准备射击技能与一把可追踪手枪。"""

    module = ModuleContentV3.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    actor = ActorState(
        player_id=PLAYER,
        name="调查员",
        source_character_id="character-goal-runtime",
        source_character_version=1,
        state={
            "skills": {"firearm-handgun": 80},
            "skill_labels": {"firearm-handgun": "射击：手枪"},
            "equipment": ["手枪"],
        },
    )
    state = create_initial_game_state(module, room_id=ROOM, actors={ACTOR: actor})
    state = state.model_copy(update={"scene_id": "cemetery"}, deep=True)
    if handgun_location_id is not None:
        handgun = state.item_instances[HANDGUN].model_copy(
            update={
                "custody": ItemCustody(
                    kind="location",
                    ref_id=handgun_location_id,
                    form="placed",
                )
            },
            deep=True,
        )
        state = state.model_copy(
            update={"item_instances": {**state.item_instances, HANDGUN: handgun}},
            deep=True,
        )
    store = InMemoryEngineStore()
    store.register_room(module_content=module, initial_state=state)
    return store


def _rule_owned_hit_store() -> InMemoryEngineStore:
    """追加一条只证明命中、不授权死亡的规则，用于核对目标完成边界。"""

    module = ModuleContentV3.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    payload = module.model_dump(mode="json")
    rules = payload["rules"]
    assert isinstance(rules, list)
    rules.append(
        {
            "id": "test_rule_owned_hit",
            "trigger": {
                "kind": "agent_match",
                "scope": {
                    "action_families": ["射击"],
                    "location_ids": ["cemetery"],
                    "target_kinds": ["entity"],
                    "target_ids": ["melodias"],
                },
                "question": {"kind": "method", "semantic_hints": ["射击"]},
                "options": [{"id": "lethal-shot", "semantic_hints": ["致命射击"]}],
            },
            "execution": {
                "branches": [{"id": "lethal-shot", "entry_step_id": "check-shot"}],
                "steps": [
                    {"id": "finish", "kind": "finish"},
                    {
                        "id": "hit",
                        "kind": "effect",
                        "effect": {
                            "type": "change_entity_state",
                            "entity_id": "melodias",
                            "key": "posture",
                            "value": "prone",
                        },
                        "next_step_id": "finish",
                    },
                    {
                        "id": "check-shot",
                        "kind": "adjudicated_check",
                        "adjudication_ref": "current",
                        "effect_authority": "rule",
                        "result_routes": {
                            "critical_success": "hit",
                            "extreme_success": "hit",
                            "hard_success": "hit",
                            "regular_success": "hit",
                            "failure": "finish",
                            "fumble": "finish",
                        },
                        "cancel_step_id": "finish",
                    },
                ],
            },
        }
    )
    actor = ActorState(
        player_id=PLAYER,
        name="调查员",
        source_character_id="character-goal-rule-runtime",
        source_character_version=1,
        state={
            "skills": {"firearm-handgun": 80},
            "skill_labels": {"firearm-handgun": "射击：手枪"},
            "equipment": ["手枪"],
        },
    )
    updated_module = ModuleContentV3.model_validate(payload)
    state = create_initial_game_state(
        updated_module, room_id=ROOM, actors={ACTOR: actor}
    )
    state = state.model_copy(update={"scene_id": "cemetery"}, deep=True)
    store = InMemoryEngineStore()
    store.register_room(module_content=updated_module, initial_state=state)
    return store


def _submission(
    *,
    request_id: str,
    revision: str,
    goal: str,
    payload: dict[str, object],
) -> SubmitProposalRequest:
    """用可信目标快照包装一份 v2 Proposal。"""

    payload.setdefault("execution_means", {"kind": "intrinsic"})
    proposal = SingleActionProposal.model_validate(
        {
            "kind": "single_action",
            "schema_version": 2,
            "semantic_goal": goal,
            **payload,
        }
    )
    return SubmitProposalRequest(
        request_id=request_id,
        room_id=ROOM,
        player_id=PLAYER,
        actor_id=ACTOR,
        source_revision=revision,
        proposal=proposal,
        requested_goal=goal,
    )


@pytest.mark.asyncio
async def test_checked_ai_death_persists_and_blocks_later_social_action() -> None:
    """致命 AI 裁决必须提交一次死亡事件，后续社交不能让 NPC 重新响应。"""

    store = _runtime_store()
    engine = AdjudicationEngineService(store, dice=DiceRoller(SequenceDiceSource([5])))
    death = {
        "semantic_focus": {"kind": "entity", "id": "melodias"},
        "anchor_ref": {"kind": "entity", "id": HANDGUN},
        "method_family": "射击",
        "method_description": "用手枪进行致命射击",
        "execution_means": {
            "kind": "item",
            "item_ref": {"kind": "entity", "id": HANDGUN},
        },
        "check_proposal": {
            "mode": "required",
            "candidates": [
                {
                    "candidate_id": "firearm-handgun",
                    "skill_id": "firearm-handgun",
                    "difficulty": "regular",
                    "method_summary": "用手枪射击守墓人",
                    "player_safe_reason": "致命行动需要射击检定",
                }
            ],
        },
        "success_effect_proposals": [
            {
                "type": "change_entity_state",
                "entity_ref": {"kind": "entity", "id": "melodias"},
                "key": "consciousness",
                "value": "dead",
            }
        ],
        "failure_effect_proposals": [{"type": "narrative_only"}],
        "completion": {
            "kind": "effects",
            "requirements": [
                {
                    "type": "change_entity_state",
                    "entity_ref": {"kind": "entity", "id": "melodias"},
                    "key": "consciousness",
                    "value": "dead",
                }
            ],
        },
    }
    request = _submission(
        request_id="kill-melodias",
        revision="0",
        goal="我要打死守墓人",
        payload=death,
    )
    pending = await engine.submit_proposal(request)
    assert pending.goal_outcome == "pending"
    assert pending.pending_decision is not None
    rolled = await engine.decide(
        CheckDecisionRequest(
            request_id="kill-melodias:select",
            room_id=ROOM,
            player_id=PLAYER,
            source_revision=pending.view_revision,
            decision_id=pending.pending_decision.decision_id,
            decision_version=pending.pending_decision.decision_version,
            choice=SelectCheckChoice(candidate_id="firearm-handgun"),
        )
    )
    assert rolled.check_run is not None
    resolved = await engine.decide_post_roll(
        PostRollDecisionRequest(
            request_id="kill-melodias:accept",
            room_id=ROOM,
            player_id=PLAYER,
            source_revision=rolled.view_revision,
            check_id=rolled.check_run.check_id,
            check_version=rolled.check_run.version,
            option_id="accept-current",
        )
    )

    assert resolved.goal_outcome == "achieved"
    assert store.inspect_state(ROOM).entities["melodias"]["consciousness"] == "dead"
    death_events = [
        event
        for event in store.inspect_domain_events(ROOM)
        if event.type == "entity.state_changed"
        and event.payload.get("entity_id") == "melodias"
        and event.payload.get("value") == "dead"
    ]
    assert len(death_events) == 1

    # 重建 Service 后按原 action 对账，只能读到同一终态，不能重掷或重写死亡事件。
    recovered = await AdjudicationEngineService(store).get_status(
        GetAdjudicationStatusRequest(
            room_id=ROOM,
            player_id=PLAYER,
            action_request_id=request.request_id,
        )
    )
    assert recovered.execution is not None
    assert recovered.execution.goal_outcome == "achieved"
    assert (
        len(
            [
                event
                for event in store.inspect_domain_events(ROOM)
                if event.type == "entity.state_changed"
                and event.payload.get("entity_id") == "melodias"
                and event.payload.get("value") == "dead"
            ]
        )
        == 1
    )

    # 真实模型曾把社交规则错误包装成 effects，并把已经满足的死亡状态当作
    # completion。目标交互必须独立于 completion，因此仍要在创建检定前拒绝。
    social = {
        "semantic_focus": {"kind": "entity", "id": "melodias"},
        "anchor_ref": {"kind": "entity", "id": "melodias"},
        "target_interaction": "social",
        "method_family": "intimidate",
        "method_description": "威胁守墓人回答问题",
        "check_proposal": {
            "mode": "required",
            "candidates": [
                {
                    "candidate_id": "intimidate",
                    "skill_id": "intimidate",
                    "difficulty": "regular",
                    "method_summary": "威胁守墓人回答问题",
                    "player_safe_reason": "社交行动需要检定",
                }
            ],
        },
        "rule_ref": {
            "rule_id": "intimidate_caretaker",
            "option_id": "intimidate",
        },
        "success_effect_proposals": [],
        "failure_effect_proposals": [],
        "completion": {
            "kind": "effects",
            "requirements": [
                {
                    "type": "change_entity_state",
                    "entity_ref": {"kind": "entity", "id": "melodias"},
                    "key": "consciousness",
                    "value": "dead",
                }
            ],
        },
    }
    event_count = len(store.inspect_domain_events(ROOM))
    with pytest.raises(AdjudicationValidationError) as raised:
        await engine.submit_proposal(
            _submission(
                request_id="threaten-dead-melodias",
                revision=resolved.view_revision,
                goal="威胁守墓人",
                payload=social,
            )
        )

    assert raised.value.result.code == "TARGET_NOT_RESPONSIVE"
    assert len(store.inspect_domain_events(ROOM)) == event_count


@pytest.mark.asyncio
async def test_remote_weapon_cannot_create_check_or_death_result() -> None:
    """物品已留在别处时，玩家声明使用它也必须在 Engine 编译阶段被拒绝。"""

    store = _runtime_store(handgun_location_id="thomas_office")
    engine = AdjudicationEngineService(store, dice=DiceRoller(SequenceDiceSource([1])))
    goal = "开枪打死守墓人"
    death_effect = {
        "type": "change_entity_state",
        "entity_ref": {"kind": "entity", "id": "melodias"},
        "key": "consciousness",
        "value": "dead",
    }
    request = _submission(
        request_id="remote-handgun-kill",
        revision="0",
        goal=goal,
        payload={
            "semantic_focus": {"kind": "entity", "id": "melodias"},
            "method_family": "任意装备攻击",
            "method_description": goal,
            "execution_means": {
                "kind": "item",
                "item_ref": {"kind": "entity", "id": HANDGUN},
            },
            "check_proposal": {
                "mode": "required",
                "candidates": [
                    {
                        "candidate_id": "player-selected-skill",
                        "skill_id": "firearm-handgun",
                        "difficulty": "regular",
                        "method_summary": goal,
                        "player_safe_reason": "玩家选择当前技能",
                    }
                ],
            },
            "success_effect_proposals": [death_effect],
            "failure_effect_proposals": [{"type": "narrative_only"}],
            "completion": {"kind": "effects", "requirements": [death_effect]},
        },
    )

    with pytest.raises(AdjudicationValidationError) as raised:
        await engine.submit_proposal(request)

    assert raised.value.result.code == "ACTION_RESOURCE_NOT_HELD"
    with pytest.raises(ContractError):
        store.inspect_completed_action(ROOM, request.request_id)
    with pytest.raises(ContractError):
        store.inspect_pending_check(ROOM, request.request_id)
    assert store.inspect_domain_events(ROOM) == ()
    assert (
        store.inspect_state(ROOM).entities.get("melodias", {}).get("consciousness")
        != "dead"
    )


@pytest.mark.asyncio
async def test_rule_owned_hit_does_not_claim_death_goal() -> None:
    """规则只提交命中后果时，即使检定成功也不能把死亡目标标记为达成。"""

    store = _rule_owned_hit_store()
    engine = AdjudicationEngineService(store, dice=DiceRoller(SequenceDiceSource([5])))
    submitted = await engine.submit_proposal(
        _submission(
            request_id="rule-owned-hit",
            revision="0",
            goal="我要打死守墓人",
            payload={
                "semantic_focus": {"kind": "entity", "id": "melodias"},
                "anchor_ref": {"kind": "entity", "id": HANDGUN},
                "target_interaction": "physical",
                "method_family": "射击",
                "method_description": "用手枪进行致命射击",
                "execution_means": {
                    "kind": "item",
                    "item_ref": {"kind": "entity", "id": HANDGUN},
                },
                "check_proposal": {
                    "mode": "required",
                    "candidates": [
                        {
                            "candidate_id": "firearm-handgun",
                            "skill_id": "firearm-handgun",
                            "difficulty": "regular",
                            "method_summary": "射击守墓人",
                            "player_safe_reason": "致命行动需要射击检定",
                        }
                    ],
                },
                "rule_ref": {
                    "rule_id": "test_rule_owned_hit",
                    "option_id": "lethal-shot",
                },
                "success_effect_proposals": [],
                "failure_effect_proposals": [],
                "completion": {
                    "kind": "effects",
                    "requirements": [
                        {
                            "type": "change_entity_state",
                            "entity_ref": {"kind": "entity", "id": "melodias"},
                            "key": "consciousness",
                            "value": "dead",
                        }
                    ],
                },
            },
        )
    )
    assert submitted.pending_decision is not None
    rolled = await engine.decide(
        CheckDecisionRequest(
            request_id="rule-owned-hit:select",
            room_id=ROOM,
            player_id=PLAYER,
            source_revision=submitted.view_revision,
            decision_id=submitted.pending_decision.decision_id,
            decision_version=submitted.pending_decision.decision_version,
            choice=SelectCheckChoice(candidate_id="firearm-handgun"),
        )
    )
    assert rolled.check_run is not None
    resolved = await engine.decide_post_roll(
        PostRollDecisionRequest(
            request_id="rule-owned-hit:accept",
            room_id=ROOM,
            player_id=PLAYER,
            source_revision=rolled.view_revision,
            check_id=rolled.check_run.check_id,
            check_version=rolled.check_run.version,
            option_id="accept-current",
        )
    )

    assert resolved.outcome == "success"
    assert resolved.goal_outcome == "not_achieved"
    state = store.inspect_state(ROOM)
    assert state.entities["melodias"].get("consciousness") != "dead"
    assert state.entities["melodias"]["posture"] == "prone"
    assert all(
        not (
            event.type == "entity.state_changed"
            and event.payload.get("key") == "consciousness"
        )
        for event in store.inspect_domain_events(ROOM)
    )


@pytest.mark.asyncio
async def test_advance_world_time_satisfies_effect_completion() -> None:
    """时间推进提交后，完成条件必须读取最终时间点而不是默认失败。"""

    store = _runtime_store()
    engine = AdjudicationEngineService(store)
    request = _submission(
        request_id="advance-time",
        revision="0",
        goal="等待到晚上",
        payload={
            "semantic_focus": {"kind": "location", "id": "cemetery"},
            "target_interaction": "observe",
            "method_family": "等待",
            "method_description": "等待时间推进到晚上",
            "check_proposal": {"mode": "none"},
            "success_effect_proposals": [
                {"type": "advance_world_time", "to_point_id": "hour_18"}
            ],
            "failure_effect_proposals": [{"type": "narrative_only"}],
            "completion": {
                "kind": "effects",
                "requirements": [
                    {"type": "advance_world_time", "to_point_id": "hour_18"}
                ],
            },
        },
    )

    result = await engine.submit_proposal(request)

    assert result.outcome == "success"
    assert result.goal_outcome == "achieved"
    assert store.inspect_state(ROOM).world_time.current_point_id == "hour_18"


def test_narrative_only_cannot_silently_complete_persistent_goal() -> None:
    """叙事支撑效果误入持久完成条件时必须显式失败。"""

    state = _runtime_store().inspect_state(ROOM)
    with pytest.raises(GoalCompletionEvaluationError, match="NarrativeOnlyEffect"):
        AdjudicationEngineService._requirement_is_satisfied(
            NarrativeOnlyEffect(),
            state,
            committed_events=(),
            actor_id=ACTOR,
        )


def test_advance_world_time_preserves_intermediate_points() -> None:
    """跨夜推进必须逐点产生时间状态，不能直接跳过中间剧情触发点。"""

    module = ModuleContentV3.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    state = create_initial_game_state(module, room_id=ROOM, actors={})
    points = advance_to_target(module, state.world_time, "hour_06")

    assert [item.current_point_id for item in points] == [
        "hour_18",
        "hour_20",
        "hour_00",
        "hour_06",
    ]


@pytest.mark.asyncio
async def test_item_condition_and_drop_are_atomic_and_visible_in_next_view() -> None:
    """打空并丢弃手枪后，最终 PlayerView 必须只在场景松散物品中显示它。"""

    store = _runtime_store()
    engine = AdjudicationEngineService(store)
    condition = {
        "type": "change_item_condition",
        "entity_ref": {"kind": "entity", "id": HANDGUN},
        "condition": "empty",
    }
    drop = {
        "type": "move_entity",
        "entity_ref": {"kind": "entity", "id": HANDGUN},
        "destination": {
            "kind": "location",
            "location_ref": {"kind": "location", "id": "cemetery"},
        },
    }
    request = _submission(
        request_id="empty-and-drop-handgun",
        revision="0",
        goal="打光所有子弹然后把手枪丢下",
        payload={
            "semantic_focus": {"kind": "entity", "id": HANDGUN},
            "anchor_ref": {"kind": "location", "id": "cemetery"},
            "method_family": "清空弹药后丢弃",
            "method_description": "打空手枪并留在墓地",
            "execution_means": {
                "kind": "item",
                "item_ref": {"kind": "entity", "id": HANDGUN},
            },
            "check_proposal": {"mode": "none", "candidates": []},
            "success_effect_proposals": [condition, drop],
            "failure_effect_proposals": [],
            "completion": {
                "kind": "effects",
                "requirements": [condition, drop],
            },
        },
    )
    execution = await engine.submit_proposal(request)

    assert execution.goal_outcome == "achieved"
    item = store.inspect_state(ROOM).item_instances[HANDGUN]
    assert item.state.condition == "empty"
    assert item.custody.kind == "location"
    assert item.custody.ref_id == "cemetery"
    view = await RuleEngineService(store).read(
        PlayerViewScope(room_id=ROOM, player_id=PLAYER, actor_id=ACTOR)
    )
    assert HANDGUN not in {entry.id for entry in view.inventory}
    loose = next(entry for entry in view.scene.loose_items if entry.id == HANDGUN)
    assert loose.condition == "empty"
    assert {result.destination_kind for result in execution.committed_results} >= {
        "location"
    }

    event_count = len(store.inspect_domain_events(ROOM))
    replayed = await AdjudicationEngineService(store).submit_proposal(request)
    assert replayed == execution
    assert len(store.inspect_domain_events(ROOM)) == event_count


@pytest.mark.asyncio
async def test_companion_travel_satisfies_canon_npc_location_requirement() -> None:
    """玩家与 Canon NPC 同行抵达后，最终状态必须判定完整目标已达成。"""

    store = _runtime_store()
    # 从会客室开始，托马斯使用模组初始位置，不提前制造运行时实体。
    initial = store.inspect_state(ROOM).model_copy(update={"scene_id": "thomas_office"})
    replacement = InMemoryEngineStore()
    module = ModuleContentV3.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    replacement.register_room(module_content=module, initial_state=initial)
    engine = AdjudicationEngineService(replacement)
    enter = {
        "type": "enter_location",
        "location_ref": {"kind": "location", "id": "cemetery"},
    }
    companion = {
        "type": "move_entity",
        "entity_ref": {"kind": "entity", "id": "thomas"},
        "destination": {
            "kind": "location",
            "location_ref": {"kind": "location", "id": "cemetery"},
        },
    }
    request = _submission(
        request_id="travel-with-thomas",
        revision="0",
        goal="带着托马斯一起去墓地",
        payload={
            "semantic_focus": {"kind": "location", "id": "cemetery"},
            "method_family": "travel",
            "method_description": "与托马斯同行前往墓地",
            "check_proposal": {"mode": "none", "candidates": []},
            "success_effect_proposals": [enter, companion],
            "failure_effect_proposals": [],
            "completion": {"kind": "effects", "requirements": [enter, companion]},
        },
    )

    execution = await engine.submit_proposal(request)

    state = replacement.inspect_state(ROOM)
    assert execution.goal_outcome == "achieved"
    assert state.scene_id == "cemetery"
    assert state.entities["thomas"]["location_id"] == "cemetery"

    # 相同最终条件再次提交时必须按幂等结果完成，不重复移动 Canon NPC。
    replay_request = request.model_copy(
        update={"request_id": "travel-with-thomas-again", "source_revision": "3"},
        deep=True,
    )
    previous_events = replacement.inspect_domain_events(ROOM)
    replayed = await engine.submit_proposal(replay_request)
    assert replayed.goal_outcome == "achieved"
    new_events = replacement.inspect_domain_events(ROOM)[len(previous_events) :]
    assert "entity.moved" not in {event.type for event in new_events}
