"""房间讨论消息恢复所需的兼容响应模型。"""

from typing import Literal

from app.dto.common import CamelModel, UtcDatetime


class RoomConversationEventRead(CamelModel):
    """GET /api/v1/rooms/{roomId}/conversation 返回项。

    当前只承载讨论区消息；行动频道将在新 GM Agent 协议中重新定义。
    """

    id: str
    type: Literal["chat.message"]
    channel: Literal["discussion"]
    payload: dict
    created_at: UtcDatetime
