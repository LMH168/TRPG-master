"""合并 RuleAgenda 与角色卡库两条并行迁移链。

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0, d7e8f9a0b1c2

本文件只汇合迁移拓扑，不重复执行任一分支的数据或表结构变更。这样无论数据库
此前位于 Agenda head 还是卡库 head，都能安全升级到同一个生产 head。
"""

from collections.abc import Sequence

revision: str = "b6c7d8e9f0a1"
down_revision: str | Sequence[str] | None = (
    "a5b6c7d8e9f0",
    "d7e8f9a0b1c2",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """两个父迁移已经完成全部写入，merge head 无需追加操作。"""


def downgrade() -> None:
    """降级到两个父 head，具体回滚继续由各自迁移负责。"""
