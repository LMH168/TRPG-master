"""可靠回合查询与恢复 REST 路由。

所有端点先用重连凭证校验房间成员，再由 service 层限制结果 owner；错误响应沿用
全项目统一信封，避免向其他玩家泄露回合内容。
"""

from typing import NoReturn

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.errors import AppException, ErrorCode
from app.dto.common import ApiResponse
from app.dto.turn import TurnRead
from app.service import room as room_service
from app.service import turn_runtime as turn_service

router = APIRouter(prefix="/rooms", tags=["turns"])


def _raise_public_error(exc: Exception) -> NoReturn:
    """把内部异常映射为稳定且不泄密的玩家错误。"""

    if isinstance(exc, turn_service.TurnReadNotFoundError):
        raise AppException(ErrorCode.TURN_NOT_FOUND, str(exc), status.HTTP_404_NOT_FOUND) from exc
    if isinstance(exc, turn_service.TurnReadAuthorizationError):
        raise AppException(ErrorCode.FORBIDDEN, str(exc), status.HTTP_403_FORBIDDEN) from exc
    if isinstance(exc, turn_service.TurnResumeUnavailableError):
        raise AppException(
            ErrorCode.TURN_RESUME_UNAVAILABLE,
            str(exc),
            status.HTTP_501_NOT_IMPLEMENTED,
        ) from exc
    if isinstance(exc, room_service.RoomAuthenticationError):
        raise AppException(ErrorCode.UNAUTHORIZED, str(exc), status.HTTP_401_UNAUTHORIZED) from exc
    if isinstance(exc, room_service.RoomAuthorizationError):
        raise AppException(ErrorCode.FORBIDDEN, str(exc), status.HTTP_403_FORBIDDEN) from exc
    raise exc


async def _member(db: AsyncSession, room_id: str, reconnect_token: str | None):
    try:
        return await room_service.require_room_member(db, room_id, reconnect_token)
    except Exception as exc:
        _raise_public_error(exc)


@router.get("/{room_id}/turns/{turn_id}", response_model=ApiResponse[TurnRead])
async def get_turn(
    room_id: str,
    turn_id: str,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[TurnRead]:
    player = await _member(db, room_id, reconnect_token)
    try:
        turn = await turn_service.get_turn(
            db,
            room_id=room_id,
            turn_id=turn_id,
            player_id=player.id,
        )
    except Exception as exc:
        _raise_public_error(exc)
    return ApiResponse.ok(turn)


@router.get("/{room_id}/turns", response_model=ApiResponse[list[TurnRead]])
async def list_turns(
    room_id: str,
    client_action_id: str | None = Query(default=None, alias="clientActionId", max_length=200),
    active_only: bool = Query(default=False, alias="activeOnly"),
    limit: int = Query(default=20, ge=1, le=100),
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[TurnRead]]:
    player = await _member(db, room_id, reconnect_token)
    turns = await turn_service.list_turns(
        db,
        room_id=room_id,
        player_id=player.id,
        client_action_id=client_action_id,
        active_only=active_only,
        limit=limit,
    )
    return ApiResponse.ok(turns)


@router.post("/{room_id}/turns/{turn_id}/resume", response_model=ApiResponse[TurnRead])
async def resume_turn(
    room_id: str,
    turn_id: str,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ApiResponse[TurnRead]:
    player = await _member(db, room_id, reconnect_token)
    try:
        turn = await turn_service.resume_turn(
            db,
            room_id=room_id,
            turn_id=turn_id,
            player_id=player.id,
            runtime_mode=settings.turn_runtime_mode,
        )
    except Exception as exc:
        _raise_public_error(exc)
    return ApiResponse.ok(turn)
