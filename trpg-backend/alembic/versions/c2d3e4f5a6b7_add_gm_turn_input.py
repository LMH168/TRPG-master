"""保存 Phase 1B 自然语言回合输入，以便模型恢复后重试。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: str | Sequence[str] | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    """为进行中的自然语言回合增加恢复所需的身份和原文。"""

    op.add_column("gm_turn_runs", sa.Column("actor_id", sa.String(100), nullable=True))
    op.add_column("gm_turn_runs", sa.Column("input_text", sa.String(4000), nullable=True))
    # Phase 0 没有生产回合数据；先填充后收紧约束，保证从空库迁移和已有测试库都可升级。
    op.execute("UPDATE gm_turn_runs SET actor_id = '', input_text = ''")
    with op.batch_alter_table("gm_turn_runs") as batch:
        batch.alter_column("actor_id", nullable=False)
        batch.alter_column("input_text", nullable=False)


def downgrade() -> None:
    """拒绝删除恢复输入，避免正在暂停的回合失去继续依据。"""

    raise RuntimeError(
        "旧 AI 主持运行时清理不可逆；Phase 1B 回合恢复字段不可自动降级，请使用数据库备份恢复"
    )
