"""消费 Narration Outbox，并按固定顺序执行至少一次 WebSocket 投递。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import anyio
from collaboration_framework.host.application import split_narration_chunks

from app.core.turn_runtime import NarrationOutboxMessage, TurnRuntimeStore
from app.service.ws_manager import ConnectionManager


class TurnOutboxDispatcher:
    """领取到期消息；稳定 payload 可在失败或重启后安全重发。"""

    def __init__(
        self,
        store: TurnRuntimeStore,
        manager: ConnectionManager,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 30,
        retry_seconds: int = 5,
    ) -> None:
        self._store = store
        self._manager = manager
        self._worker_id = worker_id or f"outbox-worker-{uuid4().hex}"
        self._lease_seconds = lease_seconds
        self._retry_seconds = retry_seconds

    async def dispatch_due(self, *, limit: int = 20) -> int:
        """投递一批到期消息，返回本次完成处理的消息数。"""

        now = datetime.now(UTC)
        messages = await self._store.claim_due_outbox(
            worker_id=self._worker_id,
            now=now,
            lease_expires_at=now + timedelta(seconds=self._lease_seconds),
            limit=limit,
        )
        for message in messages:
            await self._dispatch_claimed(message)
        return len(messages)

    async def _dispatch_claimed(self, message: NarrationOutboxMessage) -> None:
        """严格发送 chunk*、narration、view、completed；任一失败都保留重试。"""

        now = datetime.now(UTC)
        next_attempt = now + timedelta(seconds=self._retry_seconds)
        try:
            frames = self._frames(message)
            recipient_count: int | None = None
            failed = False
            for frame in frames:
                stats = await self._manager.deliver(
                    room_id=message.room_id,
                    player_id=message.player_id,
                    player_scoped=message.visibility == "player_scoped",
                    message=frame,
                )
                recipient_count = stats.recipient_count
                if stats.failure_count:
                    failed = True
                    break
                if stats.recipient_count == 0:
                    break
            if recipient_count == 0:
                outcome = "no_recipient"
                error_code = None
            elif failed:
                outcome = "failed"
                error_code = "WS_SEND_FAILED"
            else:
                outcome = "dispatched"
                error_code = None
        except Exception:
            outcome = "failed"
            error_code = "OUTBOX_PAYLOAD_INVALID"
        # 客户端可能在收到最后一帧后立刻断开并取消 WebSocket handler。结算必须在
        # 屏蔽取消的短事务中完成，否则数据库 lease 会一直保留到超时，SQLite 测试
        # 还可能留下未完成写事务；这里不屏蔽发送，只保护已发送结果的持久化收尾。
        with anyio.CancelScope(shield=True):
            await self._store.settle_outbox(
                outbox_id=message.outbox_id,
                worker_id=self._worker_id,
                outcome=outcome,
                now=now,
                next_attempt_at=next_attempt,
                error_code=error_code,
            )

    @staticmethod
    def _frames(message: NarrationOutboxMessage) -> tuple[dict, ...]:
        """只从持久化 bundle 构造帧，不重新调用 Narrator 或读取隐藏状态。"""

        narration = message.payload.get("narration")
        completion = message.payload.get("completion")
        if not isinstance(narration, dict) or not isinstance(completion, dict):
            raise ValueError("Outbox payload 缺少 narration/completion")
        text = narration.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("Outbox narration 缺少文本")
        chunks = split_narration_chunks(text)
        frames: list[dict] = [
            {
                "type": "narration.chunk",
                "payload": {
                    "messageId": message.message_id,
                    "sequence": index,
                    "text": chunk,
                },
            }
            for index, chunk in enumerate(chunks)
        ]
        frames.extend(
            (
                {"type": "narration.push", "payload": narration},
                {
                    "type": "view.updated",
                    "payload": {
                        "playerId": completion["playerId"],
                        "playerView": completion["playerView"],
                    },
                },
                {
                    "protocol_version": "1",
                    "message_type": "turn.completed",
                    "correlation_id": completion["clientActionId"],
                    "turn_id": completion["turnId"],
                    "payload": completion,
                },
            )
        )
        return tuple(frames)


__all__ = ["TurnOutboxDispatcher"]
