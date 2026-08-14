"""从仓库内 JSON 夹具加载并校验运行时基线场景。"""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import BaselineScenario

DEFAULT_SCENARIO_DIR = Path(__file__).with_name("scenarios")


def load_scenario(path: Path) -> BaselineScenario:
    """加载单个场景，并通过 Pydantic 契约拒绝未知或未脱敏字段。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return BaselineScenario.model_validate(payload)


def load_scenarios(directory: Path = DEFAULT_SCENARIO_DIR) -> tuple[BaselineScenario, ...]:
    """按场景 ID 排序加载目录，保证本地和 CI 的执行顺序一致。"""

    scenarios = tuple(load_scenario(path) for path in sorted(directory.glob("*.json")))
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("基线场景 id 必须唯一")
    return tuple(sorted(scenarios, key=lambda scenario: scenario.id))
