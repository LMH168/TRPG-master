"""把《追书人》的公开目录种子写入当前数据库，供 E2E 和本地启动使用。"""

import asyncio

from app.core.db import async_session_factory
from app.core.seed import ensure_seed_content


async def load_paper_chase() -> None:
    """幂等加载目录数据；不解析或上传模组原文。"""

    async with async_session_factory() as db:
        await ensure_seed_content(db)
    print("已加载《追书人》公开目录")


if __name__ == "__main__":
    asyncio.run(load_paper_chase())
