"""提供房间鉴权、状态广播和玩家讨论区的最小 WebSocket 基础协议。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.dto.ws import (
    ActionSubmitPayload,
    ChatMessagePayload,
    ChatSendPayload,
    ClientEnvelope,
    ErrorPayload,
    GameStartPayload,
    PlayerReadyPayload,
    RoomJoinPayload,
    ServerEnvelope,
    SessionBoundPayload,
)
from app.service import auth as auth_service
from app.service import chat as chat_service
from app.service import room as room_service
from app.service.ws_events import broadcast_room_state
from app.service.ws_manager import manager

router = APIRouter()
logger = structlog.get_logger()

_UNAUTHORIZED_CLOSE_CODE = 4401
_NOT_FOUND_CLOSE_CODE = 4404


@asynccontextmanager
async def _short_db_session() -> AsyncIterator[AsyncSession]:
    """每条消息使用短数据库会话，避免长连接长期占用事务。"""

    async with async_session_factory() as db:
        try:
            yield db
        finally:
            await db.close()


async def _send_error(
    websocket: WebSocket,
    code: str,
    message: str,
    *,
    correlation_id: str | None = None,
) -> None:
    """向当前连接返回稳定且玩家可读的协议错误。"""

    payload = ErrorPayload(code=code, message=message, correlation_id=correlation_id)
    envelope = ServerEnvelope(type="error", payload=payload.model_dump(by_alias=True))
    await websocket.send_json(envelope.model_dump(by_alias=True))


async def _bind_player(
    db: AsyncSession,
    websocket: WebSocket,
    *,
    room_id: str,
    player_id: str | None,
    reconnect_token: str,
    authenticated_user_id: str,
) -> str | None:
    """校验账号、房间成员和重连凭证后绑定连接身份。"""

    player = await room_service.get_player(db, player_id) if player_id else None
    if (
        player is None
        or player.room_id != room_id
        or player.user_id != authenticated_user_id
        or player.reconnect_token != reconnect_token
    ):
        await websocket.close(code=_NOT_FOUND_CLOSE_CODE)
        return None
    manager.add(room_id, websocket, player.id)
    await room_service.set_player_connected(db, player.id, True)
    payload = SessionBoundPayload(room_id=room_id, player_id=player.id)
    envelope = ServerEnvelope(type="session.bound", payload=payload.model_dump(by_alias=True))
    await websocket.send_json(envelope.model_dump(by_alias=True))
    return player.id


async def _broadcast_chat(
    db: AsyncSession,
    websocket: WebSocket,
    *,
    room_id: str,
    player_id: str,
    payload: ChatSendPayload,
) -> None:
    """幂等保存并广播玩家讨论区消息。"""

    player = await room_service.get_player(db, player_id)
    if player is None or player.room_id != room_id:
        return
    room = await room_service.find_room_by_id(db, room_id)
    if room.phase == "Completed":
        await _send_error(websocket, "FORBIDDEN", "游戏已结束，无法发送消息")
        return
    message = await chat_service.save_chat_message(
        db,
        room_id,
        player_id,
        payload.text.strip(),
        payload.client_message_id,
    )
    chat_payload = ChatMessagePayload(
        message_id=message.id,
        player_id=message.player_id,
        nickname=player.nickname,
        text=message.text,
        sent_at=message.created_at,
        client_message_id=message.client_message_id,
    )
    envelope = ServerEnvelope(
        type="chat.message",
        payload=chat_payload.model_dump(by_alias=True, mode="json"),
    )
    await manager.broadcast(room_id, envelope.model_dump(by_alias=True))


@router.websocket("/ws/{room_id}")
async def room_socket(websocket: WebSocket, room_id: str, token: str | None = None) -> None:
    """维护一个已登录玩家的房间实时连接。"""

    async with _short_db_session() as db:
        try:
            authenticated_user = await auth_service.get_me(db, token)
        except auth_service.AuthenticationError:
            await websocket.close(code=_UNAUTHORIZED_CLOSE_CODE)
            return

    await websocket.accept()
    bound_player_id: str | None = None
    try:
        while True:
            raw = await websocket.receive_json()
            try:
                envelope = ClientEnvelope.model_validate(raw)
            except ValidationError as exc:
                logger.warning("ws_invalid_message", validation_error_count=exc.error_count())
                continue

            async with _short_db_session() as db:
                try:
                    if envelope.type == "room.join":
                        join = RoomJoinPayload.model_validate(envelope.payload)
                        bound_player_id = await _bind_player(
                            db,
                            websocket,
                            room_id=room_id,
                            player_id=envelope.player_id,
                            reconnect_token=join.reconnect_token,
                            authenticated_user_id=authenticated_user.user_id,
                        )
                        if bound_player_id is not None:
                            await broadcast_room_state(db, room_id)
                        continue

                    if bound_player_id is None or envelope.player_id != bound_player_id:
                        continue

                    if envelope.type == "player.ready":
                        payload = PlayerReadyPayload.model_validate(envelope.payload)
                        await room_service.set_player_ready(db, bound_player_id, payload.ready)
                        await broadcast_room_state(db, room_id)
                    elif envelope.type == "game.start":
                        GameStartPayload.model_validate(envelope.payload)
                        await room_service.begin_game(db, room_id, bound_player_id)
                        await broadcast_room_state(db, room_id)
                    elif envelope.type == "chat.send":
                        payload = ChatSendPayload.model_validate(envelope.payload)
                        await _broadcast_chat(
                            db,
                            websocket,
                            room_id=room_id,
                            player_id=bound_player_id,
                            payload=payload,
                        )
                    elif envelope.type == "action.plan.submit":
                        payload = ActionSubmitPayload.model_validate(envelope.payload)
                        await _send_error(
                            websocket,
                            "GM_RUNTIME_UNAVAILABLE",
                            "AI 主持正在重建，当前仅保留房间、聊天和骰子界面",
                            correlation_id=payload.client_action_id,
                        )
                except ValidationError as exc:
                    logger.warning(
                        "ws_invalid_message",
                        event_type=envelope.type,
                        validation_error_count=exc.error_count(),
                    )
                except room_service.RoomConflictError as exc:
                    await _send_error(websocket, "ROOM_CONFLICT", str(exc))
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove(room_id, websocket)
        if bound_player_id is not None:
            async with _short_db_session() as db:
                await room_service.set_player_connected(db, bound_player_id, False)
