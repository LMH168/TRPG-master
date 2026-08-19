"""issue #107 的 WS/REST 行为测试：讨论区落库与回显、历史分页和退房清理。

复用 tests/test_ws.py 的装置模式（同步 TestClient + websocket_connect）。

⚠️ 本文件全部用**单条 WS 连接**：Starlette TestClient 的每个 websocket_connect
各起一个独立 portal 线程 + 独立事件循环，同一房间开两条连接时，广播要往另一个
事件循环的 websocket send —— 跨循环 await 直接挂死（conftest.py 顶部注释里
"各循环各连接"说的就是这件事；test_ws.py 现有的双连接用例之所以能跑，是因为
那两条连接在**不同房间**、从不互相广播）。「同房间双客户端都能收到广播」这类
断言只有 SDK e2e（真 uvicorn、单事件循环）能做——见
e2e/tests/discussion-chat.e2e.ts，那边有完整的双客户端覆盖；这里守住的是
落库、幂等、鉴权和清理这些单连接就能证明的行为。
"""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from app.main import app
from tests.test_ws import (
    ROOMS_BASE,
    advance_to_building,
    complete_character,
    create_room,
    receive_until,
    register_and_login,
)


@pytest.fixture
def sync_client() -> Iterator[TestClient]:
    yield TestClient(app)


def _join_ws(ws, player: dict) -> None:
    ws.send_json(
        {
            "type": "room.join",
            "playerId": player["playerId"],
            "payload": {"reconnectToken": player["reconnectToken"]},
        }
    )
    assert ws.receive_json()["type"] == "session.bound"


def _send_chat(ws, player: dict, text: str, client_message_id: str) -> None:
    ws.send_json(
        {
            "type": "chat.send",
            "playerId": player["playerId"],
            "payload": {"text": text, "clientMessageId": client_message_id},
        }
    )


def _submit_action(ws, player: dict, utterance: str) -> None:
    ws.send_json(
        {
            "type": "action.plan.submit",
            "playerId": player["playerId"],
            "payload": {"clientActionId": str(uuid4()), "utterance": utterance},
        }
    )


# ── 讨论区：落库 + 广播回显 ───────────────────────────


def test_chat_send_echoes_broadcast_with_full_payload(sync_client: TestClient) -> None:
    """chat.send 落库后广播 chat.message（发送者自己也靠广播回显，前端不做
    本地乐观插入）。payload 带齐渲染所需字段，时间戳带时区后缀（UtcDatetime
    的全项目约定）。"""
    token = register_and_login(sync_client, "chat_host")
    room = create_room(sync_client, token)

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        _send_chat(ws, room, "我们先去图书馆吧", "msg-1")
        envelope, _ = receive_until(ws, lambda message: message.get("type") == "chat.message")

    assert envelope["type"] == "chat.message"
    assert envelope["payload"]["text"] == "我们先去图书馆吧"
    assert envelope["payload"]["nickname"] == "房主"
    assert envelope["payload"]["playerId"] == room["playerId"]
    assert envelope["payload"]["clientMessageId"] == "msg-1"
    assert envelope["payload"]["sentAt"].endswith(("Z", "+00:00"))


def test_chat_send_is_idempotent_on_duplicate_client_message_id(
    sync_client: TestClient,
) -> None:
    """重连后重发同一条消息（相同 clientMessageId）：库里只有一行，第二次广播
    与第一次是同一条消息（messageId 相同），其他人不会看到重复气泡。"""
    token = register_and_login(sync_client, "idem_host")
    room = create_room(sync_client, token)

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        _send_chat(ws, room, "只发一次的消息", "dup-1")
        first, _ = receive_until(ws, lambda message: message.get("type") == "chat.message")
        _send_chat(ws, room, "只发一次的消息", "dup-1")
        second, _ = receive_until(ws, lambda message: message.get("type") == "chat.message")

    assert first["payload"]["messageId"] == second["payload"]["messageId"]

    history = sync_client.get(
        f"{ROOMS_BASE}/{room['roomId']}/messages",
        headers={"X-Reconnect-Token": room["reconnectToken"]},
    ).json()["data"]
    assert len(history) == 1


# ── 旧行动入口：明确停用 ─────────────────────────────


def test_action_submit_does_not_publish_old_runtime_events(sync_client: TestClient) -> None:
    """清理期间旧行动入口只返回重建提示，不伪造行动广播或主持叙事。"""
    token = register_and_login(sync_client, "act_host")
    room = create_room(sync_client, token)

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        _submit_action(ws, room, "我推开吱呀作响的木门")
        error, seen = receive_until(ws, lambda message: message.get("type") == "error")

    assert error["payload"]["code"] == "GM_RUNTIME_UNAVAILABLE"
    assert not any(
        message.get("type") in {"action.broadcast", "narration.push"} for message in seen
    )


# ── 可靠回合占用 ─────────────────────────────────────


def test_pre_turn_validation_failure_allows_explicit_retry(sync_client: TestClient) -> None:
    """运行时不存在时尚未创建 Turn，但传输连接仍应允许后续显式重试。"""
    token = register_and_login(sync_client, "fail_host")
    room = create_room(sync_client, token)

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)

        _submit_action(ws, room, "我尝试翻译古籍")
        failure, _ = receive_until(ws, lambda message: message["type"] == "error")
        assert failure["payload"]["code"] == "GM_RUNTIME_UNAVAILABLE"

        # 立刻重试，确保前置校验错误不会把连接或房间留在不可用状态。
        _submit_action(ws, room, "我再次尝试翻译")
        failure, _ = receive_until(ws, lambda message: message["type"] == "error")
        assert failure["payload"]["code"] == "GM_RUNTIME_UNAVAILABLE"


# ── 历史消息 REST ────────────────────────────────────


def test_messages_pagination_with_before_cursor(sync_client: TestClient) -> None:
    token = register_and_login(sync_client, "page_host")
    room = create_room(sync_client, token)
    headers = {"X-Reconnect-Token": room["reconnectToken"]}

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        for i in range(3):
            _send_chat(ws, room, f"第{i + 1}条", f"pg-{i}")
            receive_until(ws, lambda message: message.get("type") == "chat.message")

    page1 = sync_client.get(
        f"{ROOMS_BASE}/{room['roomId']}/messages", params={"limit": 2}, headers=headers
    ).json()["data"]
    assert [m["text"] for m in page1] == ["第3条", "第2条"]  # 倒序，最新在前

    page2 = sync_client.get(
        f"{ROOMS_BASE}/{room['roomId']}/messages",
        params={"limit": 2, "before": page1[-1]["messageId"]},
        headers=headers,
    ).json()["data"]
    assert [m["text"] for m in page2] == ["第1条"]


def test_messages_rejects_non_member(sync_client: TestClient) -> None:
    token = register_and_login(sync_client, "member_host")
    room = create_room(sync_client, token)
    # 另一个房间的人拿自己的 reconnect_token 来查这个房间 → 拒绝
    other_token = register_and_login(sync_client, "outsider")
    other_room = create_room(sync_client, other_token)

    response = sync_client.get(
        f"{ROOMS_BASE}/{room['roomId']}/messages",
        headers={"X-Reconnect-Token": other_room["reconnectToken"]},
    )
    assert response.status_code == 403


# ── 退房清理 / 复盘纯净 ──────────────────────────────


def test_end_game_clears_temporary_chat(sync_client: TestClient) -> None:
    """房主结束游戏后清空临时讨论消息，不保留旧主持回放概念。"""

    token = register_and_login(sync_client, "end_host")
    room = create_room(sync_client, token)
    headers = {"X-Reconnect-Token": room["reconnectToken"]}
    advance_to_building(sync_client, room)
    complete_character(sync_client, room["roomId"], room["reconnectToken"])

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        ws.send_json({"type": "game.start", "playerId": room["playerId"], "payload": {}})
        receive_until(
            ws,
            lambda message: (
                message.get("type") == "room.state"
                and message.get("payload", {}).get("phase") == "InGame"
            ),
        )
        _send_chat(ws, room, "这句话不该进复盘", "end-1")
        receive_until(ws, lambda message: message.get("type") == "chat.message")

    # 聊天在 end 之前查得到
    assert (
        len(
            sync_client.get(f"{ROOMS_BASE}/{room['roomId']}/messages", headers=headers).json()[
                "data"
            ]
        )
        == 1
    )

    end_response = sync_client.post(f"{ROOMS_BASE}/{room['roomId']}/end", headers=headers)
    assert end_response.status_code == 200

    # end 之后聊天被清空
    assert (
        sync_client.get(f"{ROOMS_BASE}/{room['roomId']}/messages", headers=headers).json()["data"]
        == []
    )
