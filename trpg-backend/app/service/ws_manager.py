"""房间级 WebSocket 连接登记表（issue #60）。

只负责"这个房间当前有哪些连接、往它们广播一条消息"，不关心业务逻辑。
玩家列表、准备、建卡和房间阶段均由 service/room.py 通过数据库读写。
"""

import contextlib
from dataclasses import dataclass
from typing import Protocol


class WebSocketSender(Protocol):
    """连接表只依赖 JSON 发送能力，便于可靠投递使用测试替身。"""

    async def send_json(self, message: dict, /) -> None: ...


@dataclass(frozen=True)
class DeliveryStats:
    """一次进程内投递的目标、成功与失败连接数。"""

    recipient_count: int
    success_count: int
    failure_count: int


class ConnectionManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocketSender]] = {}
        self._players: dict[tuple[str, str], set[WebSocketSender]] = {}
        self._bindings: dict[WebSocketSender, tuple[str, str] | None] = {}

    def add(
        self,
        room_id: str,
        websocket: WebSocketSender,
        player_id: str | None = None,
    ) -> None:
        """登记房间连接；完成身份绑定后同时建立 player-scoped 索引。"""

        self._rooms.setdefault(room_id, set()).add(websocket)
        binding = (room_id, player_id) if player_id is not None else None
        self._bindings[websocket] = binding
        if binding is not None:
            self._players.setdefault(binding, set()).add(websocket)

    def remove(self, room_id: str, websocket: WebSocketSender) -> None:
        binding = self._bindings.pop(websocket, None)
        if binding is not None:
            player_connections = self._players.get(binding)
            if player_connections is not None:
                player_connections.discard(websocket)
                if not player_connections:
                    del self._players[binding]
        connections = self._rooms.get(room_id)
        if connections is None:
            return
        connections.discard(websocket)
        if not connections:
            del self._rooms[room_id]

    async def broadcast(self, room_id: str, message: dict) -> None:
        # 复制一份快照再遍历：广播过程中某个连接掉线触发 remove() 会改动
        # 原集合，直接遍历原集合会撞上"运行时改变集合大小"的异常。
        for websocket in list(self._rooms.get(room_id, ())):
            # 发送失败（连接已经断了但还没走到 disconnect 清理）忽略，
            # 交给该连接自己的 receive 循环去 remove()。
            with contextlib.suppress(Exception):
                await websocket.send_json(message)

    async def deliver(
        self,
        *,
        room_id: str,
        player_id: str,
        player_scoped: bool,
        message: dict,
    ) -> DeliveryStats:
        """向房间或指定玩家投递一帧，并把真实发送失败反馈给 Outbox。"""

        targets = list(
            self._players.get((room_id, player_id), ())
            if player_scoped
            else self._rooms.get(room_id, ())
        )
        succeeded = 0
        failed = 0
        for websocket in targets:
            try:
                await websocket.send_json(message)
                succeeded += 1
            except Exception:
                failed += 1
        return DeliveryStats(
            recipient_count=len(targets),
            success_count=succeeded,
            failure_count=failed,
        )


manager = ConnectionManager()
