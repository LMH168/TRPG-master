"""新 AI 主持运行时的 PostgreSQL 权威表映射。

这些表只服务于新 GM Kernel，不复活旧 ActionPlan/Goal/Ending 运行时。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class GameSession(Base):
    """冻结模组版本并保存当前权威状态修订号的游戏会话。"""

    __tablename__ = "game_sessions"

    room_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    module_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), nullable=False)
    module_version: Mapped[str] = mapped_column(String(50), nullable=False)
    state_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ModuleVersion(Base):
    """已安装的结构化模组版本快照。"""

    __tablename__ = "module_versions"

    module_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    version: Mapped[str] = mapped_column(String(50), primary_key=True)
    world_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    content_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class RuntimeActor(Base):
    """一局中的调查员或 NPC 快照，和可复用 Character 分离。"""

    __tablename__ = "gm_actors"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("game_sessions.room_id"), nullable=False
    )
    player_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    location_id: Mapped[str] = mapped_column(String(100), nullable=False)
    state_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class TurnRun(Base):
    """记录一回合状态，允许模型失败后从稳定修订号恢复。"""

    __tablename__ = "gm_turn_runs"

    id: Mapped[str] = mapped_column(
        String(100), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("game_sessions.room_id"), nullable=False
    )
    client_request_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="collecting")
    expected_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class GameEvent(Base):
    """只追加的领域事件；事件序号在会话内单调递增。"""

    __tablename__ = "gm_events"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("game_sessions.room_id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, default="public")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class CommandReceipt(Base):
    """保存命令结果，重复请求直接返回该回执而不是再次执行。"""

    __tablename__ = "gm_command_receipts"

    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("game_sessions.room_id"), primary_key=True
    )
    client_request_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class OutboxMessage(Base):
    """事务内写入、事务外投递的可靠消息。"""

    __tablename__ = "gm_outbox_messages"

    id: Mapped[str] = mapped_column(
        String(100), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("game_sessions.room_id"), nullable=False
    )
    event_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
