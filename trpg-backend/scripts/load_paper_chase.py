"""把《追书人》的公开目录种子写入当前数据库，供 E2E 和本地启动使用。"""

import asyncio

from sqlalchemy import select

from app.core.db import async_session_factory
from app.core.seed import BUILTIN_SYSTEM_ID, ensure_seed_content
from app.models.content import Scenario

E2E_MULTIPLAYER_SCENARIO_ID = "00000000-0000-0000-0000-000000000099"


async def load_paper_chase() -> None:
    """幂等加载目录数据；不解析或上传模组原文。"""

    async with async_session_factory() as db:
        await ensure_seed_content(db)
        # 仅供 E2E 验证账号、房间和讨论区多人外壳；不包含 AI 主持运行内容，
        # 也不代表第二个正式预设已经可玩。
        existing = await db.scalar(
            select(Scenario).where(Scenario.module_id == "e2e-multiplayer-coc7")
        )
        if existing is None:
            db.add(
                Scenario(
                    id=E2E_MULTIPLAYER_SCENARIO_ID,
                    module_id="e2e-multiplayer-coc7",
                    game_system_id=BUILTIN_SYSTEM_ID,
                    title="E2E 多人测试目录",
                    status="ready",
                    version="test-1",
                    authors=["TRPG-master"],
                    players_min=2,
                    players_max=2,
                    difficulty=1,
                    story_pages=[],
                )
            )
            await db.commit()
    print("已加载《追书人》公开目录")


if __name__ == "__main__":
    asyncio.run(load_paper_chase())
