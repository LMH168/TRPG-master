"""为事件驱动长期记忆增加可重建投影表。

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f4a5b6c7d8e9"
down_revision: str | Sequence[str] | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 Memory 投影游标及玩家安全派生条目。"""

    op.create_table(
        "memory_projection_runs",
        sa.Column("turn_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("room_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("projection_version", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("schema_version = 1", name="ck_memory_runs_schema_version"),
        sa.CheckConstraint("projection_version = 1", name="ck_memory_runs_projection_version"),
        sa.CheckConstraint("version >= 1", name="ck_memory_runs_version"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_memory_runs_attempt_count"),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'completed', 'retryable_failure', 'dead_letter')",
            name="ck_memory_runs_status",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
            name="ck_memory_runs_complete_lease",
        ),
        sa.CheckConstraint(
            "status = 'leased' OR lease_owner IS NULL",
            name="ck_memory_runs_lease_status",
        ),
        sa.CheckConstraint(
            "status NOT IN ('completed', 'dead_letter') OR completed_at IS NOT NULL",
            name="ck_memory_runs_terminal_time",
        ),
        sa.CheckConstraint(
            "status NOT IN ('retryable_failure', 'dead_letter') OR last_error_code IS NOT NULL",
            name="ck_memory_runs_failure_error",
        ),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["rooms.id"],
            name="fk_memory_runs_room",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["turn_records.turn_id"],
            name="fk_memory_runs_turn",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("turn_id", name="pk_memory_projection_runs"),
    )
    op.create_index(
        "ix_memory_runs_due",
        "memory_projection_runs",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_memory_runs_room_status",
        "memory_projection_runs",
        ["room_id", "status", "updated_at"],
    )

    op.create_table(
        "memory_entries",
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column("room_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("source_turn_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("projection_version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("subject_id", sa.String(length=200), nullable=False),
        sa.Column("object_id", sa.String(length=200), nullable=True),
        sa.Column("location_id", sa.String(length=200), nullable=True),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.String(length=200), nullable=False),
        sa.Column("source_event_id", sa.String(length=200), nullable=True),
        sa.Column("source_sequence", sa.BigInteger(), nullable=True),
        sa.Column("source_ordinal", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("scope_owner_id", sa.String(length=200), nullable=True),
        sa.Column("visibility", sa.String(length=20), nullable=False),
        sa.Column("viewer_player_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("epistemic_status", sa.String(length=30), nullable=False),
        sa.Column("topic_key", sa.String(length=500), nullable=True),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("search_text", sa.String(length=4000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_by", sa.String(length=64), nullable=True),
        sa.CheckConstraint("schema_version = 1", name="ck_memory_entries_schema_version"),
        sa.CheckConstraint("projection_version = 1", name="ck_memory_entries_projection_version"),
        sa.CheckConstraint("source_ordinal >= 0", name="ck_memory_entries_source_ordinal"),
        sa.CheckConstraint(
            "kind IN ('completed_action', 'location_visit', 'discovered_information', "
            "'world_event', 'conversation', 'relationship_change', 'unresolved_goal')",
            name="ck_memory_entries_kind",
        ),
        sa.CheckConstraint(
            "scope IN ('campaign', 'player', 'entity')",
            name="ck_memory_entries_scope",
        ),
        sa.CheckConstraint(
            "visibility IN ('public', 'player_scoped')",
            name="ck_memory_entries_visibility",
        ),
        sa.CheckConstraint(
            "epistemic_status IN ('confirmed', 'experienced', 'heard', 'asserted', "
            "'presentation_only')",
            name="ck_memory_entries_epistemic_status",
        ),
        sa.CheckConstraint(
            "(scope = 'campaign' AND scope_owner_id IS NULL) OR "
            "(scope != 'campaign' AND scope_owner_id IS NOT NULL)",
            name="ck_memory_entries_scope_owner",
        ),
        sa.CheckConstraint(
            "(visibility = 'public' AND viewer_player_id IS NULL) OR "
            "(visibility = 'player_scoped' AND viewer_player_id IS NOT NULL)",
            name="ck_memory_entries_viewer",
        ),
        sa.CheckConstraint(
            "source_kind != 'game_event' OR "
            "(source_event_id IS NOT NULL AND source_sequence IS NOT NULL)",
            name="ck_memory_entries_event_source",
        ),
        sa.CheckConstraint(
            "superseded_by IS NULL OR superseded_by != memory_id",
            name="ck_memory_entries_not_self_superseded",
        ),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["rooms.id"],
            name="fk_memory_entries_room",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_turn_id"],
            ["memory_projection_runs.turn_id"],
            name="fk_memory_entries_projection_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["viewer_player_id"],
            ["players.id"],
            name="fk_memory_entries_viewer_player",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["room_id", "superseded_by"],
            ["memory_entries.room_id", "memory_entries.memory_id"],
            name="fk_memory_entries_superseded_same_room",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("memory_id", name="pk_memory_entries"),
        sa.UniqueConstraint("room_id", "memory_id", name="uq_memory_entries_room_id"),
    )
    op.create_index(
        "ix_memory_entries_context",
        "memory_entries",
        [
            "room_id",
            "visibility",
            "viewer_player_id",
            "scope",
            "scope_owner_id",
            "created_at",
        ],
    )
    op.create_index(
        "ix_memory_entries_subject",
        "memory_entries",
        ["room_id", "subject_id", "created_at"],
    )
    op.create_index(
        "ix_memory_entries_location",
        "memory_entries",
        ["room_id", "location_id", "created_at"],
    )
    op.create_index(
        "ix_memory_entries_source_turn",
        "memory_entries",
        ["source_turn_id", "source_ordinal"],
    )
    op.create_index(
        "ix_memory_entries_topic",
        "memory_entries",
        ["room_id", "topic_key"],
    )


def downgrade() -> None:
    """删除可重建 Memory 读模型，不触碰 Turn、Event 或 Engine 状态。"""

    op.drop_index("ix_memory_entries_topic", table_name="memory_entries")
    op.drop_index("ix_memory_entries_source_turn", table_name="memory_entries")
    op.drop_index("ix_memory_entries_location", table_name="memory_entries")
    op.drop_index("ix_memory_entries_subject", table_name="memory_entries")
    op.drop_index("ix_memory_entries_context", table_name="memory_entries")
    op.drop_table("memory_entries")
    op.drop_index("ix_memory_runs_room_status", table_name="memory_projection_runs")
    op.drop_index("ix_memory_runs_due", table_name="memory_projection_runs")
    op.drop_table("memory_projection_runs")
