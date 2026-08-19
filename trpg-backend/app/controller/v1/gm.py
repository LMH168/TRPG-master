"""新 GM 运行时的 Phase 0 会话和命令 API。"""

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.controller.dependencies import get_current_user
from app.core.db import get_db
from app.core.errors import AppException, ErrorCode
from app.dto.common import ApiResponse
from app.dto.gm import CommandEnvelope, CommandResult, SessionCreateBody, SessionRead
from app.models.user import User
from app.service import gm_runtime
from app.service import room as room_service

router = APIRouter(prefix="/gm/sessions", tags=["gm-runtime"])


@router.post("", response_model=ApiResponse[SessionRead], status_code=status.HTTP_201_CREATED)
async def create_gm_session(
    payload: SessionCreateBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiResponse[SessionRead]:
    """创建固定模组版本的 GM 会话和首个调查员 Actor。"""

    del user  # 当前 Phase 0 先由现有登录依赖完成身份校验。
    try:
        result = await gm_runtime.create_session(
            db,
            room_id=payload.room_id,
            module_id=payload.module_id,
            actor_id=payload.actor_id,
            display_name=payload.display_name,
        )
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
        await room_service.require_room_member(db, room_id, reconnect_token)
        result = await gm_runtime.submit_command(db, room_id=room_id, envelope=payload)
    except (room_service.RoomAuthenticationError, room_service.RoomAuthorizationError) as exc:
        raise AppException(ErrorCode.UNAUTHORIZED, str(exc), status.HTTP_401_UNAUTHORIZED) from exc
    except gm_runtime.GmRuntimeError as exc:
        code = ErrorCode.REVISION_CONFLICT if "revision" in str(exc) else ErrorCode.CONFLICT
        raise AppException(code, str(exc), status.HTTP_409_CONFLICT) from exc
    return ApiResponse.ok(result)
