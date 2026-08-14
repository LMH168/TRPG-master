"""新增可靠回合、提交回执与叙事 Outbox 持久化结构。

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建四张新表，并为既有记录增加可空 turn_id 关联。"""

    op.create_table(
        "turn_records",
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("client_action_id", sa.String(200), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("player_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("request_schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("phase_version", sa.Integer(), nullable=False),
        sa.Column("resume_point", sa.String(30), nullable=False),
        sa.Column("waiting_reason", sa.String(30), nullable=False),
        sa.Column("commit_state", sa.String(30), nullable=False),
        sa.Column("recovery_action", sa.String(30), nullable=False),
        sa.Column("error_schema_version", sa.Integer(), nullable=True),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("result_schema_version", sa.Integer(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("lease_owner", sa.String(200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("phase_version >= 1", name="ck_turn_records_phase_version"),
        sa.CheckConstraint(
            "status IN ('received', 'planning', 'adjudicating', 'executing', "
            "'awaiting_narration', 'delivering', 'completed', 'failed', 'cancelled')",
            name="ck_turn_records_status",
        ),
        sa.CheckConstraint(
            "commit_state IN ('not_committed', 'partially_committed', 'committed')",
            name="ck_turn_records_commit_state",
        ),
        sa.CheckConstraint(
            "resume_point IN ('planning', 'adjudicating', 'executing', 'narrating', "
            "'delivering', 'awaiting_player', 'none')",
            name="ck_turn_records_resume_point",
        ),
        sa.CheckConstraint(
            "waiting_reason IN ('skill_choice', 'post_roll_decision', 'none')",
            name="ck_turn_records_waiting_reason",
        ),
        sa.CheckConstraint(
            "recovery_action IN ('wait', 'retry_same_input', 'choose_skill', "
            "'choose_post_roll', 'fetch_result', 'submit_new_input', 'none')",
            name="ck_turn_records_recovery_action",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
            name="ck_turn_records_complete_lease",
        ),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("turn_id"),
        sa.UniqueConstraint(
            "room_id", "client_action_id", name="uq_turn_records_room_client_action"
        ),
    )
    op.create_index(
        "ix_turn_records_room_player_created",
        "turn_records",
        ["room_id", "player_id", "created_at"],
    )
    op.create_index("ix_turn_records_room_status", "turn_records", ["room_id", "status"])
    op.create_index("ix_turn_records_lease", "turn_records", ["status", "lease_expires_at"])

    op.create_table(
        "room_turn_reservations",
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["turn_records.turn_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("room_id"),
        sa.UniqueConstraint("turn_id", name="uq_room_turn_reservations_turn"),
    )
    op.create_table(
        "turn_commit_receipts",
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("engine_request_id", sa.String(200), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("action_request_id", sa.String(200), nullable=False),
        sa.Column("committed_state_version", sa.BigInteger(), nullable=False),
        sa.Column("first_event_sequence", sa.BigInteger(), nullable=True),
        sa.Column("last_event_sequence", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "committed_state_version >= 0",
            name="ck_turn_commit_receipts_state_version",
        ),
        sa.CheckConstraint(
            "(first_event_sequence IS NULL) = (last_event_sequence IS NULL)",
            name="ck_turn_commit_receipts_complete_event_range",
        ),
        sa.CheckConstraint(
            "first_event_sequence IS NULL OR first_event_sequence <= last_event_sequence",
            name="ck_turn_commit_receipts_ordered_event_range",
        ),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["turn_records.turn_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("room_id", "engine_request_id", name="pk_turn_commit_receipts"),
    )
    op.create_index(
        "ix_turn_commit_receipts_turn",
        "turn_commit_receipts",
        ["turn_id", "created_at"],
    )
    op.create_table(
        "narration_outbox",
        sa.Column("outbox_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("player_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.String(200), nullable=False),
        sa.Column("message_type", sa.String(50), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False),
        sa.Column("payload_schema_version", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "visibility IN ('public', 'player_scoped')",
            name="ck_narration_outbox_visibility",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'dispatched', 'dead_letter')",
            name="ck_narration_outbox_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_narration_outbox_attempt_count"),
        sa.CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
            name="ck_narration_outbox_complete_lease",
        ),
        sa.ForeignKeyConstraint(["turn_id"], ["turn_records.turn_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("outbox_id"),
        sa.UniqueConstraint("message_id"),
        sa.UniqueConstraint("turn_id", "message_type", name="uq_narration_outbox_turn_type"),
    )
    op.create_index("ix_narration_outbox_due", "narration_outbox", ["status", "next_attempt_at"])
    op.create_index(
        "ix_narration_outbox_room_player",
        "narration_outbox",
        ["room_id", "player_id", "created_at"],
    )

    # 使用 batch 模式让 SQLite 也真实创建外键；所有历史行保持 NULL。
    for table_name in ("action_plan_runs", "events", "game_events"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(sa.Column("turn_id", sa.Uuid(), nullable=True))
            batch_op.create_foreign_key(
                f"fk_{table_name}_turn_id",
                "turn_records",
                ["turn_id"],
                ["turn_id"],
            )
            batch_op.create_index(f"ix_{table_name}_turn_id", ["turn_id"])


def downgrade() -> None:
    """移除可靠回合结构，保留既有业务表与历史数据。"""

    for table_name in ("game_events", "events", "action_plan_runs"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_index(f"ix_{table_name}_turn_id")
            batch_op.drop_constraint(f"fk_{table_name}_turn_id", type_="foreignkey")
            batch_op.drop_column("turn_id")

    op.drop_index("ix_narration_outbox_room_player", table_name="narration_outbox")
    op.drop_index("ix_narration_outbox_due", table_name="narration_outbox")
    op.drop_table("narration_outbox")
    op.drop_index("ix_turn_commit_receipts_turn", table_name="turn_commit_receipts")
    op.drop_table("turn_commit_receipts")
    op.drop_table("room_turn_reservations")
    op.drop_index("ix_turn_records_lease", table_name="turn_records")
    op.drop_index("ix_turn_records_room_status", table_name="turn_records")
    op.drop_index("ix_turn_records_room_player_created", table_name="turn_records")
    op.drop_table("turn_records")
