"""验证 Proposal 编译器只在可信上下文中绑定身份、引用与内部命令。"""

from __future__ import annotations

from pathlib import Path

import pytest
from collaboration_framework.contracts import (
    AdjudicationValidationError,
    ModuleContent,
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
