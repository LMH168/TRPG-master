"""验证统一 GM 编排模式、阶段轨迹与旧回合保守恢复规则。"""

import pytest

from app.core.gm_orchestration import (
    GameMasterExecutionMode,
    GameMasterOrchestrationRequest,
    GameMasterOrchestrationResult,
    GameMasterOrchestrationSnapshot,
    GameMasterOrchestrator,
    GameMasterRecoveryEvidence,
    GameMasterStage,
    GameMasterStageObserver,
    resolve_legacy_execution_mode,
)


class _FakeExecutionPort:
    """只回放给定阶段，证明编排器不依赖 Engine、Narrator 或数据库实现。"""

    def __init__(
        self,
        stages: tuple[GameMasterStage, ...],
        *,
        result_mode: GameMasterExecutionMode | None = None,
    ) -> None:
        self._stages = stages
        self._result_mode = result_mode

    async def execute(
        self,
        request: GameMasterOrchestrationRequest,
        *,
        on_stage: GameMasterStageObserver,
    ) -> GameMasterOrchestrationResult:
        completed = [GameMasterStage.ACCEPTED]
        for stage in self._stages:
            await on_stage(stage)
            completed.append(stage)
        return GameMasterOrchestrationResult(
            turn_id=request.turn_id,
            execution_mode=self._result_mode or request.execution_mode,
            completed_stages=tuple(completed),
        )


def test_stage_trace_is_monotonic_and_mode_can_narrow_once() -> None:
    snapshot = GameMasterOrchestrationSnapshot(
        execution_mode=GameMasterExecutionMode.NEW_ACTION,
        completed_stages=(GameMasterStage.ACCEPTED,),
    )

    advanced = snapshot.advance(
        GameMasterStage.CONTEXT_LOADED,
        execution_mode=GameMasterExecutionMode.ACTION_PLAN,
    )

    assert advanced.execution_mode == GameMasterExecutionMode.ACTION_PLAN
    assert advanced.completed_stages == (
        GameMasterStage.ACCEPTED,
        GameMasterStage.CONTEXT_LOADED,
    )
    with pytest.raises(ValueError, match="不能倒退或重复"):
        advanced.advance(GameMasterStage.ACCEPTED)


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        (
            GameMasterRecoveryEvidence(has_outbox=True, receipt_count=1),
            GameMasterExecutionMode.DELIVERY_ONLY,
        ),
        (
            GameMasterRecoveryEvidence(has_action_plan=True),
            GameMasterExecutionMode.ACTION_PLAN,
        ),
        (
            GameMasterRecoveryEvidence(has_adjudication_execution=True),
            GameMasterExecutionMode.SINGLE_ADJUDICATION,
        ),
        (
            GameMasterRecoveryEvidence(has_agenda_execution=True, receipt_count=1),
            GameMasterExecutionMode.AGENDA_CONTINUATION,
        ),
        (
            GameMasterRecoveryEvidence(receipt_count=1),
            GameMasterExecutionMode.NARRATION_ONLY,
        ),
    ],
)
def test_legacy_mode_is_adopted_only_from_authoritative_evidence(
    evidence: GameMasterRecoveryEvidence,
    expected: GameMasterExecutionMode,
) -> None:
    assert resolve_legacy_execution_mode(evidence) == expected


def test_legacy_mode_rejects_ambiguous_or_unproven_state() -> None:
    assert resolve_legacy_execution_mode(GameMasterRecoveryEvidence()) is None


def test_legacy_recovery_prefers_the_owner_with_a_remaining_cursor() -> None:
    assert (
        resolve_legacy_execution_mode(
            GameMasterRecoveryEvidence(
                has_action_plan=True,
                has_adjudication_execution=True,
                has_agenda_execution=True,
                receipt_count=2,
            )
        )
        == GameMasterExecutionMode.ACTION_PLAN
    )
    assert (
        resolve_legacy_execution_mode(
            GameMasterRecoveryEvidence(
                has_adjudication_execution=True,
                has_agenda_execution=True,
                receipt_count=2,
            )
        )
        == GameMasterExecutionMode.AGENDA_CONTINUATION
    )
    assert (
        resolve_legacy_execution_mode(
            GameMasterRecoveryEvidence(
                has_adjudication_execution=True,
                receipt_count=1,
            )
        )
        == GameMasterExecutionMode.SINGLE_ADJUDICATION
    )


async def test_orchestrator_delegates_and_observes_monotonic_trace() -> None:
    request = GameMasterOrchestrationRequest(
        turn_id="turn-1",
        room_id="room-1",
        player_id="player-1",
        actor_id="actor-1",
        client_action_id="action-1",
        execution_mode=GameMasterExecutionMode.ACTION_PLAN,
    )
    snapshot = GameMasterOrchestrationSnapshot(
        execution_mode=GameMasterExecutionMode.ACTION_PLAN,
        completed_stages=(GameMasterStage.ACCEPTED,),
    )
    observed: list[GameMasterStage] = []
    orchestrator = GameMasterOrchestrator(
        _FakeExecutionPort(
            (
                GameMasterStage.CONTEXT_LOADED,
                GameMasterStage.HOST_COMPLETED,
                GameMasterStage.VALIDATED,
            )
        )
    )

    result = await orchestrator.run(
        request,
        snapshot=snapshot,
        on_stage=lambda stage: _append_stage(observed, stage),
    )

    assert observed == [
        GameMasterStage.CONTEXT_LOADED,
        GameMasterStage.HOST_COMPLETED,
        GameMasterStage.VALIDATED,
    ]
    assert result.completed_stages[-1] == GameMasterStage.VALIDATED


async def _append_stage(stages: list[GameMasterStage], stage: GameMasterStage) -> None:
    """异步测试 observer，模拟后续写入 Turn 轨迹的适配器。"""

    stages.append(stage)


async def test_orchestrator_rejects_out_of_order_adapter_trace() -> None:
    request = GameMasterOrchestrationRequest(
        turn_id="turn-1",
        room_id="room-1",
        player_id="player-1",
        actor_id="actor-1",
        client_action_id="action-1",
        execution_mode=GameMasterExecutionMode.SINGLE_ADJUDICATION,
    )
    snapshot = GameMasterOrchestrationSnapshot(
        execution_mode=GameMasterExecutionMode.SINGLE_ADJUDICATION,
        completed_stages=(GameMasterStage.ACCEPTED,),
    )

    with pytest.raises(ValueError, match="不能倒退或重复"):
        await GameMasterOrchestrator(
            _FakeExecutionPort((GameMasterStage.ENGINE_COMMITTED, GameMasterStage.CONTEXT_LOADED))
        ).run(request, snapshot=snapshot)


async def test_orchestrator_allows_new_action_to_narrow_once() -> None:
    request = GameMasterOrchestrationRequest(
        turn_id="turn-1",
        room_id="room-1",
        player_id="player-1",
        actor_id="actor-1",
        client_action_id="action-1",
        execution_mode=GameMasterExecutionMode.NEW_ACTION,
    )
    snapshot = GameMasterOrchestrationSnapshot(
        execution_mode=GameMasterExecutionMode.NEW_ACTION,
        completed_stages=(GameMasterStage.ACCEPTED,),
    )

    result = await GameMasterOrchestrator(
        _FakeExecutionPort(
            (GameMasterStage.CONTEXT_LOADED, GameMasterStage.HOST_COMPLETED),
            result_mode=GameMasterExecutionMode.ACTION_PLAN,
        )
    ).run(request, snapshot=snapshot)

    assert result.execution_mode == GameMasterExecutionMode.ACTION_PLAN


async def test_orchestrator_rejects_unresolved_legacy_mode() -> None:
    request = GameMasterOrchestrationRequest(
        turn_id="turn-1",
        room_id="room-1",
        player_id="player-1",
        actor_id="actor-1",
        client_action_id="action-1",
        execution_mode=GameMasterExecutionMode.LEGACY_UNKNOWN,
        recovering=True,
    )
    snapshot = GameMasterOrchestrationSnapshot(
        execution_mode=GameMasterExecutionMode.LEGACY_UNKNOWN,
    )

    with pytest.raises(ValueError, match="必须先由权威证据解析"):
        await GameMasterOrchestrator(_FakeExecutionPort(())).run(
            request,
            snapshot=snapshot,
        )
