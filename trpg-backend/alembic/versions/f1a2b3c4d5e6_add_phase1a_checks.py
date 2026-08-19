"""增加 Phase 1A 服务端检定和待决策持久化表。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | Sequence[str] | None = "ea1b2c3d4f50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立不可重掷的 CheckRun 和可恢复 PendingDecision。"""

    op.create_table(
        "gm_check_runs",
        sa.Column("id", sa.String(100), nullable=False),
        sa.Column("room_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("client_request_id", sa.String(200), nullable=False),
        sa.Column("skill_id", sa.String(100), nullable=False),
        sa.Column("goal", sa.String(500), nullable=False),
        sa.Column("difficulty", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("roll", sa.Integer(), nullable=True),
        sa.Column("target_value", sa.Integer(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["room_id"], ["game_sessions.room_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_gm_check_runs_room_actor", "gm_check_runs", ["room_id", "actor_id"])
    op.create_table(
        "gm_pending_decisions",
        sa.Column("id", sa.String(100), nullable=False),
        sa.Column("room_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("check_id", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["game_sessions.room_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("check_id", name="uq_gm_pending_decisions_check"),
    )
    op.create_index(
        "ix_gm_pending_decisions_actor_status",
        "gm_pending_decisions",
        ["actor_id", "status"],
    )


def downgrade() -> None:
    """拒绝自动删除检定历史，使用备份回滚。"""

    raise RuntimeError(
        "旧 AI 主持运行时清理不可逆；Phase 1A 检定历史不可自动降级，请使用数据库备份恢复"
    )
