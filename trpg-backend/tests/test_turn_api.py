"""可靠回合 REST 查询、权限隔离与 resume 骨架测试。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import SqlAlchemyTurnStore
from app.core.turn_runtime import (
    TurnCommitState,
    TurnInputSnapshot,
    TurnRecoveryAction,
    TurnResultSnapshot,
    TurnResumePoint,
    TurnStatus,
    new_turn_record,
    transition_turn,
)
from app.models.room import Player, Room


async def _members(db: AsyncSession) -> tuple[str, Player, Player]:
    """创建同房间的 owner 与另一名玩家，供权限测试复用。"""

    room_id = str(uuid4())
    owner = Player(
        id=str(uuid4()),
        room_id=room_id,
        nickname="Owner",
        is_host=True,
        reconnect_token=str(uuid4()),
    )
    other = Player(
        id=str(uuid4()),
        room_id=room_id,
        nickname="Other",
        reconnect_token=str(uuid4()),
    )
    db.add(
        Room(
            id=room_id,
            room_code=uuid4().hex[:6].upper(),
            room_name="Turn API",
            max_players=4,
            phase="InGame",
        )
    )
    db.add_all([owner, other])
    await db.commit()
    return room_id, owner, other


def _new_turn(room_id: str, player_id: str, action_id: str):
    """构造 API 测试用初始回合。"""

    return new_turn_record(
        TurnInputSnapshot(
            room_id=room_id,
            player_id=player_id,
            actor_id="investigator-1",
            client_action_id=action_id,
            utterance="检查上锁的抽屉",
        ),
        now=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
    )


async def _complete_turn(store: SqlAlchemyTurnStore, current):
    """按合法状态机推进到带最终结果的 completed。"""

    planning = transition_turn(
        current,
        status=TurnStatus.PLANNING,
        resume_point=TurnResumePoint.PLANNING,
        recovery_action=TurnRecoveryAction.WAIT,
    )
    await store.compare_and_swap(expected_phase_version=current.phase_version, updated=planning)
    narrating = transition_turn(
        planning,
        status=TurnStatus.AWAITING_NARRATION,
        resume_point=TurnResumePoint.NARRATING,
        commit_state=TurnCommitState.COMMITTED,
        recovery_action=TurnRecoveryAction.WAIT,
    )
    await store.compare_and_swap(expected_phase_version=planning.phase_version, updated=narrating)
    result = TurnResultSnapshot(
        message_id="message-final-1",
        narration={"text": "抽屉里放着一把黄铜钥匙。"},
        player_view={"inventory": ["brass-key"]},
        view_revision="revision-7",
    )
    delivering = transition_turn(
        narrating,
        status=TurnStatus.DELIVERING,
        resume_point=TurnResumePoint.DELIVERING,
        recovery_action=TurnRecoveryAction.FETCH_RESULT,
        result=result,
    )
    await store.compare_and_swap(expected_phase_version=narrating.phase_version, updated=delivering)
    completed = transition_turn(
        delivering,
        status=TurnStatus.COMPLETED,
        resume_point=TurnResumePoint.NONE,
        recovery_action=TurnRecoveryAction.FETCH_RESULT,
    )
    return await store.compare_and_swap(
        expected_phase_version=delivering.phase_version,
        updated=completed,
    )


@pytest.mark.asyncio
async def test_get_turn_returns_only_player_safe_projection(
    client: AsyncClient,
    db_session: AsyncSession,
    turn_store_factory,
) -> None:
    room_id, owner, other = await _members(db_session)
    store: SqlAlchemyTurnStore = turn_store_factory()
    current, _ = await store.create_or_get(_new_turn(room_id, owner.id, "action-safe"))
    completed = await _complete_turn(store, current)
    url = f"/api/v1/rooms/{room_id}/turns/{completed.turn_id}"

    unauthorized = await client.get(url)
    forbidden = await client.get(url, headers={"X-Reconnect-Token": other.reconnect_token})
    response = await client.get(url, headers={"X-Reconnect-Token": owner.reconnect_token})

    assert unauthorized.status_code == 401
    assert forbidden.status_code == 403
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["turnId"] == completed.turn_id
    assert data["status"] == "completed"
    assert data["messageId"] == "message-final-1"
    assert data["narration"] == {"text": "抽屉里放着一把黄铜钥匙。"}
    assert data["playerView"] == {"inventory": ["brass-key"]}
    assert data["viewRevision"] == "revision-7"
    assert data["createdAt"].endswith("Z") or data["createdAt"].endswith("+00:00")
    assert {"request", "utterance", "actorId", "inputFingerprint", "leaseOwner"}.isdisjoint(data)


@pytest.mark.asyncio
async def test_list_turns_filters_by_client_action_and_active_state(
    client: AsyncClient,
    db_session: AsyncSession,
    turn_store_factory,
) -> None:
    room_id, owner, _ = await _members(db_session)
    store: SqlAlchemyTurnStore = turn_store_factory()
    first, _ = await store.create_or_get(_new_turn(room_id, owner.id, "action-complete"))
    await _complete_turn(store, first)
    active, _ = await store.create_or_get(_new_turn(room_id, owner.id, "action-active"))
    headers = {"X-Reconnect-Token": owner.reconnect_token}

    active_response = await client.get(
        f"/api/v1/rooms/{room_id}/turns?activeOnly=true",
        headers=headers,
    )
    found_response = await client.get(
        f"/api/v1/rooms/{room_id}/turns?clientActionId=action-complete",
        headers=headers,
    )

    assert active_response.status_code == 200
    assert [item["turnId"] for item in active_response.json()["data"]] == [active.turn_id]
    assert found_response.status_code == 200
    assert [item["clientActionId"] for item in found_response.json()["data"]] == ["action-complete"]


@pytest.mark.asyncio
async def test_resume_endpoint_is_explicitly_unavailable_in_legacy_mode(
    client: AsyncClient,
    db_session: AsyncSession,
    turn_store_factory,
) -> None:
    room_id, owner, _ = await _members(db_session)
    store: SqlAlchemyTurnStore = turn_store_factory()
    turn, _ = await store.create_or_get(_new_turn(room_id, owner.id, "action-resume"))

    response = await client.post(
        f"/api/v1/rooms/{room_id}/turns/{turn.turn_id}/resume",
        headers={"X-Reconnect-Token": owner.reconnect_token},
    )

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "TURN_RESUME_UNAVAILABLE"
