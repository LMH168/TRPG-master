"""健康检查接口，给部署平台/监控探活用，不需要鉴权、不碰数据库。"""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings, host_model_is_configured
from app.core.db import get_db
from app.dto.common import ApiResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=ApiResponse[dict[str, str]])
async def health(db: AsyncSession = Depends(get_db)) -> ApiResponse[dict[str, str]]:
    """返回存储和生产主持配置状态，不输出密钥或上游响应内容。"""

    storage = "ok"
    try:
        await db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        storage = "unavailable"
    settings = get_settings()
    model = "configured" if host_model_is_configured(settings) else "unconfigured"
    overall = "ok" if storage == "ok" and model == "configured" else "degraded"
    return ApiResponse.ok(
        {
            "status": overall,
            "storage": storage,
            "model": model,
            "provider": settings.host_model_provider,
        }
    )
