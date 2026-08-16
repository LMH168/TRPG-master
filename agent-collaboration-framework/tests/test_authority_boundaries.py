"""验证 Proposal-only Host 与 Engine 单一权威写入口的依赖边界。"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from collaboration_framework.engine import AdjudicationEngineService
from collaboration_framework.ports.adjudication_executor import (
    AdjudicationExecutor,
    ProposalSubmissionExecutor,
)

ROOT = Path(__file__).resolve().parents[1]


def _imported_names(path: Path) -> set[str]:
    """读取模块的直接导入名，防止只读角色依赖权威状态写模型。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def test_read_only_roles_do_not_import_authoritative_state_types() -> None:
    """Host、Narrator 和回合协调层不能获得 GameState/Event 写端口。"""

    module_paths = (
        ROOT / "collaboration_framework/host/application/action_plan_orchestrator.py",
        ROOT / "collaboration_framework/host/application/narrator.py",
    )
    for path in module_paths:
        imported = _imported_names(path)
        assert "GameState" not in imported
        assert "DomainEvent" not in imported


def test_public_executor_ports_have_one_submission_shape() -> None:
    """外部端口只接受 Proposal，旧 ActionAdjudication writer 不可见。"""

    assert "submit" not in AdjudicationExecutor.__dict__
    assert set(ProposalSubmissionExecutor.__dict__) >= {"submit_proposal"}
    assert "submit" not in ProposalSubmissionExecutor.__dict__


def test_engine_exposes_proposal_submission_only() -> None:
    """Engine 保留历史内部 helper，但不再暴露无 Validator 的公开 submit。"""

    public_methods = {
        name
        for name, member in inspect.getmembers(
            AdjudicationEngineService, inspect.isfunction
        )
        if not name.startswith("_")
    }
    assert "submit_proposal" in public_methods
    assert "submit" not in public_methods
