"""删除旧 AI 主持运行时、结构化模组执行层及其派生数据。

这是 runtime-v3 重建前的有意破坏性基线：账号、房间、角色卡、聊天、模组目录、
预设角色、静态素材和生图数据保留；旧对局状态和主持记录不迁移。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: str | Sequence[str] | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """按外键依赖顺序删除旧主持运行时表和房间字段。"""

    tables = (
        "memory_entries",
        "memory_projection_runs",
        "agenda_step_executions",
        "narration_outbox",
        "room_turn_reservations",
        "turn_commit_receipts",
        "events",
        "action_plan_runs",
        "game_events",
        "turn_records",
        "ending_command_executions",
        "ending_drafts",
        "inventory_command_executions",
        "inventory_import_drafts",
        "adjudication_command_executions",
        "check_runs",
        "pending_check_decisions",
        "action_executions",
        "room_action_reservations",
        "game_sessions",
        "module_versions",
        "check_results",
        "room_summaries",
        "module_import_jobs",
        "module_checkpoints",
        "module_san_triggers",
        "module_win_conditions",
        "entities",
        "scenario_scenes",
    )
    if op.get_bind().dialect.name == "postgresql":
        # 这些表全都属于被放弃的旧运行时，交叉外键没有保留价值。CASCADE 只在
        # PostgreSQL 清理同一批旧表时使用，不会触及账号、房间或角色卡基础表。
        for table_name in tables:
            op.execute(sa.text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))
    else:
        for table_name in tables:
            op.drop_table(table_name)

    with op.batch_alter_table("rooms") as batch_op:
        batch_op.drop_column("module_version")
        batch_op.drop_column("host_speech_voice_type")
        batch_op.drop_column("discovered_scene_ids")


def downgrade() -> None:
    """旧主持数据已明确放弃，禁止伪造一个无法恢复数据的降级。"""

    raise RuntimeError("旧 AI 主持运行时清理不可逆；请从清理前备份恢复")
