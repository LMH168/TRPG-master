"""冻结 GM 内容版本并保存模型上下文审计快照。

Revision ID: c3d4e5f6a7b8
Revises: c2d3e4f5a6b7
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加内容冻结和上下文审计字段，并为旧空值提供明确基线。"""

    op.add_column("module_versions", sa.Column("content_hash", sa.String(64), nullable=True))
    op.add_column("game_sessions", sa.Column("content_hash", sa.String(64), nullable=True))
    op.add_column("game_sessions", sa.Column("ruleset_version", sa.String(50), nullable=True))
    op.add_column("game_sessions", sa.Column("ruleset_profile", sa.String(100), nullable=True))
    op.add_column("gm_turn_runs", sa.Column("context_json", sa.JSON(), nullable=True))
    # 历史值明确标为未经校验，不能伪装成 v2 内容哈希。
    op.execute("UPDATE module_versions SET content_hash = 'legacy-unverified'")
    op.execute(
        "UPDATE game_sessions SET content_hash = 'legacy-unverified', "
        "ruleset_version = 'legacy', ruleset_profile = 'legacy'"
    )
    with op.batch_alter_table("module_versions") as batch:
        batch.alter_column("content_hash", nullable=False)
    with op.batch_alter_table("game_sessions") as batch:
        batch.alter_column("content_hash", nullable=False)
        batch.alter_column("ruleset_version", nullable=False)
        batch.alter_column("ruleset_profile", nullable=False)


def downgrade() -> None:
    """移除本次新增字段，让更早的不可逆清理迁移继续守住降级边界。"""

    with op.batch_alter_table("game_sessions") as batch:
        batch.drop_column("ruleset_profile")
        batch.drop_column("ruleset_version")
        batch.drop_column("content_hash")
    with op.batch_alter_table("module_versions") as batch:
        batch.drop_column("content_hash")
    op.drop_column("gm_turn_runs", "context_json")
