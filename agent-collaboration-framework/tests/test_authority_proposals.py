"""验证 Proposal-only Host 契约不会携带或伪造权威提交字段。"""

from __future__ import annotations

import pytest
from collaboration_framework.contracts import (
    HostDecisionProposal,
    SingleActionProposal,
)
from pydantic import TypeAdapter, ValidationError


def _dynamic_pickup_payload() -> dict[str, object]:
    """构造可表达“创建并拾取”的无授权 Proposal。"""

    return {
        "kind": "single_action",
        "schema_version": 1,
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
        "failure_effect_proposals": [],
    }


def test_host_decision_union_accepts_ordered_dynamic_effects() -> None:
    proposal = TypeAdapter(HostDecisionProposal).validate_python(
        _dynamic_pickup_payload()
    )

    assert isinstance(proposal, SingleActionProposal)
    assert [effect.type for effect in proposal.success_effect_proposals] == [
        "ensure_runtime_entity",
        "move_entity",
    ]
    assert "persistence_intent" not in proposal.model_dump(mode="json")


@pytest.mark.parametrize(
    "trusted_field",
    [
        "room_id",
        "player_id",
        "actor_id",
        "request_id",
        "source_revision",
        "authority_level",
        "commit_status",
        "dice_result",
    ],
)
def test_single_action_proposal_rejects_trusted_submission_fields(
    trusted_field: str,
) -> None:
    payload = _dynamic_pickup_payload()
    payload[trusted_field] = "forged"

    with pytest.raises(ValidationError):
        SingleActionProposal.model_validate(payload)


def test_move_to_inventory_cannot_name_an_actor() -> None:
    payload = _dynamic_pickup_payload()
    effects = payload["success_effect_proposals"]
    assert isinstance(effects, list)
    move = effects[1]
    assert isinstance(move, dict)
    destination = move["destination"]
    assert isinstance(destination, dict)
    destination["actor_id"] = "someone-else"

    with pytest.raises(ValidationError):
        SingleActionProposal.model_validate(payload)


def test_runtime_declaration_rejects_a_canon_reference() -> None:
    payload = _dynamic_pickup_payload()
    effects = payload["success_effect_proposals"]
    assert isinstance(effects, list)
    ensure = effects[0]
    assert isinstance(ensure, dict)
    ensure["runtime_ref"] = {"kind": "entity", "id": "authored-key"}

    with pytest.raises(ValidationError, match="runtime_entity"):
        SingleActionProposal.model_validate(payload)
