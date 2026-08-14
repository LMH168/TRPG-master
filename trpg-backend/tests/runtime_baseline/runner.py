"""执行基线场景、校验权威不变量并汇总不可恶化指标。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from .contracts import (
    BaselineMetrics,
    BaselineResult,
    BaselineScenario,
    BaselineTurn,
    BaselineTurnResult,
)


class RuntimeBaselineAdapter(Protocol):
    """把场景契约适配到当前运行时；实现必须在场景之间隔离状态。"""

    async def prepare(self, scenario: BaselineScenario) -> Mapping[str, str]: ...

    async def execute_turn(
        self,
        turn: BaselineTurn,
        *,
        aliases: Mapping[str, str],
    ) -> BaselineTurnResult: ...

    async def close(self) -> None: ...


class BaselineRunner:
    """按统一流程执行场景，并对结构化结果应用强制断言。"""

    def __init__(self, adapter_factory: Callable[[], RuntimeBaselineAdapter]) -> None:
        self._adapter_factory = adapter_factory

    async def run(self, scenario: BaselineScenario) -> BaselineResult:
        """执行一个场景；已知缺陷只计数，权威不变量仍然硬失败。"""

        adapter = self._adapter_factory()
        results: list[BaselineTurnResult] = []
        try:
            aliases = await adapter.prepare(scenario)
            for turn in scenario.turns:
                for _ in range(turn.repeat):
                    results.append(await adapter.execute_turn(turn, aliases=aliases))
        finally:
            await adapter.close()

        failures = self._validate(scenario, results)
        hard_failures = tuple(
            message
            for code, message in failures
            if code not in scenario.expectation.known_gap_assertions
        )
        known_gaps = tuple(sorted(set(scenario.expectation.known_gaps)))
        return BaselineResult(
            scenario_id=scenario.id,
            category=scenario.category,
            passed=not hard_failures,
            turns=tuple(results),
            hard_failures=hard_failures,
            known_gaps=known_gaps,
        )

    @staticmethod
    def _validate(
        scenario: BaselineScenario,
        results: Sequence[BaselineTurnResult],
    ) -> list[tuple[str, str]]:
        """校验终态、事件、状态和叙事证据，不比较自然语言全文。"""

        expectation = scenario.expectation
        failures: list[tuple[str, str]] = []
        for result in results:
            if result.status not in expectation.terminal_statuses:
                failures.append(
                    (
                        "terminal_status",
                        f"{result.client_action_id}: 非预期终态 {result.status}",
                    )
                )
            missing_events = set(expectation.required_event_types) - set(result.event_types)
            if missing_events:
                failures.append(
                    (
                        "required_events",
                        f"{result.client_action_id}: 缺少事件 {sorted(missing_events)!r}",
                    )
                )
            forbidden_events = set(expectation.forbidden_event_types) & set(result.event_types)
            if forbidden_events:
                failures.append(
                    (
                        "forbidden_events",
                        f"{result.client_action_id}: 出现禁止事件 {sorted(forbidden_events)!r}",
                    )
                )
            for key, expected in expectation.required_state.items():
                if result.state.get(key) != expected:
                    failures.append(
                        (
                            f"required_state:{key}",
                            f"{result.client_action_id}: 状态 {key!r}="
                            f"{result.state.get(key)!r}，预期 {expected!r}",
                        )
                    )
            missing_evidence = set(expectation.required_narration_evidence) - set(
                result.narration_evidence
            )
            if missing_evidence:
                failures.append(
                    (
                        "required_narration_evidence",
                        f"{result.client_action_id}: 叙事缺少证据 {sorted(missing_evidence)!r}",
                    )
                )
            forbidden_claims = set(expectation.forbidden_narration_claims) & set(
                result.narration_claims
            )
            if forbidden_claims:
                failures.append(
                    (
                        "forbidden_narration_claims",
                        f"{result.client_action_id}: 叙事包含无依据声明 "
                        f"{sorted(forbidden_claims)!r}",
                    )
                )
        return failures


def collect_metrics(results: Sequence[BaselineResult]) -> BaselineMetrics:
    """将场景结果压缩成适合与冻结阈值比较的确定性指标。"""

    errors: Counter[str] = Counter()
    metrics = BaselineMetrics(
        scenarios_total=len(results),
        scenarios_passed=sum(result.passed for result in results),
        known_gaps=sum(len(result.known_gaps) for result in results),
    )
    for result in results:
        for turn in result.turns:
            if turn.status not in {"completed", "failed", "cancelled", "needs_clarification"}:
                metrics.turns_without_terminal_status += 1
            if turn.error_phase is not None:
                errors[turn.error_phase] += 1
            if not turn.commit_known:
                metrics.unknown_commit_state += 1
            metrics.duplicate_rolls += _duplicates(turn.roll_ids)
            metrics.duplicate_events += _duplicates(turn.event_ids)
            metrics.duplicate_state_changes += _duplicates(
                tuple(str(version) for version in turn.state_versions)
            )
        metrics.state_narration_mismatches += sum(
            "state_narration_mismatch" in failure for failure in result.hard_failures
        )
    metrics.errors_by_phase = dict(sorted(errors.items()))
    return metrics


def _duplicates(values: Sequence[str]) -> int:
    """返回序列中超出首次出现的重复项数量。"""

    counts = Counter(values)
    return sum(count - 1 for count in counts.values() if count > 1)


async def run_scenarios(
    scenarios: Sequence[BaselineScenario],
    adapter_factory: Callable[[], RuntimeBaselineAdapter],
) -> tuple[BaselineResult, ...]:
    """依次执行场景，避免共享数据库状态影响确定性。"""

    runner = BaselineRunner(adapter_factory)
    return tuple([await runner.run(scenario) for scenario in scenarios])
