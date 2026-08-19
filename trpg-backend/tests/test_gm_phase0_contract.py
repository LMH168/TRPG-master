"""Phase 0 DTO 与预设 ModulePack 的最小契约测试。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

from app.dto.gm import CommandEnvelope
from app.models.gm import CheckRun, PendingDecisionRecord
from app.models.room import Room
from app.service.gm_runtime import create_session, submit_command
from app.service.module_runtime import ModulePackError, load_preset
from scripts.provider_smoke import _classify_error, _verify_control_flow
from tests.helpers import bearer, create_room, reconnect, register


def test_command_rejects_unknown_fields() -> None:
    """模型多输出字段时必须在边界拒绝，而不是静默丢弃。"""

    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(
            {
                "client_request_id": "request-1",
                "expected_revision": 0,
                "actor_id": "actor-1",
                "command": {
                    "kind": "wait_until",
                    "target_time": datetime.now(UTC).isoformat(),
                    "write_state": {"san": 0},
                },
            }
        )


def test_paper_chase_preset_has_static_catalog() -> None:
    """《追书人》目录应可在无模型情况下加载。"""

    pack = load_preset("paper_chase")
    assert pack.title == "追书人"
    assert pack.catalog["story_pages"]


def test_module_pack_rejects_invalid_catalog(tmp_path: Path) -> None:
    """manifest 与 catalog 不一致时不能安装半成品运行包。"""

    (tmp_path / "manifest.json").write_text(
        '{"module_id":"bad","title":"A","content_version":"v1","catalog_file":"catalog.json"}',
        encoding="utf-8",
    )
    (tmp_path / "catalog.json").write_text('{"title":"B","story_pages":[]}', encoding="utf-8")
    with pytest.raises(ModulePackError, match="标题不一致"):
        from app.service.module_runtime import load_module_pack

        load_module_pack(tmp_path)


async def test_wait_command_is_idempotent(db_session) -> None:
    """同一 client_request_id 重试只返回原回执，不重复推进 revision。"""

    room_id = "00000000-0000-0000-0000-000000000099"
    db_session.add(
        Room(
            id=room_id,
            room_code="GMTEST",
            room_name="Phase 0",
            max_players=1,
        )
    )
    await db_session.commit()
    session = await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id="actor-1",
        display_name="调查员",
    )
    target = session.projection.world_time + timedelta(hours=2)
    command = CommandEnvelope(
        client_request_id="wait-1",
        expected_revision=0,
        actor_id="actor-1",
        command={"kind": "wait_until", "target_time": target},
    )
    first = await submit_command(db_session, room_id=room_id, envelope=command)
    second = await submit_command(db_session, room_id=room_id, envelope=command)
    assert first.revision == second.revision == 1
    assert first.events[0].event_id == second.events[0].event_id


async def test_provider_control_flow_gate_is_deterministic() -> None:
    """超时、取消和普通错误必须得到稳定且不含异常正文的分类。"""

    assert await _verify_control_flow()
    assert _classify_error(RuntimeError("不得输出的上游正文")) == "RuntimeError"


async def test_gm_session_rejects_another_players_identity(client: AsyncClient) -> None:
    """登录账号、房间凭证和 actor 必须属于同一个玩家。"""

    host_token = await register(client, nickname="房主")
    intruder_token = await register(client, nickname="旁观者")
    room = await create_room(client, token=host_token, max_players=1)
    payload = {
        "roomId": room["roomId"],
        "moduleId": "paper-chase",
        "actorId": room["playerId"],
        "displayName": "调查员",
    }

    missing_room_identity = await client.post(
        "/api/v1/gm/sessions", json=payload, headers=bearer(host_token)
    )
    assert missing_room_identity.status_code == 401

    mismatched_account = await client.post(
        "/api/v1/gm/sessions",
        json=payload,
        headers={**bearer(intruder_token), **reconnect(room["reconnectToken"])},
    )
    assert mismatched_account.status_code == 403


async def test_gm_command_rejects_another_actor(client: AsyncClient) -> None:
    """房间成员不能把自己的重连凭证用于其他 actor 的命令。"""

    host_token = await register(client, nickname="房主")
    room = await create_room(client, token=host_token, max_players=1)
    headers = {**bearer(host_token), **reconnect(room["reconnectToken"])}
    created = await client.post(
        "/api/v1/gm/sessions",
        json={
            "roomId": room["roomId"],
            "moduleId": "paper-chase",
            "actorId": room["playerId"],
            "displayName": "调查员",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    world_time = datetime.fromisoformat(created.json()["data"]["projection"]["worldTime"])

    response = await client.post(
        f"/api/v1/gm/sessions/{room['roomId']}/turns",
        json={
            "clientRequestId": "forged-actor",
            "expectedRevision": 0,
            "actorId": "00000000-0000-0000-0000-000000000001",
            "command": {
                "kind": "wait_until",
                "targetTime": (world_time + timedelta(hours=1)).isoformat(),
            },
        },
        headers=reconnect(room["reconnectToken"]),
    )
    assert response.status_code == 403


async def test_kernel_moves_inspects_and_talks_without_forcing_a_check(db_session) -> None:
    """低风险移动、调查和交谈直接提交领域事件，不凭空建立检定。"""

    room_id = "00000000-0000-0000-0000-000000000098"
    db_session.add(Room(id=room_id, room_code="GM1A", room_name="Phase 1A", max_players=1))
    await db_session.commit()
    await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id="actor-1a",
        display_name="调查员",
    )
    commands = [
        {"kind": "move_actor", "targetId": "library"},
        {"kind": "inspect_target", "targetId": "old_newspapers"},
        {"kind": "talk_to_npc", "targetId": "librarian", "topic": "旧书"},
    ]
    # 先移动，再调查和交谈；这些低风险动作不应凭空建立检定。
    moved = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="move-1a",
            expected_revision=0,
            actor_id="actor-1a",
            command=commands[0],
        ),
    )
    assert moved.projection.location_id == "library"
    inspected = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="inspect-1a",
            expected_revision=1,
            actor_id="actor-1a",
            command=commands[1],
        ),
    )
    assert inspected.events[0].event_type == "target_inspected"
    talked = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="talk-1a",
            expected_revision=2,
            actor_id="actor-1a",
            command=commands[2],
        ),
    )
    assert talked.events[0].event_type == "npc_contacted"


async def test_kernel_check_roll_is_server_owned_and_idempotent(db_session) -> None:
    """客户端不能提交骰点；同一 roll 请求重放返回相同骰点和 revision。"""

    room_id = "00000000-0000-0000-0000-000000000097"
    db_session.add(Room(id=room_id, room_code="GM1B", room_name="Phase 1A", max_players=1))
    await db_session.commit()
    await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id="actor-1b",
        display_name="调查员",
    )
    start = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="check-start",
            expected_revision=0,
            actor_id="actor-1b",
            command={
                "kind": "start_check",
                "checkId": "check-1a",
                "skillId": "library-use",
                "goal": "检索旧报纸",
            },
        ),
    )
    assert start.check and start.check.status == "awaiting_roll"
    assert start.pending_decisions and start.pending_decisions[0].check_id == "check-1a"
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(
            {
                "clientRequestId": "forged-roll",
                "expectedRevision": 1,
                "actorId": "actor-1b",
                "command": {"kind": "roll_check", "checkId": "check-1a", "roll": 1},
            }
        )
    roll_command = CommandEnvelope(
        client_request_id="check-roll",
        expected_revision=1,
        actor_id="actor-1b",
        command={"kind": "roll_check", "checkId": "check-1a"},
    )
    first = await submit_command(db_session, room_id=room_id, envelope=roll_command)
    second = await submit_command(db_session, room_id=room_id, envelope=roll_command)
    assert first.check and second.check
    assert first.check.roll == second.check.roll
    assert first.revision == second.revision == 2
    assert await db_session.scalar(select(CheckRun).where(CheckRun.id == "check-1a")) is not None
    assert (
        await db_session.scalar(
            select(PendingDecisionRecord).where(PendingDecisionRecord.check_id == "check-1a")
        )
        is not None
    )
