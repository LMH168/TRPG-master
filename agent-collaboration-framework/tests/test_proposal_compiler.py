"""验证 Proposal 编译器只在可信上下文中绑定身份、引用与内部命令。"""

from __future__ import annotations

from pathlib import Path

import pytest
from collaboration_framework.contracts import (
    AdjudicationValidationError,
    ItemComponent,
    ItemCustody,
    ItemDisplay,
    ItemInstance,
    ModuleContent,
    MoveEntityEffect,
    SingleActionProposal,
    SubmitProposalRequest,
)
from collaboration_framework.engine import (
    AdjudicationEngineService,
    EngineRuntimeSnapshot,
    GameState,
    InMemoryEngineStore,
    ProposalCompiler,
    ProposalShadowCompiler,
)

ROOT = Path(__file__).resolve().parents[1]


def _dynamic_pickup_payload() -> dict[str, object]:
    """构造覆盖有序创建和拾取的 Proposal。"""

    return {
        "kind": "single_action",
        "semantic_goal": "拾起桌边发现的黄铜钥匙",
        "semantic_focus": {"kind": "runtime_entity", "id": "brass-key"},
        "anchor_ref": {"kind": "location", "id": "study"},
        "method_family": "pick_up",
        "method_description": "拾起黄铜钥匙",
        "check_proposal": {"mode": "none", "candidates": []},
        "success_effect_proposals": [
            {
                "type": "ensure_runtime_entity",
                "runtime_ref": {"kind": "runtime_entity", "id": "brass-key"},
                "entity_kind": "object",
                "name": "黄铜钥匙",
                "location_ref": {"kind": "location", "id": "study"},
            },
            {
                "type": "move_entity",
                "entity_ref": {"kind": "runtime_entity", "id": "brass-key"},
                "destination": {"kind": "self_inventory"},
            },
        ],
    }


def _runtime() -> EngineRuntimeSnapshot:
    """加载稳定 fixture，供身份、revision 和引用测试使用。"""

    module = ModuleContent.model_validate_json(
        (ROOT / "fixtures/demo-module.json").read_text(encoding="utf-8")
    )
    state = GameState.model_validate_json(
        (ROOT / "fixtures/demo-state.json").read_text(encoding="utf-8")
    )
    return EngineRuntimeSnapshot(
        module_id=module.module_id,
        module_version=module.version,
        module_content=module,
        game_state=state,
        revision="0",
    )


def _runtime_with_handgun(*, location_id: str) -> EngineRuntimeSnapshot:
    """在指定地点放置一把已知手枪，用于验证跨地点 custody 门禁。"""

    runtime = _runtime()
    handgun = ItemInstance(
        id="handgun",
        room_id=runtime.game_state.room_id,
        origin="runtime",
        definition_id="handgun",
        display=ItemDisplay(name="手枪"),
        item_component=ItemComponent(),
        custody=ItemCustody(kind="location", ref_id=location_id, form="placed"),
        created_event_id="seed-handgun",
        last_event_id="seed-handgun",
        updated_revision="0",
    )
    state = runtime.game_state.model_copy(
        update={"item_instances": {handgun.id: handgun}},
        deep=True,
    )
    return runtime.model_copy(update={"game_state": state}, deep=True)


def _submission(**updates: object) -> SubmitProposalRequest:
    """构造由 Coordinator 提供可信字段的提交信封。"""

    values: dict[str, object] = {
        "request_id": "turn-10-action-1",
        "room_id": "room_01",
        "player_id": "player_01",
        "actor_id": "pc_1",
        "source_revision": "0",
        "proposal": SingleActionProposal.model_validate(_dynamic_pickup_payload()),
    }
    values.update(updates)
    return SubmitProposalRequest.model_validate(values)


def _v2_pickup_submission(**updates: object) -> SubmitProposalRequest:
    """构造不依赖 method_family 词表的 v2 拾取请求。"""

    payload = _dynamic_pickup_payload()
    payload["schema_version"] = 2
    payload["method_family"] = "拾取临时物品"
    effects = payload["success_effect_proposals"]
    assert isinstance(effects, list)
    payload["completion"] = {"kind": "effects", "requirements": [effects[1]]}
    values: dict[str, object] = {
        "proposal": SingleActionProposal.model_validate(payload),
        "requested_goal": payload["semantic_goal"],
    }
    values.update(updates)
    return _submission(**values)


def _existing_handgun_pickup_submission(**updates: object) -> SubmitProposalRequest:
    """构造尝试把既有手枪放回本人库存的 Proposal v2。"""

    effect = {
        "type": "move_entity",
        "entity_ref": {"kind": "entity", "id": "handgun"},
        "destination": {"kind": "self_inventory"},
    }
    goal = "掏出手枪"
    payload = {
        "kind": "single_action",
        "schema_version": 2,
        "semantic_goal": goal,
        "semantic_focus": {"kind": "entity", "id": "handgun"},
        "method_family": "physical",
        "method_description": "从随身装备中取出手枪",
        "check_proposal": {"mode": "none", "candidates": []},
        "success_effect_proposals": [effect],
        "failure_effect_proposals": [],
        "completion": {"kind": "effects", "requirements": [effect]},
    }
    values: dict[str, object] = {
        "proposal": SingleActionProposal.model_validate(payload),
        "requested_goal": goal,
    }
    values.update(updates)
    return _submission(**values)


def test_v2_compiles_open_method_family_from_declared_completion() -> None:
    """开放中文动作方式不会绕过或阻断结构化持久结果。"""

    command = ProposalCompiler().compile(_runtime(), _v2_pickup_submission())

    assert command.schema_version == 2
    assert command.completion_mode == "effects"
    assert command.adjudication.persistence_intent == "inventory"
    assert command.completion_requirements[0].type == "move_entity"


def test_v2_accepts_pickup_only_when_item_is_at_current_location() -> None:
    """当前位置的 loose item 可以进入背包，不能误伤正常拾取。"""

    command = ProposalCompiler().compile(
        _runtime_with_handgun(location_id="study"),
        _existing_handgun_pickup_submission(),
    )

    effect = command.adjudication.success_effects[0]
    assert isinstance(effect, MoveEntityEffect)
    assert effect.holder_actor_id == "pc_1"


def test_v2_rejects_pickup_from_another_location() -> None:
    """玩家离开丢枪地点后，模型知道物品 ID 也不能隔空取回。"""

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            _runtime_with_handgun(location_id="cemetery"),
            _existing_handgun_pickup_submission(),
        )

    assert raised.value.result.code == "ITEM_NOT_AT_CURRENT_LOCATION"
    assert raised.value.result.repairability == "requires_player_choice"


@pytest.mark.asyncio
async def test_submit_proposal_rejects_remote_pickup_without_side_effects() -> None:
    """Engine 事务拒绝远程拾取后不得留下事件或改变手枪 custody。"""

    runtime = _runtime_with_handgun(location_id="cemetery")
    store = InMemoryEngineStore()
    store.register_room(
        module_content=runtime.module_content,
        initial_state=runtime.game_state,
    )

    with pytest.raises(AdjudicationValidationError) as raised:
        await AdjudicationEngineService(store).submit_proposal(
            _existing_handgun_pickup_submission()
        )

    assert raised.value.result.code == "ITEM_NOT_AT_CURRENT_LOCATION"
    assert store.inspect_domain_events("room_01") == ()
    handgun = store.inspect_state("room_01").item_instances["handgun"]
    assert handgun.custody.kind == "location"
    assert handgun.custody.ref_id == "cemetery"
    assert handgun.version == 1


def test_v2_rejects_model_narrowing_the_trusted_goal() -> None:
    """模型不能把玩家的完整目标静默缩减成较弱的动作。"""

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            _runtime(),
            _v2_pickup_submission(requested_goal="拾起钥匙并把它交给同伴"),
        )

    assert raised.value.result.code == "PROPOSAL_SEMANTIC_GOAL_CHANGED"


def test_compiler_binds_trusted_actor_and_resolves_ordered_runtime_refs() -> None:
    command = ProposalCompiler().compile(_runtime(), _submission())

    ensure, move = command.adjudication.success_effects
    assert ensure.type == "ensure_runtime_entity"
    assert move.type == "move_entity"
    assert ensure.entity_id == move.entity_id
    assert move.holder_actor_id == "pc_1"
    assert command.adjudication.target.id == "study"
    assert command.adjudication.persistence_intent == "inventory"
    assert command.validation.status == "accepted"


def test_compiler_derives_stable_runtime_ids_for_replay() -> None:
    compiler = ProposalCompiler()

    first = compiler.compile(_runtime(), _submission())
    second = compiler.compile(_runtime(), _submission())

    assert first == second
    assert first.proposal_fingerprint == second.proposal_fingerprint


def test_compiler_rejects_stale_trusted_revision() -> None:
    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(_runtime(), _submission(source_revision="9"))

    assert raised.value.result.code == "SOURCE_REVISION_STALE"
    assert raised.value.result.repairability == "retry_with_latest_revision"


def test_compiler_rejects_rule_owned_effect_override() -> None:
    payload = _dynamic_pickup_payload()
    payload["rule_ref"] = {"rule_id": "rule-1", "option_id": "option-1"}
    request = _submission(proposal=SingleActionProposal.model_validate(payload))

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(_runtime(), request)

    assert raised.value.result.code == "RULE_EFFECT_OVERRIDE"


def test_compiler_rejects_runtime_ref_used_before_declaration() -> None:
    payload = _dynamic_pickup_payload()
    effects = payload["success_effect_proposals"]
    assert isinstance(effects, list)
    effects.reverse()
    request = _submission(proposal=SingleActionProposal.model_validate(payload))

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(_runtime(), request)

    assert raised.value.result.code == "RUNTIME_REF_UNDECLARED"


def test_compiler_rejects_empty_effect_without_rule_authority() -> None:
    """没有规则托管时，空 Effect 不能伪装成已完成动作。"""

    payload = _dynamic_pickup_payload()
    payload.update(
        {
            "semantic_goal": "开枪打死守墓人",
            "semantic_focus": {"kind": "entity", "id": "butler"},
            "anchor_ref": None,
            "method_family": "combat",
            "method_description": "开枪攻击守墓人",
            "success_effect_proposals": [],
            "failure_effect_proposals": [],
        }
    )

    with pytest.raises(AdjudicationValidationError) as raised:
        ProposalCompiler().compile(
            _runtime(),
            _submission(proposal=SingleActionProposal.model_validate(payload)),
        )

    assert raised.value.result.code == "PERSISTENT_EFFECT_REQUIRED"


def test_compiler_accepts_explicit_death_state_effect() -> None:
    """杀死 NPC 必须编译为可回放的 consciousness=dead 状态事件。"""

    payload = _dynamic_pickup_payload()
    payload.update(
        {
            "semantic_goal": "开枪打死守墓人",
            "semantic_focus": {"kind": "entity", "id": "butler"},
            "anchor_ref": None,
            "method_family": "combat",
            "method_description": "开枪攻击守墓人",
            "success_effect_proposals": [
                {
                    "type": "change_entity_state",
                    "entity_ref": {"kind": "entity", "id": "butler"},
                    "key": "consciousness",
                    "value": "dead",
                }
            ],
            "failure_effect_proposals": [],
        }
    )
    command = ProposalCompiler().compile(
        _runtime(),
        _submission(proposal=SingleActionProposal.model_validate(payload)),
    )

    effect = command.adjudication.success_effects[0]
    assert effect.type == "change_entity_state"
    assert effect.entity_id == "butler"
    assert effect.key == "consciousness"
    assert effect.value == "dead"


def test_shadow_compiler_only_reports_structural_differences() -> None:
    command = ProposalCompiler().compile(_runtime(), _submission())

    comparison = ProposalShadowCompiler().compare(
        _runtime(),
        _submission(),
        command.adjudication.model_copy(update={"summary": "旧链路摘要"}),
    )

    assert comparison.matches is False
    assert comparison.differing_fields == ("summary",)
    assert len(comparison.proposal_fingerprint) == 64


@pytest.mark.asyncio
async def test_submit_proposal_compiles_and_commits_inside_engine_boundary() -> None:
    runtime = _runtime()
    store = InMemoryEngineStore()
    store.register_room(
        module_content=runtime.module_content,
        initial_state=runtime.game_state,
    )
    payload = _dynamic_pickup_payload()
    payload.update(
        {
            "semantic_goal": "向书房里的人点头致意",
            "semantic_focus": {"kind": "location", "id": "study"},
            "anchor_ref": None,
            "method_family": "social_gesture",
            "method_description": "点头致意",
            "success_effect_proposals": [{"type": "narrative_only"}],
        }
    )
    request = _submission(proposal=SingleActionProposal.model_validate(payload))

    execution = await AdjudicationEngineService(store).submit_proposal(request)

    assert execution.status == "resolved"
    assert len(store.inspect_domain_events("room_01")) == 1


@pytest.mark.asyncio
async def test_submit_proposal_persists_npc_death_state() -> None:
    """死亡 Proposal 必须同时改变权威状态并留下可回放事件。"""

    runtime = _runtime()
    store = InMemoryEngineStore()
    store.register_room(
        module_content=runtime.module_content,
        initial_state=runtime.game_state,
    )
    payload = _dynamic_pickup_payload()
    payload.update(
        {
            "semantic_goal": "开枪打死守墓人",
            "semantic_focus": {"kind": "entity", "id": "butler"},
            "anchor_ref": None,
            "method_family": "combat",
            "method_description": "开枪攻击守墓人",
            "success_effect_proposals": [
                {
                    "type": "change_entity_state",
                    "entity_ref": {"kind": "entity", "id": "butler"},
                    "key": "consciousness",
                    "value": "dead",
                }
            ],
            "failure_effect_proposals": [],
        }
    )

    execution = await AdjudicationEngineService(store).submit_proposal(
        _submission(proposal=SingleActionProposal.model_validate(payload))
    )

    assert execution.status == "resolved"
    state = store.inspect_state("room_01")
    assert state.entities["butler"]["consciousness"] == "dead"
    events = store.inspect_domain_events("room_01")
    assert any(
        event.type == "entity.state_changed"
        and event.payload
        == {
            "entity_id": "butler",
            "key": "consciousness",
            "value": "dead",
        }
        for event in events
    )


@pytest.mark.asyncio
async def test_submit_proposal_rejects_stale_revision_without_side_effects() -> None:
    runtime = _runtime()
    store = InMemoryEngineStore()
    store.register_room(
        module_content=runtime.module_content,
        initial_state=runtime.game_state,
    )

    with pytest.raises(AdjudicationValidationError):
        await AdjudicationEngineService(store).submit_proposal(
            _submission(source_revision="9")
        )

    assert store.inspect_domain_events("room_01") == ()
