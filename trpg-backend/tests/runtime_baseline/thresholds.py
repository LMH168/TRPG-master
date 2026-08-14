"""加载冻结阈值并拒绝可靠性指标恶化。"""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import BaselineMetrics

DEFAULT_THRESHOLD_PATH = Path(__file__).with_name("baseline-thresholds.json")


def load_thresholds(path: Path = DEFAULT_THRESHOLD_PATH) -> dict[str, object]:
    """读取受版本控制的阈值文件。"""

    return json.loads(path.read_text(encoding="utf-8"))


def metric_regressions(
    metrics: BaselineMetrics,
    thresholds: dict[str, object],
) -> tuple[str, ...]:
    """返回所有恶化项；指标改善和新增成功场景不会失败。"""

    regressions: list[str] = []
    maximums = thresholds.get("maximums", {})
    if not isinstance(maximums, dict):
        raise ValueError("thresholds.maximums 必须是对象")
    payload = metrics.model_dump()
    for name, maximum in maximums.items():
        actual = payload.get(name)
        if not isinstance(maximum, int) or not isinstance(actual, int):
            raise ValueError(f"阈值 {name} 必须对应整数指标")
        if actual > maximum:
            regressions.append(f"{name}={actual} 超过冻结上限 {maximum}")

    error_maximums = thresholds.get("errors_by_phase", {})
    if not isinstance(error_maximums, dict):
        raise ValueError("thresholds.errors_by_phase 必须是对象")
    for phase, actual in metrics.errors_by_phase.items():
        maximum = error_maximums.get(phase, 0)
        if not isinstance(maximum, int):
            raise ValueError(f"阶段阈值 {phase} 必须是整数")
        if actual > maximum:
            regressions.append(f"errors_by_phase.{phase}={actual} 超过冻结上限 {maximum}")

    minimum_success_rate = thresholds.get("minimum_success_rate", 1.0)
    if not isinstance(minimum_success_rate, int | float):
        raise ValueError("minimum_success_rate 必须是数字")
    if metrics.success_rate < minimum_success_rate:
        regressions.append(
            f"success_rate={metrics.success_rate:.4f} 低于冻结下限 {minimum_success_rate:.4f}"
        )
    return tuple(regressions)
