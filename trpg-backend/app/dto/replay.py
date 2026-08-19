"""房间讨论消息恢复所需的兼容响应模型。"""

from typing import Literal

from app.dto.common import CamelModel, UtcDatetime


class RoomConversationEventRead(CamelModel):
    """GET /api/v1/rooms/{roomId}/conversation 返回项。

    讨论区来自 ChatMessage，GM 行动频道由 TurnRun 和 CommandReceipt 投影。
    """

    id: str
    type: Literal["chat.message", "action.broadcast", "narration.push", "check.result"]
    channel: Literal["discussion", "action"]
    payload: dict
    created_at: UtcDatetime
