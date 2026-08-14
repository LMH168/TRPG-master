"""验证基线契约、加载顺序和执行结果规范化。"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from .contracts import BaselineScenario, BaselineTurn, BaselineTurnResult
from .loader import load_scenarios
from .runner import BaselineRunner, collect_metrics


class _ContractAdapter:
    """仅用于验证运行器契约，不模拟生产规则。"""

    async def prepare(self, scenario: BaselineScenario) -> Mapping[str, str]:
        return {
            alias: f"resolved:{target}" for alias, target in scenario.initial_state.aliases.items()
        }

    async def execute_turn(
        self,
        turn: BaselineTurn,
        *,
        aliases: Mapping[str, str],
    ) -> BaselineTurnResult:
        assert aliases["@caretaker"] == "resolved:thomas"
        return BaselineTurnResult(
            client_action_id=turn.client_action_id,
            status="completed",
            phases=("understanding_action", "executing_action", "generating_narration"),
            event_types=("action.broadcast", "narration.push"),
        )

    async def close(self) -> None:
        return None


def test_loader_reads_versioned_sanitized_scenarios_in_stable_order() -> None:
    scenarios = load_scenarios()

    assert scenarios
    assert [scenario.id for scenario in scenarios] == sorted(scenario.id for scenario in scenarios)
    assert all(scenario.schema_version == 1 for scenario in scenarios)
    assert all(not scenario.contains_private_data for scenario in scenarios)


def test_contract_rejects_unknown_schema_and_private_data() -> None:
    payload = load_scenarios()[0].model_dump(mode="json")
    payload["schema_version"] = 2
    with pytest.raises(ValidationError, match="不支持的场景"):
        BaselineScenario.model_validate(payload)

    payload["schema_version"] = 1
    payload["contains_private_data"] = True
    with pytest.raises(ValidationError, match="必须完成脱敏"):
        BaselineScenario.model_validate(payload)


async def test_runner_compares_structured_results_without_snapshotting_prose() -> None:
    scenario = load_scenarios()[0]
    result = await BaselineRunner(_ContractAdapter).run(scenario)

    assert result.passed is True
    assert result.hard_failures == ()
    assert [turn.client_action_id for turn in result.turns] == [
        "baseline-conversation-1",
        "baseline-conversation-2",
    ]
    assert collect_metrics((result,)).model_dump() == {
        "scenarios_total": 1,
        "scenarios_passed": 1,
        "turns_without_terminal_status": 0,
        "errors_by_phase": {},
        "duplicate_rolls": 0,
        "duplicate_events": 0,
        "duplicate_state_changes": 0,
        "state_narration_mismatches": 0,
        "unknown_commit_state": 0,
        "known_gaps": 0,
    }
