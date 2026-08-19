"""验证清理迁移只保留无 AI 主持基础设施，并能从合并前基线安全升级。"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PRE_CLEANUP_REVISION = "c7d8e9f0a1b2"
HEAD_REVISION = "d8e9f0a1b2c3"

# 这些表承载账号、房间、角色、聊天、目录素材和生图，清理后必须继续存在。
FOUNDATION_TABLES = {
    "alembic_version",
    "games",
    "game_systems",
    "worlds",
    "scenarios",
    "module_pregens",
    "module_assets",
    "users",
    "user_sessions",
    "user_character_templates",
    "user_character_template_portraits",
    "rooms",
    "players",
    "characters",
    "character_portraits",
    "portrait_generation_tasks",
    "notes",
    "chat_messages",
}

# 旧 AI 主持编排、权威执行和投影数据全部放弃，避免新架构继续背负双写路径。
REMOVED_AI_TABLES = {
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
}


def _run_alembic(database: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在独立 SQLite 数据库运行 Alembic，避免迁移测试污染开发数据库。"""

    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _upgrade_or_fail(database: Path, revision: str) -> None:
    """升级到指定 revision，并在失败时保留 Alembic 的完整诊断。"""

    result = _run_alembic(database, "upgrade", revision)
    assert result.returncode == 0, result.stdout + result.stderr


def _table_names(database: Path) -> set[str]:
    """读取 SQLite 当前所有表名。"""

    with sqlite3.connect(database) as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def _column_names(database: Path, table: str) -> set[str]:
    """读取指定表的列名集合。"""

    with sqlite3.connect(database) as connection:
        return {row[1] for row in connection.execute(f"PRAGMA table_info('{table}')")}


def _current_revision(database: Path) -> str:
    """返回数据库记录的唯一 Alembic revision。"""

    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def test_alembic_has_single_clean_foundation_head(tmp_path: Path) -> None:
    """迁移拓扑必须只有一个清理后的 head。"""

    database = tmp_path / "heads.db"
    result = _run_alembic(database, "heads")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == f"{HEAD_REVISION} (head)"


def test_fresh_upgrade_keeps_foundation_and_removes_ai_runtime(tmp_path: Path) -> None:
    """全新数据库升级后只留下重构所需的产品基础表。"""

    database = tmp_path / "fresh.db"
    _upgrade_or_fail(database, "head")

    tables = _table_names(database)
    assert tables >= FOUNDATION_TABLES
    assert REMOVED_AI_TABLES.isdisjoint(tables)
    assert _current_revision(database) == HEAD_REVISION
    assert {"module_version", "host_speech_voice_type", "discovered_scene_ids"}.isdisjoint(
        _column_names(database, "rooms")
    )


def test_cleanup_upgrade_preserves_foundation_rows(tmp_path: Path) -> None:
    """从旧合并 head 清理时，账号与目录数据不能随主持数据被删除。"""

    database = tmp_path / "upgrade.db"
    _upgrade_or_fail(database, PRE_CLEANUP_REVISION)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO games (id, name, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            ("00000000-0000-0000-0000-000000000001", "COC"),
        )
        connection.execute(
            "INSERT INTO users (id, account, password_hash, nickname, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (
                "00000000-0000-0000-0000-000000000002",
                "cleanup-user",
                "hash",
                "清理测试用户",
            ),
        )
        connection.commit()

    _upgrade_or_fail(database, "head")

    with sqlite3.connect(database) as connection:
        game = connection.execute("SELECT name FROM games").fetchone()
        user = connection.execute("SELECT account FROM users").fetchone()
    assert game == ("COC",)
    assert user == ("cleanup-user",)
    assert REMOVED_AI_TABLES.isdisjoint(_table_names(database))


def test_cleanup_migration_is_explicitly_irreversible(tmp_path: Path) -> None:
    """旧主持数据无法可靠重建，降级必须明确失败而非创建空壳表。"""

    database = tmp_path / "irreversible.db"
    _upgrade_or_fail(database, "head")

    result = _run_alembic(database, "downgrade", PRE_CLEANUP_REVISION)

    assert result.returncode != 0
    assert "旧 AI 主持运行时清理不可逆" in result.stdout + result.stderr
    assert _current_revision(database) == HEAD_REVISION
