"""建立 Phase 0 新 GM 运行时的 Actor、回合、回执和 Outbox 表。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "ea1b2c3d4f50"
down_revision: str | Sequence[str] | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建新运行时的最小持久化边界。"""

    op.create_table(
        "module_versions",
        sa.Column("module_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("world_ref", sa.String(200), nullable=False),
        sa.Column("content_schema_version", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["module_id"], ["scenarios.id"]),
        sa.PrimaryKeyConstraint("module_id", "version"),
    )
    op.create_table(
        "game_sessions",
        sa.Column("room_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("module_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("module_version", sa.String(50), nullable=False),
        sa.Column("state_schema_version", sa.Integer(), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"]),
        sa.ForeignKeyConstraint(
            ["module_id", "module_version"],
            ["module_versions.module_id", "module_versions.version"],
        ),
        sa.PrimaryKeyConstraint("room_id"),
    )
    op.create_table(
        "gm_actors",
        sa.Column("id", sa.String(100), nullable=False),
        sa.Column("room_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("player_id", sa.String(100), nullable=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("location_id", sa.String(100), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["game_sessions.room_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "gm_turn_runs",
        sa.Column("id", sa.String(100), nullable=False),
        sa.Column("room_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("client_request_id", sa.String(200), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("expected_revision", sa.BigInteger(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["game_sessions.room_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_request_id", name="uq_gm_turn_runs_request"),
    )
    op.create_table(
        "gm_events",
        sa.Column("id", sa.String(100), nullable=False),
        sa.Column("room_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("client_request_id", sa.String(200), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["game_sessions.room_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_id", "sequence", name="uq_gm_events_sequence"),
    )
    op.create_table(
        "gm_command_receipts",
        sa.Column("room_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("client_request_id", sa.String(200), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["game_sessions.room_id"]),
        sa.PrimaryKeyConstraint("room_id", "client_request_id"),
    )
    op.create_table(
        "gm_outbox_messages",
        sa.Column("id", sa.String(100), nullable=False),
        sa.Column("room_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("event_id", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["game_sessions.room_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_gm_outbox_event"),
    )


def downgrade() -> None:
    """拒绝自动降级，避免破坏已产生的权威运行记录。"""

    raise RuntimeError(
        "旧 AI 主持运行时清理不可逆；Phase 0 GM 运行时迁移不可逆，请使用备份恢复数据库"
    )
