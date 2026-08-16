"""为 RuleAgenda 增加可对账的步骤执行证明。

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a5b6c7d8e9f0"
down_revision: str | Sequence[str] | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建步骤执行表，并强制每条记录绑定真实 Turn receipt。"""

    op.create_table(
        "agenda_step_executions",
        sa.Column("execution_id", sa.String(length=64), nullable=False),
        sa.Column("room_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("origin_turn_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("execution_turn_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("agenda_id", sa.String(length=200), nullable=False),
        sa.Column("source_event_id", sa.String(length=200), nullable=False),
        sa.Column("rule_id", sa.String(length=100), nullable=False),
        sa.Column("branch_id", sa.String(length=100), nullable=False),
        sa.Column("step_id", sa.String(length=100), nullable=False),
        sa.Column("execution_kind", sa.String(length=40), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("request_schema_version", sa.Integer(), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("result_schema_version", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("committed_state_version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("schema_version = 1", name="ck_agenda_step_executions_schema_version"),
        sa.CheckConstraint(
            "request_schema_version >= 1",
            name="ck_agenda_step_executions_request_schema_version",
        ),
        sa.CheckConstraint(
            "result_schema_version >= 1",
            name="ck_agenda_step_executions_result_schema_version",
        ),
        sa.CheckConstraint(
            "committed_state_version >= 0",
            name="ck_agenda_step_executions_state_version",
        ),
        sa.CheckConstraint(
            "execution_kind IN ('passive_check', 'adjudicated_check', "
            "'ruleset_action', 'npc_opportunity', 'presentation', 'effect_segment')",
            name="ck_agenda_step_executions_kind",
        ),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["rooms.id"],
            name="fk_agenda_step_executions_room",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["origin_turn_id"],
            ["turn_records.turn_id"],
            name="fk_agenda_step_executions_origin_turn",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_turn_id"],
            ["turn_records.turn_id"],
            name="fk_agenda_step_executions_execution_turn",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["room_id", "execution_id"],
            ["turn_commit_receipts.room_id", "turn_commit_receipts.engine_request_id"],
            name="fk_agenda_step_executions_receipt",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("execution_id", name="pk_agenda_step_executions"),
        sa.UniqueConstraint(
            "room_id",
            "execution_id",
            name="uq_agenda_step_executions_room_execution",
        ),
    )
    op.create_index(
        "ix_agenda_step_executions_agenda",
        "agenda_step_executions",
        ["room_id", "agenda_id", "created_at"],
    )
    op.create_index(
        "ix_agenda_step_executions_execution_turn",
        "agenda_step_executions",
        ["execution_turn_id", "created_at"],
    )


def downgrade() -> None:
    """删除派生步骤证明，不修改现有 Turn receipt 或 GameState。"""

    op.drop_index(
        "ix_agenda_step_executions_execution_turn",
        table_name="agenda_step_executions",
    )
    op.drop_index(
        "ix_agenda_step_executions_agenda",
        table_name="agenda_step_executions",
    )
    op.drop_table("agenda_step_executions")
