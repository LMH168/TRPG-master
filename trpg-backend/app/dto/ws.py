"""基础房间 WebSocket 协议；AI 主持事件将在新 runtime 中重新定义。"""

from typing import Any

from pydantic import Field, field_validator

from app.dto.common import CamelModel, UtcDatetime
from app.dto.room import RoomPlayerRead


class RoomJoinPayload(CamelModel):
    """使用房间重连凭证绑定当前 WebSocket 身份。"""

    reconnect_token: str = Field(..., min_length=1)
    room_code: str | None = None
    nickname: str | None = None


class PlayerReadyPayload(CamelModel):
    """设置玩家准备状态。"""

    ready: bool


class GameStartPayload(CamelModel):
    """请求从建卡阶段进入基础游戏页面。"""


class ActionSubmitPayload(CamelModel):
    """保留前端提交形状；新 GM Agent 接入前由服务端明确拒绝。"""

    client_action_id: str = Field(..., min_length=1, max_length=200)
    utterance: str = Field(..., min_length=1, max_length=2000)

    @field_validator("client_action_id", "utterance")
    @classmethod
    def strip_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("不能为空")
        return stripped


class ChatSendPayload(CamelModel):
    """玩家讨论区消息，不进入任何模型上下文。"""

    text: str = Field(..., min_length=1, max_length=2000)
    client_message_id: str = Field(..., min_length=1, max_length=64)


class SessionBoundPayload(CamelModel):
    room_id: str
    player_id: str


class ChatMessagePayload(CamelModel):
    message_id: str
    player_id: str
    nickname: str
    text: str
    sent_at: UtcDatetime
    client_message_id: str


class RoomStatePayload(CamelModel):
    room_id: str
    phase: str
    players: list[RoomPlayerRead]


class ErrorPayload(CamelModel):
    code: str
    message: str
    correlation_id: str | None = None


class ClientEnvelope(CamelModel):
    """客户端消息信封；具体 payload 由事件分支二次校验。"""

    type: str
    player_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ServerEnvelope(CamelModel):
    """服务端消息信封。"""

    type: str
    payload: dict[str, Any]
