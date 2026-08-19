"""验证清理后保留的房间 WebSocket、聊天入口和主持不可用边界。"""

import json
from collections.abc import Iterator

import anyio
import pytest
from starlette.testclient import TestClient

from app.main import app

ROOMS_BASE = "/api/v1/rooms"
RECEIVE_TIMEOUT_SECONDS = 5.0


@pytest.fixture
def sync_client() -> Iterator[TestClient]:
    """提供同时支持 HTTP 和 WebSocket 的同步测试客户端。"""

    yield TestClient(app)


def register_and_login(client: TestClient, account: str = "host1") -> str:
    """注册测试账号并返回登录令牌。"""

    response = client.post(
        "/api/v1/auth/register",
        json={"account": account, "password": "secret1", "nickname": "房主"},
    )
    assert response.status_code == 201
    return response.json()["data"]["token"]


def create_room(client: TestClient, token: str, max_players: int = 1) -> dict:
    """创建一个归属于当前账号的基础房间。"""

    response = client.post(
        ROOMS_BASE,
        json={"roomName": "WS测试房间", "nickname": "房主", "maxPlayers": max_players},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    return response.json()["data"]


def complete_character(
    client: TestClient,
    room_id: str,
    reconnect_token: str,
    name: str = "陈探员",
) -> None:
    """创建并完成一张最小合法角色卡，供房间开局测试复用。"""

    headers = {"X-Reconnect-Token": reconnect_token}
    draft = client.post(f"{ROOMS_BASE}/{room_id}/characters", headers=headers)
    character_id = draft.json()["data"]["characterId"]
    response = client.patch(
        f"{ROOMS_BASE}/{room_id}/characters/{character_id}",
        json={
            "name": name,
            "attributes": {
                "STR": 50,
                "CON": 50,
                "POW": 50,
                "DEX": 50,
                "APP": 50,
                "SIZ": 50,
                "INT": 50,
                "EDU": 50,
                "LUCK": 50,
            },
            "derivedStats": {"HP": 12},
            "skills": {},
            "equipment": [],
            "occupation": None,
            "background": "",
            "notes": "",
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert (
        client.post(
            f"{ROOMS_BASE}/{room_id}/characters/{character_id}/complete", headers=headers
        ).status_code
        == 200
    )


def advance_to_building(client: TestClient, room: dict) -> None:
    """选择目录模组并把房间推进到建卡阶段。"""

    headers = {"X-Reconnect-Token": room["reconnectToken"]}
    modules = client.get("/api/v1/modules").json()["data"]
    module_id = next(module["id"] for module in modules if module["playersMin"] <= 1)
    assert (
        client.post(
            f"{ROOMS_BASE}/{room['roomId']}/module",
            json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
            headers=headers,
        ).status_code
        == 200
    )
    response = client.post(f"{ROOMS_BASE}/{room['roomId']}/start-story", headers=headers)
    assert response.status_code == 200


def receive_json(ws, *, timeout: float = RECEIVE_TIMEOUT_SECONDS):
    """带超时读取 WebSocket 消息，避免缺事件时测试永久阻塞。"""

    async def _receive_with_timeout():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    try:
        message = ws.portal.call(_receive_with_timeout)
    except TimeoutError as exc:
        raise AssertionError(f"WebSocket 在 {timeout}s 内没有发送消息") from exc
    if message.get("type") == "websocket.close":
        raise AssertionError(f"WebSocket 已关闭: {message!r}")
    return json.loads(message["text"])


def receive_until(ws, predicate, *, limit: int = 24):
    """持续读取到目标事件，并返回目标与过程中看到的全部事件。"""

    seen = []
    for _ in range(limit):
        message = receive_json(ws)
        seen.append(message)
        if predicate(message):
            return message, seen
    raise AssertionError(f"未收到预期 WebSocket 事件: {seen!r}")


def start_game(client: TestClient, room: dict, token: str) -> None:
    """通过保留的 WS 协议开局，并确认房间进入 InGame。"""

    with client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        ws.send_json(
            {
                "type": "room.join",
                "playerId": room["playerId"],
                "payload": {"reconnectToken": room["reconnectToken"]},
            }
        )
        assert receive_json(ws)["type"] == "session.bound"
        ws.send_json({"type": "game.start", "playerId": room["playerId"], "payload": {}})
        state, _ = receive_until(
            ws,
            lambda message: (
                message.get("type") == "room.state"
                and message.get("payload", {}).get("phase") == "InGame"
            ),
        )
        assert state["payload"]["roomId"] == room["roomId"]


def test_action_submit_reports_runtime_rebuild(sync_client: TestClient) -> None:
    """旧行动协议不再执行规则，必须稳定返回明确的重建中错误。"""

    token = register_and_login(sync_client, "runtime_removed")
    room = create_room(sync_client, token)
    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        ws.send_json(
            {
                "type": "room.join",
                "playerId": room["playerId"],
                "payload": {"reconnectToken": room["reconnectToken"]},
            }
        )
        assert receive_json(ws)["type"] == "session.bound"
        ws.send_json(
            {
                "type": "action.plan.submit",
                "playerId": room["playerId"],
                "payload": {"clientActionId": "cleanup-action", "utterance": "我查看房间"},
            }
        )
        error, _ = receive_until(ws, lambda message: message.get("type") == "error")

    assert error["payload"]["code"] == "GM_RUNTIME_UNAVAILABLE"
    assert error["payload"]["correlationId"] == "cleanup-action"
