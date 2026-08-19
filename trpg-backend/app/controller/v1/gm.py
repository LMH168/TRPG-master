"""新 GM 运行时的 Phase 0 会话和命令 API。"""

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.controller.dependencies import get_current_user
from app.core.config import get_settings, host_model_is_configured
from app.core.db import get_db
from app.core.errors import AppException, ErrorCode
from app.dto.common import ApiResponse
from app.dto.gm import (
    CommandEnvelope,
    CommandResult,
    GmTurnRead,
    PlayerProjection,
    SessionCreateBody,
    SessionRead,
    TurnInputBody,
)
from app.models.user import User
from app.service import gm_runtime
from app.service import room as room_service

router = APIRouter(prefix="/gm/sessions", tags=["gm-runtime"])


@router.post("", response_model=ApiResponse[SessionRead], status_code=status.HTTP_201_CREATED)
async def create_gm_session(
    payload: SessionCreateBody,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiResponse[SessionRead]:
    """仅允许房间成员为自己的玩家身份创建固定版本 GM 会话。"""

    try:
        settings = get_settings()
        if settings.app_env == "production" and not host_model_is_configured(settings):
            raise AppException(
                ErrorCode.HOST_MODEL_UNAVAILABLE,
                "生产主持 provider 未配置，暂不能开始游戏",
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        player = await room_service.require_room_member(db, payload.room_id, reconnect_token)
        if player.user_id != user.id or player.id != payload.actor_id:
            raise room_service.RoomAuthorizationError("不能为其他玩家创建 GM 会话")
        result = await gm_runtime.create_session(
            db,
            room_id=payload.room_id,
            module_id=payload.module_id,
            actor_id=payload.actor_id,
            display_name=payload.display_name,
        )
    except room_service.RoomAuthenticationError as exc:
        raise AppException(ErrorCode.UNAUTHORIZED, str(exc), status.HTTP_401_UNAUTHORIZED) from exc
    except room_service.RoomAuthorizationError as exc:
        raise AppException(ErrorCode.FORBIDDEN, str(exc), status.HTTP_403_FORBIDDEN) from exc
    except gm_runtime.GmRuntimeError as exc:
        raise AppException(ErrorCode.CONFLICT, str(exc), status.HTTP_409_CONFLICT) from exc
    return ApiResponse.ok(result)


@router.post("/{room_id}/turns", response_model=ApiResponse[CommandResult])
async def submit_gm_command(
    room_id: str,
    payload: CommandEnvelope,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[CommandResult]:
    """提交受 DTO 限制的命令，并返回幂等回执和玩家投影。"""

    try:
        player = await room_service.require_room_member(db, room_id, reconnect_token)
        if player.id != payload.actor_id:
            raise room_service.RoomAuthorizationError("不能替其他玩家提交 GM 命令")
        # 直接命令只有玩家点击投骰时需要 Narrator 续写；其他 Kernel
        # 调用保持纯确定性，不会意外增加模型请求。
        result = await gm_runtime.submit_command(
            db,
            room_id=room_id,
            envelope=payload,
            narrate=payload.command.kind == "roll_check",
        )
    except room_service.RoomAuthenticationError as exc:
        raise AppException(ErrorCode.UNAUTHORIZED, str(exc), status.HTTP_401_UNAUTHORIZED) from exc
    except room_service.RoomAuthorizationError as exc:
        raise AppException(ErrorCode.FORBIDDEN, str(exc), status.HTTP_403_FORBIDDEN) from exc
    except gm_runtime.GmRuntimeError as exc:
        code = ErrorCode.REVISION_CONFLICT if "revision" in str(exc) else ErrorCode.CONFLICT
        raise AppException(code, str(exc), status.HTTP_409_CONFLICT) from exc
    return ApiResponse.ok(result)


@router.get("/{room_id}/projection", response_model=ApiResponse[PlayerProjection])
async def get_gm_projection(
    room_id: str,
    actor_id: str,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[PlayerProjection]:
    """读取当前玩家投影，供浏览器刷新和重连恢复。"""

    try:
        player = await room_service.require_room_member(db, room_id, reconnect_token)
        if player.id != actor_id:
            raise room_service.RoomAuthorizationError("不能读取其他玩家投影")
        result = await gm_runtime.read_projection(db, room_id=room_id, actor_id=actor_id)
    except room_service.RoomAuthenticationError as exc:
        raise AppException(ErrorCode.UNAUTHORIZED, str(exc), status.HTTP_401_UNAUTHORIZED) from exc
    except room_service.RoomAuthorizationError as exc:
        raise AppException(ErrorCode.FORBIDDEN, str(exc), status.HTTP_403_FORBIDDEN) from exc
    except gm_runtime.GmRuntimeError as exc:
        raise AppException(ErrorCode.NOT_FOUND, str(exc), status.HTTP_404_NOT_FOUND) from exc
    return ApiResponse.ok(result)


@router.post("/{room_id}/turns/free-text", response_model=ApiResponse[GmTurnRead])
async def submit_gm_free_text(
    room_id: str,
    payload: TurnInputBody,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[GmTurnRead]:
    """把玩家自然语言交给受约束 AI 主持，并返回澄清或 Kernel 回执。"""

    try:
        player = await room_service.require_room_member(db, room_id, reconnect_token)
        if player.id != payload.actor_id:
            raise room_service.RoomAuthorizationError("不能替其他玩家提交自然语言行动")
        result = await gm_runtime.submit_free_text(db, room_id=room_id, payload=payload)
    except room_service.RoomAuthenticationError as exc:
        raise AppException(ErrorCode.UNAUTHORIZED, str(exc), status.HTTP_401_UNAUTHORIZED) from exc
    except room_service.RoomAuthorizationError as exc:
        raise AppException(ErrorCode.FORBIDDEN, str(exc), status.HTTP_403_FORBIDDEN) from exc
    except gm_runtime.GmRuntimeError as exc:
        code = ErrorCode.CONFLICT
        message = str(exc)
        if message == "gm_unavailable":
            code = ErrorCode.HOST_MODEL_UNAVAILABLE
        elif "revision" in message:
            code = ErrorCode.REVISION_CONFLICT
        raise AppException(code, message, status.HTTP_409_CONFLICT) from exc
    return ApiResponse.ok(result)
