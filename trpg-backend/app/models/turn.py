"""可靠回合协议的 ORM 模型。

本文件持久化统一回合身份、房间活动回合占用、Engine 提交回执和最终叙事
Outbox。它只提供数据库事实，不负责推进回合状态或发送 WebSocket 消息。
"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class TurnRecordModel(Base):
    """一次玩家输入从接收到终态的统一持久化记录。"""

    __tablename__ = "turn_records"
    __table_args__ = (
        UniqueConstraint(
            "room_id",
            "client_action_id",
            name="uq_turn_records_room_client_action",
        ),
        CheckConstraint("phase_version >= 1", name="ck_turn_records_phase_version"),
        CheckConstraint(
            "status IN ('received', 'planning', 'adjudicating', 'executing', "
            "'awaiting_narration', 'delivering', 'completed', 'failed', 'cancelled')",
            name="ck_turn_records_status",
        ),
        CheckConstraint(
            "commit_state IN ('not_committed', 'partially_committed', 'committed')",
            name="ck_turn_records_commit_state",
        ),
        CheckConstraint(
            "resume_point IN ('planning', 'adjudicating', 'executing', 'narrating', "
            "'delivering', 'awaiting_player', 'none')",
            name="ck_turn_records_resume_point",
        ),
        CheckConstraint(
            "waiting_reason IN ('skill_choice', 'post_roll_decision', 'none')",
            name="ck_turn_records_waiting_reason",
        ),
        CheckConstraint(
            "recovery_action IN ('wait', 'retry_same_input', 'choose_skill', "
            "'choose_post_roll', 'fetch_result', 'submit_new_input', 'none')",
            name="ck_turn_records_recovery_action",
        ),
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
            name="ck_turn_records_complete_lease",
        ),
        Index("ix_turn_records_room_player_created", "room_id", "player_id", "created_at"),
        Index("ix_turn_records_room_status", "room_id", "status"),
        Index("ix_turn_records_lease", "status", "lease_expires_at"),
    )

    turn_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False
    )
    client_action_id: Mapped[str] = mapped_column(String(200), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    player_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    request_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    request_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    orchestration_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    orchestration_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    phase_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resume_point: Mapped[str] = mapped_column(String(30), nullable=False)
    waiting_reason: Mapped[str] = mapped_column(String(30), nullable=False)
    commit_state: Mapped[str] = mapped_column(String(30), nullable=False)
    recovery_action: Mapped[str] = mapped_column(String(30), nullable=False)
    pending_decision_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RoomTurnReservation(Base):
    """数据库级房间占用，保证每个房间最多一个活动回合。"""

    __tablename__ = "room_turn_reservations"
    __table_args__ = (UniqueConstraint("turn_id", name="uq_room_turn_reservations_turn"),)

    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id", ondelete="CASCADE"), primary_key=True
    )
    turn_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("turn_records.turn_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class TurnCommitReceiptRecord(Base):
    """Engine 权威事务写入的提交证明，允许命令不产生 DomainEvent。"""

    __tablename__ = "turn_commit_receipts"
    __table_args__ = (
        PrimaryKeyConstraint("room_id", "engine_request_id", name="pk_turn_commit_receipts"),
        CheckConstraint(
            "committed_state_version >= 0",
            name="ck_turn_commit_receipts_state_version",
        ),
        CheckConstraint(
            "(first_event_sequence IS NULL) = (last_event_sequence IS NULL)",
            name="ck_turn_commit_receipts_complete_event_range",
        ),
        CheckConstraint(
            "first_event_sequence IS NULL OR first_event_sequence <= last_event_sequence",
            name="ck_turn_commit_receipts_ordered_event_range",
        ),
        Index("ix_turn_commit_receipts_turn", "turn_id", "created_at"),
    )

    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False
    )
    engine_request_id: Mapped[str] = mapped_column(String(200), nullable=False)
    turn_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("turn_records.turn_id", ondelete="CASCADE"),
        nullable=False,
    )
    action_request_id: Mapped[str] = mapped_column(String(200), nullable=False)
    committed_state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_event_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_event_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NarrationOutboxRecord(Base):
    """最终叙事的至少一次投递记录；稳定 payload 可安全重复发送。"""

    __tablename__ = "narration_outbox"
    __table_args__ = (
        UniqueConstraint("turn_id", "message_type", name="uq_narration_outbox_turn_type"),
        CheckConstraint(
            "visibility IN ('public', 'player_scoped')",
            name="ck_narration_outbox_visibility",
        ),
        CheckConstraint(
            "status IN ('pending', 'leased', 'dispatched', 'dead_letter')",
            name="ck_narration_outbox_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_narration_outbox_attempt_count"),
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
            name="ck_narration_outbox_complete_lease",
        ),
        Index("ix_narration_outbox_due", "status", "next_attempt_at"),
        Index("ix_narration_outbox_room_player", "room_id", "player_id", "created_at"),
    )

    outbox_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    turn_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("turn_records.turn_id", ondelete="CASCADE"),
        nullable=False,
    )
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    message_type: Mapped[str] = mapped_column(String(50), nullable=False)
    visibility: Mapped[str] = mapped_column(String(20), nullable=False)
    payload_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
