"""为可靠回合增加玩家安全待决策快照。

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: str | Sequence[str] | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存刷新后恢复技能选择或掷骰后决策所需的公开快照。"""

    op.add_column(
        "turn_records",
        sa.Column("pending_decision_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """移除待决策快照；不影响已提交 Engine 状态与最终结果。"""

    op.drop_column("turn_records", "pending_decision_json")
