"""合并当前运行时与上游角色卡模板头像的两条迁移链。

本迁移只负责收敛 Alembic 拓扑，不重复执行任何表结构变更，保证已有数据库可以
从任一历史 head 升级到唯一的最新 head。
"""

from collections.abc import Sequence


revision: str = "c7d8e9f0a1b2"
down_revision: str | Sequence[str] | None = (
    "b6c7d8e9f0a1",
    "a3c4d5e6f7b8",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """两个父迁移已经完成全部写入，merge head 不需要追加操作。"""


def downgrade() -> None:
    """降级时回到两个独立父 head，具体回滚由各父迁移负责。"""
