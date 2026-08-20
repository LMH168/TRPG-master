"""为 GM 检定增加可恢复的 CoC7 扩展状态。

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加单个 JSON 字段保存奖惩骰、幸运与强推状态。"""

    op.add_column(
        "gm_check_runs",
        sa.Column("details_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    """移除检定扩展状态字段。"""

    op.drop_column("gm_check_runs", "details_json")
