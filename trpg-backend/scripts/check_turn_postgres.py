"""CI 中确认 Phase 0 GM 回合持久化表已在 PostgreSQL 建立。"""

import asyncio
import os

import asyncpg


async def main() -> None:
    """检查新 Kernel 的回合、事件、回执和 Outbox 表，不触碰业务数据。"""

    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(url)
    try:
        names = await connection.fetch(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = ANY($1::text[])
            """,
            ["gm_turn_runs", "gm_events", "gm_command_receipts", "gm_outbox_messages"],
        )
        found = {row["table_name"] for row in names}
        assert found == {
            "gm_turn_runs",
            "gm_events",
            "gm_command_receipts",
            "gm_outbox_messages",
        }
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
