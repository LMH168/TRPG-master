"""验证可靠回合状态机、幂等身份与内存 Store 契约。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.turn_runtime import (
    InMemoryTurnStore,
    TurnCommitState,
    TurnConflictError,
    TurnContractError,
    TurnInputSnapshot,
    TurnRecord,
    TurnRecoveryAction,
    TurnResumePoint,
    TurnStatus,
    TurnWaitingReason,
    new_turn_record,
    transition_turn,
)


def _request(
    *,
    action_id: str = "action-1",
    utterance: str = "观察书房",
    player_id: str = "player-1",
) -> TurnInputSnapshot:
    return TurnInputSnapshot(
        room_id="room-1",
        player_id=player_id,
        actor_id="actor-1",
        client_action_id=action_id,
        utterance=utterance,
    )


def test_input_fingerprint_normalizes_boundary_whitespace() -> None:
    first = _request(utterance=" 观察书房 ")
    second = _request(utterance="观察书房")

    assert first.utterance == "观察书房"
    assert first.fingerprint() == second.fingerprint()


def test_transition_rejects_skipped_or_reversed_phase() -> None:
    turn = new_turn_record(_request())

    with pytest.raises(TurnContractError, match="非法回合状态转换"):
        transition_turn(
            turn,
            status=TurnStatus.EXECUTING,
            resume_point=TurnResumePoint.EXECUTING,
            recovery_action=TurnRecoveryAction.WAIT,
        )


def test_waiting_reason_requires_adjudicating_player_boundary() -> None:
    turn = new_turn_record(_request())
    planning = transition_turn(
        turn,
        status=TurnStatus.PLANNING,
        resume_point=TurnResumePoint.PLANNING,
        recovery_action=TurnRecoveryAction.WAIT,
    )

    with pytest.raises(ValueError, match="玩家等待原因"):
        transition_turn(
            planning,
            status=TurnStatus.ADJUDICATING,
            resume_point=TurnResumePoint.ADJUDICATING,
            waiting_reason=TurnWaitingReason.SKILL_CHOICE,
            recovery_action=TurnRecoveryAction.CHOOSE_SKILL,
        )


def test_commit_state_cannot_move_backwards() -> None:
    turn = new_turn_record(_request())
    planning = transition_turn(
        turn,
        status=TurnStatus.PLANNING,
        resume_point=TurnResumePoint.PLANNING,
        commit_state=TurnCommitState.COMMITTED,
        recovery_action=TurnRecoveryAction.WAIT,
    )

    with pytest.raises(TurnContractError, match="commit_state"):
        transition_turn(
            planning,
            status=TurnStatus.AWAITING_NARRATION,
            resume_point=TurnResumePoint.NARRATING,
            commit_state=TurnCommitState.NOT_COMMITTED,
            recovery_action=TurnRecoveryAction.WAIT,
        )


async def test_same_request_is_idempotent_and_different_input_conflicts() -> None:
    store = InMemoryTurnStore()
    proposed = new_turn_record(_request())

    created, is_new = await store.create_or_get(proposed)
    replayed, replay_is_new = await store.create_or_get(new_turn_record(_request()))

    assert is_new is True
    assert replay_is_new is False
    assert replayed == created

    conflicting = new_turn_record(_request(utterance="离开书房")).model_copy(
        update={"turn_id": proposed.turn_id}
    )
    with pytest.raises(TurnConflictError) as exc_info:
        await store.create_or_get(conflicting)
    assert exc_info.value.code == "TURN_IDEMPOTENCY_CONFLICT"


async def test_room_reservation_survives_wait_and_releases_at_terminal() -> None:
    store = InMemoryTurnStore()
    turn, _ = await store.create_or_get(new_turn_record(_request()))
    planning = transition_turn(
        turn,
        status=TurnStatus.PLANNING,
        resume_point=TurnResumePoint.PLANNING,
        recovery_action=TurnRecoveryAction.WAIT,
    )
    await store.compare_and_swap(expected_phase_version=1, updated=planning)
    adjudicating = transition_turn(
        planning,
        status=TurnStatus.ADJUDICATING,
        resume_point=TurnResumePoint.AWAITING_PLAYER,
        waiting_reason=TurnWaitingReason.SKILL_CHOICE,
        recovery_action=TurnRecoveryAction.CHOOSE_SKILL,
    )
    await store.compare_and_swap(expected_phase_version=2, updated=adjudicating)

    with pytest.raises(TurnConflictError) as busy:
        await store.create_or_get(new_turn_record(_request(action_id="action-2")))
    assert busy.value.code == "TURN_IN_PROGRESS"

    cancelled = transition_turn(
        adjudicating,
        status=TurnStatus.CANCELLED,
        resume_point=TurnResumePoint.NONE,
        commit_state=TurnCommitState.NOT_COMMITTED,
        recovery_action=TurnRecoveryAction.NONE,
    )
    await store.compare_and_swap(expected_phase_version=3, updated=cancelled)

    second, is_new = await store.create_or_get(new_turn_record(_request(action_id="action-2")))
    assert is_new is True
    assert second.client_action_id == "action-2"


async def test_compare_and_swap_rejects_stale_worker() -> None:
    store = InMemoryTurnStore()
    turn, _ = await store.create_or_get(new_turn_record(_request()))
    planning = transition_turn(
        turn,
        status=TurnStatus.PLANNING,
        resume_point=TurnResumePoint.PLANNING,
        recovery_action=TurnRecoveryAction.WAIT,
    )
    await store.compare_and_swap(expected_phase_version=1, updated=planning)

    stale = planning.model_copy(
        update={
            "phase_version": 2,
            "updated_at": datetime.now(UTC) + timedelta(seconds=1),
        }
    )
    with pytest.raises(TurnConflictError) as exc_info:
        await store.compare_and_swap(expected_phase_version=1, updated=stale)
    assert exc_info.value.code == "TURN_VERSION_CONFLICT"


async def test_compare_and_swap_rejects_forged_phase_jump() -> None:
    store = InMemoryTurnStore()
    turn, _ = await store.create_or_get(new_turn_record(_request()))
    forged = TurnRecord.model_validate(
        {
            **turn.model_dump(),
            "status": TurnStatus.EXECUTING,
            "phase_version": turn.phase_version + 1,
            "resume_point": TurnResumePoint.EXECUTING,
            "updated_at": datetime.now(UTC),
        }
    )

    with pytest.raises(TurnContractError, match="非法回合状态转换"):
        await store.compare_and_swap(expected_phase_version=turn.phase_version, updated=forged)


async def test_worker_lease_can_only_be_released_by_current_owner() -> None:
    store = InMemoryTurnStore()
    turn, _ = await store.create_or_get(new_turn_record(_request()))
    now = datetime.now(UTC)
    claimed = await store.claim(
        turn_id=turn.turn_id,
        worker_id="worker-1",
        now=now,
        lease_expires_at=now + timedelta(seconds=60),
    )

    with pytest.raises(TurnConflictError) as busy:
        await store.claim(
            turn_id=turn.turn_id,
            worker_id="worker-2",
            now=now,
            lease_expires_at=now + timedelta(seconds=60),
        )
    assert busy.value.code == "TURN_WORKER_BUSY"

    with pytest.raises(TurnConflictError) as lost:
        await store.release_claim(
            turn_id=turn.turn_id,
            worker_id="worker-2",
            expected_phase_version=claimed.phase_version,
            now=now,
        )
    assert lost.value.code == "TURN_LEASE_LOST"

    released = await store.release_claim(
        turn_id=turn.turn_id,
        worker_id="worker-1",
        expected_phase_version=claimed.phase_version,
        now=now,
    )
    assert released.lease_owner is None
