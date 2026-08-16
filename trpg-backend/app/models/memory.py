"""跨回合 Memory 读模型及其可恢复投影运行状态 ORM。"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class MemoryProjectionRunRecord(Base):
    """一个可靠 Turn 的幂等 Memory 投影游标和 worker lease。"""

    __tablename__ = "memory_projection_runs"
    __table_args__ = (
        CheckConstraint("schema_version = 1", name="ck_memory_runs_schema_version"),
        CheckConstraint("projection_version = 1", name="ck_memory_runs_projection_version"),
        CheckConstraint("version >= 1", name="ck_memory_runs_version"),
        CheckConstraint("attempt_count >= 0", name="ck_memory_runs_attempt_count"),
        CheckConstraint(
            "status IN ('pending', 'leased', 'completed', 'retryable_failure', 'dead_letter')",
            name="ck_memory_runs_status",
        ),
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
            name="ck_memory_runs_complete_lease",
        ),
        CheckConstraint(
            "status = 'leased' OR lease_owner IS NULL",
            name="ck_memory_runs_lease_status",
        ),
        CheckConstraint(
            "status NOT IN ('completed', 'dead_letter') OR completed_at IS NOT NULL",
            name="ck_memory_runs_terminal_time",
        ),
        CheckConstraint(
            "status NOT IN ('retryable_failure', 'dead_letter') OR last_error_code IS NOT NULL",
            name="ck_memory_runs_failure_error",
        ),
        Index("ix_memory_runs_due", "status", "next_attempt_at"),
        Index("ix_memory_runs_room_status", "room_id", "status", "updated_at"),
    )

    turn_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("turn_records.turn_id", ondelete="CASCADE"),
        primary_key=True,
    )
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    projection_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryEntryRecord(Base):
    """有稳定来源、玩家可见性和认知等级的派生 Memory。"""

    __tablename__ = "memory_entries"
    __table_args__ = (
        UniqueConstraint("room_id", "memory_id", name="uq_memory_entries_room_id"),
        ForeignKeyConstraint(
            ["room_id", "superseded_by"],
            ["memory_entries.room_id", "memory_entries.memory_id"],
            name="fk_memory_entries_superseded_same_room",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("schema_version = 1", name="ck_memory_entries_schema_version"),
        CheckConstraint("projection_version = 1", name="ck_memory_entries_projection_version"),
        CheckConstraint("source_ordinal >= 0", name="ck_memory_entries_source_ordinal"),
        CheckConstraint(
            "kind IN ('completed_action', 'location_visit', 'discovered_information', "
            "'world_event', 'conversation', 'relationship_change', 'unresolved_goal')",
            name="ck_memory_entries_kind",
        ),
        CheckConstraint(
            "scope IN ('campaign', 'player', 'entity')",
            name="ck_memory_entries_scope",
        ),
        CheckConstraint(
            "visibility IN ('public', 'player_scoped')",
            name="ck_memory_entries_visibility",
        ),
        CheckConstraint(
            "epistemic_status IN ('confirmed', 'experienced', 'heard', 'asserted', "
            "'presentation_only')",
            name="ck_memory_entries_epistemic_status",
        ),
        CheckConstraint(
            "(scope = 'campaign' AND scope_owner_id IS NULL) OR "
            "(scope != 'campaign' AND scope_owner_id IS NOT NULL)",
            name="ck_memory_entries_scope_owner",
        ),
        CheckConstraint(
            "(visibility = 'public' AND viewer_player_id IS NULL) OR "
            "(visibility = 'player_scoped' AND viewer_player_id IS NOT NULL)",
            name="ck_memory_entries_viewer",
        ),
        CheckConstraint(
            "source_kind != 'game_event' OR "
            "(source_event_id IS NOT NULL AND source_sequence IS NOT NULL)",
            name="ck_memory_entries_event_source",
        ),
        CheckConstraint(
            "superseded_by IS NULL OR superseded_by != memory_id",
            name="ck_memory_entries_not_self_superseded",
        ),
        Index(
            "ix_memory_entries_context",
            "room_id",
            "visibility",
            "viewer_player_id",
            "scope",
            "scope_owner_id",
            "created_at",
        ),
        Index("ix_memory_entries_subject", "room_id", "subject_id", "created_at"),
        Index("ix_memory_entries_location", "room_id", "location_id", "created_at"),
        Index("ix_memory_entries_source_turn", "source_turn_id", "source_ordinal"),
        Index("ix_memory_entries_topic", "room_id", "topic_key"),
    )

    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False
    )
    source_turn_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("memory_projection_runs.turn_id", ondelete="CASCADE"),
        nullable=False,
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    projection_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(200), nullable=False)
    object_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    location_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_owner_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    visibility: Mapped[str] = mapped_column(String(20), nullable=False)
    viewer_player_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("players.id", ondelete="CASCADE"), nullable=True
    )
    epistemic_status: Mapped[str] = mapped_column(String(30), nullable=False)
    topic_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    search_text: Mapped[str] = mapped_column(String(4000), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    projected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
