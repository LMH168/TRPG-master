"""验证目标完成条件在真实 v3 Engine 中形成跨回合权威状态。"""

from __future__ import annotations

from pathlib import Path

import pytest
from collaboration_framework.contracts import (
    AdjudicationValidationError,
    CheckDecisionRequest,
    GetAdjudicationStatusRequest,
    ModuleContentV3,
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
from collaboration_framework.engine.initialization import create_initial_game_state

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


def _runtime_store() -> InMemoryEngineStore:
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

    social = {
        "semantic_focus": {"kind": "entity", "id": "melodias"},
        "anchor_ref": None,
        "method_family": "威胁",
        "method_description": "威胁守墓人回答问题",
        "check_proposal": {"mode": "none", "candidates": []},
        "success_effect_proposals": [{"type": "narrative_only"}],
        "failure_effect_proposals": [],
        "completion": {"kind": "process", "interaction": "social"},
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
                "method_family": "射击",
                "method_description": "用手枪进行致命射击",
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
