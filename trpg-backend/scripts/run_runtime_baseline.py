"""运行 AI 主持运行时 v2 确定性基线，并按需执行真实模型评测。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tests.runtime_baseline.application_adapter import InMemoryRuntimeAdapter
from tests.runtime_baseline.faults import FaultRecoveryAdapter
from tests.runtime_baseline.loader import load_scenarios
from tests.runtime_baseline.runner import collect_metrics, run_scenarios
from tests.runtime_baseline.thresholds import load_thresholds, metric_regressions


async def build_deterministic_report() -> dict[str, Any]:
    """执行核心与故障场景，返回不含时间戳和随机 ID 的稳定报告。"""

    scenarios = load_scenarios()
    core = tuple(item for item in scenarios if item.category != "fault-recovery")
    faults = tuple(item for item in scenarios if item.category == "fault-recovery")
    core_results = await run_scenarios(core, InMemoryRuntimeAdapter)
    fault_results = await run_scenarios(faults, FaultRecoveryAdapter)
    results = (*core_results, *fault_results)
    metrics = collect_metrics(results)
    regressions = metric_regressions(metrics, load_thresholds())
    return {
        "schema_version": 1,
        "mode": "deterministic",
        "metrics": {
            **metrics.model_dump(mode="json"),
            "success_rate": metrics.success_rate,
        },
        "regressions": list(regressions),
        "scenarios": [
            {
                "id": result.scenario_id,
                "category": result.category,
                "passed": result.passed,
                "known_gaps": list(result.known_gaps),
                "hard_failures": list(result.hard_failures),
            }
            for result in results
        ],
    }


def run_real_model_evaluation() -> dict[str, Any]:
    """显式运行现有真实模型游玩评测；报告不包含密钥或模型正文。"""

    # Settings 只在用户明确传入 --real-model 后实例化，默认路径不会读取模型配置。
    from app.core.config import Settings

    settings = Settings()
    provider = settings.host_model_provider
    model = {
        "deepseek": settings.deepseek_model,
        "qwen": settings.qwen_model,
        "openai": settings.openai_model,
    }.get(provider, "unknown")
    environment = os.environ.copy()
    environment["RUN_REAL_MODEL_PLAY_SIM"] = "1"
    environment.setdefault("SIM_LOG_PATH", "/tmp/trpg-runtime-baseline-real-model.jsonl")
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_play_sim_real_model.py", "-q"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "provider": provider,
        "model": model,
        "suite": "tests/test_play_sim_real_model.py",
        "duration_seconds": round(time.monotonic() - started, 3),
        "status": "passed" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
    }


def parse_args() -> argparse.Namespace:
    """解析命令行；真实模型评测必须通过显式开关启用。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real-model",
        action="store_true",
        help="额外运行会产生网络请求和模型费用的真实模型评测",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="可选的 JSON 报告路径；省略时只写标准输出",
    )
    return parser.parse_args()


def main() -> int:
    """生成报告并以退出码表达确定性回归或真实模型失败。"""

    args = parse_args()
    # 运行时现有结构化日志写 stdout；报告模式把它们转到 stderr，保证 stdout 是纯 JSON。
    with contextlib.redirect_stdout(sys.stderr):
        report = asyncio.run(build_deterministic_report())
    if args.real_model:
        report["real_model"] = run_real_model_evaluation()
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    regressions = report["regressions"]
    scenarios = report["scenarios"]
    failed = bool(regressions) or any(not item["passed"] for item in scenarios)
    if args.real_model and report["real_model"]["status"] != "passed":
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
