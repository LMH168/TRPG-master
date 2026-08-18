"""为可靠 Turn 增加可恢复的 GM 编排索引。

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: str | Sequence[str] | None = "b6c7d8e9f0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增可空字段；旧 Turn 保持未知模式，由权威执行证明保守收养。"""

    op.add_column(
        "turn_records",
        sa.Column("orchestration_schema_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "turn_records",
        sa.Column("orchestration_json", sa.JSON(), nullable=True),
    )
    with op.batch_alter_table("turn_records") as batch_op:
        batch_op.create_check_constraint(
            "ck_turn_records_complete_orchestration",
            "(orchestration_schema_version IS NULL) = (orchestration_json IS NULL)",
        )
        batch_op.create_check_constraint(
            "ck_turn_records_orchestration_version",
            "orchestration_schema_version IS NULL OR orchestration_schema_version = 1",
        )


def downgrade() -> None:
    """只移除派生编排索引，不改写 Turn、receipt、Event 或 Outbox。"""

    with op.batch_alter_table("turn_records") as batch_op:
        batch_op.drop_constraint(
            "ck_turn_records_orchestration_version",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_turn_records_complete_orchestration",
            type_="check",
        )
        batch_op.drop_column("orchestration_json")
        batch_op.drop_column("orchestration_schema_version")
