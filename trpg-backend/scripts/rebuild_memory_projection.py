"""按房间清空并重建可派生的长期 Memory 投影。"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.service.memory_projection import memory_projection_supervisor


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重建指定房间的 Memory 投影")
    parser.add_argument("--room-id", required=True, help="需要重建的精确房间 ID")
    parser.add_argument("--batch-size", type=int, default=100, help="每批扫描的 Turn 数量")
    return parser.parse_args()


async def _run(room_id: str, batch_size: int) -> None:
    """执行一次有界批量重建并输出机器可读计数。"""

    if batch_size < 1:
        raise ValueError("batch-size 必须大于 0")
    report = await memory_projection_supervisor.rebuild_room(
        room_id,
        batch_size=batch_size,
    )
    print(json.dumps(report.__dict__, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    args = _arguments()
    asyncio.run(_run(args.room_id, args.batch_size))
