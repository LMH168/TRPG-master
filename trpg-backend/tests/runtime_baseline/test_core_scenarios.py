"""通过真实内存 Engine 执行 AI 主持运行时 v2 核心基线场景。"""

from __future__ import annotations

import pytest

from .application_adapter import InMemoryRuntimeAdapter
from .loader import load_scenarios
from .runner import BaselineRunner

CORE_SCENARIOS = tuple(
    scenario for scenario in load_scenarios() if scenario.category != "fault-recovery"
)


@pytest.mark.parametrize("scenario", CORE_SCENARIOS, ids=lambda scenario: scenario.id)
async def test_core_scenario_satisfies_authoritative_invariants(scenario) -> None:
    result = await BaselineRunner(InMemoryRuntimeAdapter).run(scenario)

    assert result.hard_failures == ()
    assert result.passed is True


@pytest.mark.parametrize("scenario", CORE_SCENARIOS, ids=lambda scenario: scenario.id)
async def test_core_scenario_is_deterministic_between_fresh_runs(scenario) -> None:
    runner = BaselineRunner(InMemoryRuntimeAdapter)

    first = await runner.run(scenario)
    second = await runner.run(scenario)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


async def test_repeated_client_action_id_does_not_append_events_or_state_versions() -> None:
    scenario = next(item for item in CORE_SCENARIOS if item.id == "idempotency.action-retry")
    result = await BaselineRunner(InMemoryRuntimeAdapter).run(scenario)

    assert len(result.turns) == 2
    assert result.turns[0].event_ids
    assert result.turns[1].event_ids == ()
    assert result.turns[1].state_versions == ()


async def test_dynamic_content_scenarios_are_now_authoritative_successes() -> None:
    runner = BaselineRunner(InMemoryRuntimeAdapter)
    item = next(item for item in CORE_SCENARIOS if item.id == "dynamic.item-pickup")
    location = next(item for item in CORE_SCENARIOS if item.id == "dynamic.location-enter")

    item_result = await runner.run(item)
    location_result = await runner.run(location)

    assert item_result.known_gaps == ()
    assert location_result.known_gaps == ()
    assert item_result.hard_failures == ()
    assert location_result.hard_failures == ()
