"""验证各执行阶段故障后的提交边界、恢复和去重行为。"""

from __future__ import annotations

import pytest

from .application_adapter import InMemoryRuntimeAdapter
from .faults import FaultRecoveryAdapter
from .loader import load_scenarios
from .runner import BaselineRunner, collect_metrics, run_scenarios
from .thresholds import load_thresholds, metric_regressions

FAULT_SCENARIOS = tuple(
    scenario for scenario in load_scenarios() if scenario.category == "fault-recovery"
)


@pytest.mark.parametrize("scenario", FAULT_SCENARIOS, ids=lambda scenario: scenario.id)
async def test_fault_scenario_recovers_without_duplicate_authoritative_writes(scenario) -> None:
    result = await BaselineRunner(FaultRecoveryAdapter).run(scenario)

    assert result.passed is True
    assert result.hard_failures == ()
    assert result.turns[0].commit_known is True
    assert result.turns[0].error_phase == scenario.faults[0].point
    assert len(result.turns[0].event_ids) == len(set(result.turns[0].event_ids))
    assert len(result.turns[0].state_versions) == len(set(result.turns[0].state_versions))


async def test_frozen_thresholds_reject_regressions_but_allow_improvements() -> None:
    core = tuple(scenario for scenario in load_scenarios() if scenario.category != "fault-recovery")
    core_results = await run_scenarios(core, InMemoryRuntimeAdapter)
    fault_results = await run_scenarios(FAULT_SCENARIOS, FaultRecoveryAdapter)
    results = (*core_results, *fault_results)
    metrics = collect_metrics(results)
    thresholds = load_thresholds()

    assert metric_regressions(metrics, thresholds) == ()
    assert metrics.known_gaps == 2
    assert metrics.scenarios_total == 15
    degraded = metrics.model_copy(update={"unknown_commit_state": 1})
    assert metric_regressions(degraded, thresholds) == ("unknown_commit_state=1 超过冻结上限 0",)
