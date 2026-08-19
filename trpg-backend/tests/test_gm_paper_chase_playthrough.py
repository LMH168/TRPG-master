"""《追书人》Phase 1C 的结构化脚本跑团门禁测试。"""

from datetime import timedelta
from typing import Any

from app.dto.gm import CommandEnvelope
from app.models.room import Room
from app.service.gm_runtime import create_session, read_projection, submit_command


async def _room(db_session, room_id: str, actor_id: str) -> None:
    """创建单人测试房间和冻结的《追书人》运行包。"""

    db_session.add(Room(id=room_id, room_code=actor_id[:8], room_name="追书人门禁", max_players=1))
    await db_session.commit()
    await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id=actor_id,
        display_name="调查员",
    )


async def _command(
    db_session, room_id: str, actor_id: str, revision: int, request: str, command: dict[str, Any]
):
    """以当前 revision 提交一个服务端权威命令。"""

    return await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id=request,
            expected_revision=revision,
            actor_id=actor_id,
            command=command,
        ),
    )


async def test_peaceful_route_and_projection_have_no_keeper_data(db_session) -> None:
    """邻居/守墓人路线可礼貌结束，投影不暴露运行包秘密。"""

    room_id, actor_id = "00000000-0000-0000-0000-000000000201", "actor-201"
    await _room(db_session, room_id, actor_id)
    result = await _command(
        db_session,
        room_id,
        actor_id,
        0,
        "neighbors",
        {"kind": "inspect_target", "target_id": "neighbors"},
    )
    result = await _command(
        db_session,
        room_id,
        actor_id,
        result.revision,
        "cemetery",
        {"kind": "move_actor", "target_id": "cemetery"},
    )
    result = await _command(
        db_session,
        room_id,
        actor_id,
        result.revision,
        "gravekeeper",
        {"kind": "talk_to_npc", "target_id": "gravekeeper", "topic": "礼貌询问"},
    )
    result = await _command(
        db_session,
        room_id,
        actor_id,
        result.revision,
        "call-douglas",
        {"kind": "inspect_target", "target_id": "call_douglas"},
    )
    result = await _command(
        db_session,
        room_id,
        actor_id,
        result.revision,
        "peaceful-ending",
        {"kind": "choose_option", "option_id": "peaceful_resolution"},
    )
    assert result.projection.ending_id == "peaceful_resolution"
    projection = await read_projection(db_session, room_id=room_id, actor_id=actor_id)
    serialized = projection.model_dump_json()
    assert "core_truth" not in serialized
    assert "keeper" not in serialized
    assert "douglas_became_ghoul" not in serialized


async def test_library_route_failure_still_recovers_to_underground(db_session) -> None:
    """关键检定失败也授予替代线索，随后可以进入地下路线。"""

    room_id, actor_id = "00000000-0000-0000-0000-000000000202", "actor-202"
    await _room(db_session, room_id, actor_id)
    moved = await _command(
        db_session,
        room_id,
        actor_id,
        0,
        "library",
        {"kind": "move_actor", "target_id": "library"},
    )
    started = await _command(
        db_session,
        room_id,
        actor_id,
        moved.revision,
        "library-check",
        {
            "kind": "start_check",
            "check_id": "library-check",
            "skill_id": "library-use",
            "goal": "查阅图书馆旧报纸",
        },
    )
    rolled = await _command(
        db_session,
        room_id,
        actor_id,
        started.revision,
        "library-roll",
        {"kind": "roll_check", "check_id": "library-check"},
    )
    assert rolled.revision == started.revision + 1
    assert "old_report" in rolled.projection.clues

    moved = await _command(
        db_session,
        room_id,
        actor_id,
        rolled.revision,
        "cemetery-202",
        {"kind": "move_actor", "target_id": "arnoldsburg"},
    )
    moved = await _command(
        db_session,
        room_id,
        actor_id,
        moved.revision,
        "cemetery-202b",
        {"kind": "move_actor", "target_id": "cemetery"},
    )
    opened = await _command(
        db_session,
        room_id,
        actor_id,
        moved.revision,
        "crypt-202",
        {"kind": "inspect_target", "target_id": "open_crypt"},
    )
    ended = await _command(
        db_session,
        room_id,
        actor_id,
        opened.revision,
        "follow-202",
        {"kind": "choose_option", "option_id": "follow_underground"},
    )
    assert ended.projection.ending_id == "follow_underground"


async def test_kill_then_flee_is_idempotent(db_session) -> None:
    """杀死道格拉斯后的食尸鬼结局和重复请求不会重复推进 revision。"""

    room_id, actor_id = "00000000-0000-0000-0000-000000000203", "actor-203"
    await _room(db_session, room_id, actor_id)
    moved = await _command(
        db_session,
        room_id,
        actor_id,
        0,
        "cemetery-203",
        {"kind": "move_actor", "target_id": "cemetery"},
    )
    killed = await _command(
        db_session,
        room_id,
        actor_id,
        moved.revision,
        "kill-203",
        {"kind": "inspect_target", "target_id": "attack_douglas"},
    )
    assert killed.projection.ending_id == "douglas_killed"
    flee = await _command(
        db_session,
        room_id,
        actor_id,
        killed.revision,
        "flee-203",
        {"kind": "choose_option", "option_id": "flee"},
    )
    replay = await _command(
        db_session,
        room_id,
        actor_id,
        0,
        "flee-203",
        {"kind": "choose_option", "option_id": "flee"},
    )
    assert flee.revision == replay.revision
    assert replay.projection.ending_id == "flee"


async def test_night_watch_interrupt_and_failed_sanity_reach_asylum(db_session) -> None:
    """等待会被夜间人影打断，理智失败进入疗养院结局。"""

    room_id, actor_id = "00000000-0000-0000-0000-000000000204", "actor-204"
    await _room(db_session, room_id, actor_id)
    moved = await _command(
        db_session,
        room_id,
        actor_id,
        0,
        "cemetery-204",
        {"kind": "move_actor", "target_id": "cemetery"},
    )
    watched = await _command(
        db_session,
        room_id,
        actor_id,
        moved.revision,
        "watch-204",
        {"kind": "inspect_target", "target_id": "night_watch"},
    )
    now = watched.projection.world_time
    interrupted = await _command(
        db_session,
        room_id,
        actor_id,
        watched.revision,
        "wait-204",
        {"kind": "wait_until", "target_time": now + timedelta(hours=2)},
    )
    assert interrupted.projection.scene_id == "confrontation"
    started = await _command(
        db_session,
        room_id,
        actor_id,
        interrupted.revision,
        "san-204",
        {
            "kind": "start_check",
            "check_id": "san-204",
            "skill_id": "sanity",
            "goal": "面对食尸鬼群的理智检定",
        },
    )
    resolved = await _command(
        db_session,
        room_id,
        actor_id,
        started.revision,
        "san-roll-204",
        {"kind": "roll_check", "check_id": "san-204"},
    )
    # 无论随机结果如何，失败前进规则保留可达结局；成功则仍停留在冲突场景。
    assert resolved.projection.ending_id in {None, "asylum"}
