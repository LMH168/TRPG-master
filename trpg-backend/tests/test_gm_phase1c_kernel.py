"""验证 Phase 1C 数据驱动 Kernel 和 CoC7 最小规则闭环。"""

import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from app.dto.gm import CommandEnvelope
from app.models.gm import GameSession, RuntimeActor
from app.models.room import Room
from app.service.gm_ai import _candidates_for, build_context_snapshot
from app.service.gm_runtime import (
    GmRuntimeError,
    _apply_runtime_action_outcome,
    _apply_scheduled_event,
    _next_scheduled_event,
    _passes_difficulty,
    _schedule_timeline_events,
    _success_level,
    create_session,
    read_projection,
    submit_command,
)
from app.service.module_runtime import ModulePackError, _validate_runtime, load_preset


def _actor() -> RuntimeActor:
    """创建无需数据库落盘的最小调查员状态。"""

    return RuntimeActor(
        id="actor-generic",
        room_id="00000000-0000-0000-0000-000000000001",
        display_name="调查员",
        location_id="room-a",
        state_json={"alive": True, "hp": 10, "max_hp": 10, "san": 50, "luck": 50},
    )


def test_success_levels_cover_coc7_boundaries() -> None:
    """成功等级和大失败边界必须按技能值确定。"""

    assert _success_level(1, 60) == "critical"
    assert _success_level(12, 60) == "extreme"
    assert _success_level(30, 60) == "hard"
    assert _success_level(60, 60) == "regular"
    assert _success_level(96, 40) == "fumble"
    assert _success_level(96, 60) == "failure"
    assert _passes_difficulty("hard", "hard") is True
    assert _passes_difficulty("regular", "hard") is False


def test_generic_module_action_runs_without_story_names() -> None:
    """通用效果解释器不认识任何《追书人》专名也能移动和授予线索。"""

    actor = _actor()
    state: dict[str, Any] = {
        "scene_id": "scene-a",
        "location_id": "room-a",
        "known_locations": ["room-a"],
        "clues": [],
        "flags": {},
        "_runtime": {
            "actions": [
                {
                    "id": "generic-action",
                    "scene_id": "scene-a",
                    "action": "inspect_target",
                    "target_id": "generic-object",
                    "outcome": {
                        "clues": ["generic-clue"],
                        "location_id": "room-b",
                        "scene_id": "scene-b",
                        "facts": ["通用动作已经完成。"],
                    },
                }
            ]
        },
    }

    facts = _apply_runtime_action_outcome(state, actor, "inspect_target", "generic-object")

    assert facts == ["通用动作已经完成。"]
    assert state["clues"] == ["generic-clue"]
    assert state["scene_id"] == "scene-b"
    assert actor.location_id == "room-b"


def test_timeline_queue_is_ordered_and_idempotent() -> None:
    """多个定时事件按到期时间触发，重复调度不会生成第二份。"""

    actor = _actor()
    state: dict[str, Any] = {
        "world_time": datetime(1920, 1, 1, 10, tzinfo=UTC).isoformat(),
        "flags": {},
        "_runtime": {
            "timeline": [
                {"id": "later", "trigger": {"after_minutes": 20}, "effect": {"scene_id": "b"}},
                {"id": "first", "trigger": {"after_minutes": 10}, "effect": {"scene_id": "a"}},
            ]
        },
    }

    _schedule_timeline_events(state, ["later", "first", "first"])
    first = _next_scheduled_event(state, datetime(1920, 1, 1, 11, tzinfo=UTC))

    assert [item["id"] for item in state["scheduled_events"]] == ["first", "later"]
    assert first is not None and first["id"] == "first"
    _apply_scheduled_event(state, actor, first)
    assert state["scene_id"] == "a"
    assert [item["id"] for item in state["scheduled_events"]] == ["later"]


def test_dead_npc_is_removed_from_open_action_candidates() -> None:
    """NPC 死亡后上下文候选不得再次允许交谈，防止叙事复活。"""

    state: dict[str, Any] = {
        "scene_id": "scene-a",
        "clues": [],
        "flags": {},
        "npc_states": {"npc-a": {"alive": False}},
        "_runtime": {
            "locations": [{"id": "room-a", "label": "房间", "exits": []}],
            "objects": [],
            "npcs": [
                {
                    "id": "npc-a",
                    "name": "某人",
                    "location_id": "room-a",
                    "visibility": "public",
                }
            ],
            "actions": [],
            "checkpoints": [],
        },
    }

    candidates = _candidates_for(state, "room-a")

    assert all(candidate.target_id != "npc-a" for candidate in candidates)


def test_module_effect_rejects_unknown_and_dangling_fields() -> None:
    """通用效果拼错字段或引用不存在的定时事件时必须静态失败。"""

    runtime = copy.deepcopy(load_preset("paper_chase").runtime)
    runtime["actions"][0]["outcome"]["secret_patch"] = {"ending_id": "flee"}
    with pytest.raises(ModulePackError, match="未知效果"):
        _validate_runtime(runtime)

    runtime = copy.deepcopy(load_preset("paper_chase").runtime)
    runtime["actions"][0]["outcome"]["schedule_events"] = ["missing-event"]
    with pytest.raises(ModulePackError, match="不存在的定时事件"):
        _validate_runtime(runtime)


async def test_failed_check_restores_luck_decision_after_refresh(db_session, monkeypatch) -> None:
    """失败检定持久化骰点，刷新后仍可花幸运且不会重掷。"""

    room_id = "00000000-0000-0000-0000-000000000211"
    actor_id = "actor-211"
    db_session.add(Room(id=room_id, room_code="P1C211", room_name="骰后选择", max_players=1))
    await db_session.commit()
    await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id=actor_id,
        display_name="调查员",
    )
    session = await db_session.get(GameSession, room_id)
    assert session is not None
    state = dict(session.state_json)
    state["scene_id"] = "cemetery"
    state["location_id"] = "cemetery"
    state["clues"] = [*state["clues"], "tunnel_hint"]
    session.state_json = state
    actor = await db_session.get(RuntimeActor, actor_id)
    assert actor is not None
    actor.location_id = "cemetery"
    await db_session.commit()

    started = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="open-crypt-start-211",
            expected_revision=0,
            actor_id=actor_id,
            command={
                "kind": "start_check",
                "check_id": "open-crypt-211",
                "skill_id": "strength",
                "goal": "open_crypt",
            },
        ),
    )
    monkeypatch.setattr("app.service.gm_runtime.secrets.randbelow", lambda _limit: 60)
    rolled = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="open-crypt-roll-211",
            expected_revision=started.revision,
            actor_id=actor_id,
            command={"kind": "roll_check", "check_id": "open-crypt-211"},
        ),
    )

    assert rolled.check is not None and rolled.check.roll == 61
    assert rolled.check.status == "awaiting_roll_decision"
    refreshed = await read_projection(db_session, room_id=room_id, actor_id=actor_id)
    assert refreshed.pending_decisions[0].options == ["accept_failure", "spend_luck", "push"]

    resolved = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="open-crypt-luck-211",
            expected_revision=rolled.revision,
            actor_id=actor_id,
            command={
                "kind": "resolve_check",
                "check_id": "open-crypt-211",
                "option": "spend_luck",
            },
        ),
    )

    assert resolved.check is not None and resolved.check.roll == 61
    assert resolved.check.luck_spent == 11
    assert resolved.check.final_result is True
    assert resolved.projection.luck == 39
    assert resolved.projection.location_id == "crypt"


async def test_start_check_rejects_undeclared_skill_and_difficulty(db_session) -> None:
    """Kernel 必须拒绝场景外目标、错误技能和模型擅自修改的难度。"""

    room_id = "00000000-0000-0000-0000-000000000213"
    actor_id = "actor-213"
    db_session.add(Room(id=room_id, room_code="P1C213", room_name="检定校验", max_players=1))
    await db_session.commit()
    await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id=actor_id,
        display_name="调查员",
    )
    session = await db_session.get(GameSession, room_id)
    actor = await db_session.get(RuntimeActor, actor_id)
    assert session is not None and actor is not None
    state = dict(session.state_json)
    state["scene_id"] = "cemetery"
    state["location_id"] = "cemetery"
    session.state_json = state
    actor.location_id = "cemetery"
    await db_session.commit()

    invalid_commands = [
        (
            "undeclared-check-213",
            {"skill_id": "spot-hidden", "goal": "missing-checkpoint"},
            "未在当前场景声明",
        ),
        (
            "wrong-skill-213",
            {"skill_id": "spot-hidden", "goal": "gravekeeper"},
            "技能与模组声明不一致",
        ),
        (
            "wrong-difficulty-213",
            {"skill_id": "charm", "goal": "gravekeeper", "difficulty": "hard"},
            "难度与模组声明不一致",
        ),
    ]
    for request_id, fields, message in invalid_commands:
        command = {
            "kind": "start_check",
            "check_id": request_id,
            **fields,
        }
        with pytest.raises(GmRuntimeError, match=message):
            await submit_command(
                db_session,
                room_id=room_id,
                envelope=CommandEnvelope(
                    client_request_id=request_id,
                    expected_revision=0,
                    actor_id=actor_id,
                    command=command,
                ),
            )


async def test_intent_context_contains_public_skill_guidance(db_session) -> None:
    """意图模型必须收到公开技能用途，但不能收到规则书或 keeper 数值。"""

    room_id = "00000000-0000-0000-0000-000000000214"
    actor_id = "actor-214"
    db_session.add(Room(id=room_id, room_code="P1C214", room_name="技能上下文", max_players=1))
    await db_session.commit()
    await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id=actor_id,
        display_name="调查员",
    )

    snapshot = await build_context_snapshot(db_session, room_id=room_id, actor_id=actor_id)
    assert snapshot.module_slice is not None
    skills = snapshot.module_slice.structured_data.get("skills", [])
    assert isinstance(skills, list)
    assert any(
        isinstance(skill, dict)
        and skill.get("id") == "spot-hidden"
        and skill.get("purpose")
        and skill.get("no_check_when")
        for skill in skills
    )
    assert "stats" not in snapshot.model_dump_json()
    assert "keeper_background" not in snapshot.model_dump_json()
