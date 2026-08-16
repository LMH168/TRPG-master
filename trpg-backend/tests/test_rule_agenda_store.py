"""RuleAgenda 步骤执行证明的 SQL 外键、查询隔离与恢复测试。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from collaboration_framework.engine import AgendaStepExecution
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.turn_runtime import TurnCommitReceipt, TurnInputSnapshot, new_turn_record
from app.models.engine import AgendaStepExecutionRecord
from app.models.room import Player, Room
from app.models.turn import TurnCommitReceiptRecord


async def _room_player(db_session: AsyncSession) -> tuple[str, str]:
    """创建满足 Turn 与 execution 外键要求的隔离房间。"""

    room_id = str(uuid4())
    player_id = str(uuid4())
    db_session.add(
        Room(
            id=room_id,
            room_code=uuid4().hex[:6].upper(),
            room_name="Agenda Store 测试",
            max_players=4,
            phase="InGame",
        )
    )
    db_session.add(
        Player(
            id=player_id,
            room_id=room_id,
            nickname="调查员",
            is_host=True,
            reconnect_token=str(uuid4()),
        )
    )
    await db_session.commit()
    return room_id, player_id


def _turn(room_id: str, player_id: str):
    return new_turn_record(
        TurnInputSnapshot(
            room_id=room_id,
            player_id=player_id,
            actor_id="actor-1",
            client_action_id=f"action-{uuid4()}",
            utterance="调查地穴入口",
        ),
        now=datetime(2026, 8, 16, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_execution_requires_matching_receipt_and_is_room_scoped(
    db_session: AsyncSession,
    turn_store_factory,
    engine_store_factory,
) -> None:
    room_id, player_id = await _room_player(db_session)
    turn_store = turn_store_factory()
    turn, _ = await turn_store.create_or_get(_turn(room_id, player_id))
    execution_id = "a" * 64
    created_at = datetime(2026, 8, 16, 1, 0, tzinfo=UTC)

    # SQLite 测试连接不强制外键，直接检查 ORM 元数据，PostgreSQL 迁移测试再验证执行。
    foreign_key_targets = {
        foreign_key.target_fullname
        for foreign_key in AgendaStepExecutionRecord.__table__.foreign_keys
    }
    assert "turn_commit_receipts.room_id" in foreign_key_targets
    assert "turn_commit_receipts.engine_request_id" in foreign_key_targets

    receipt = TurnCommitReceipt(
        room_id=room_id,
        engine_request_id=execution_id,
        turn_id=turn.turn_id,
        action_request_id=turn.client_action_id,
        committed_state_version=3,
        first_event_sequence=None,
        last_event_sequence=None,
        created_at=created_at,
    )
    db_session.add(
        TurnCommitReceiptRecord(
            room_id=receipt.room_id,
            engine_request_id=receipt.engine_request_id,
            turn_id=receipt.turn_id,
            action_request_id=receipt.action_request_id,
            committed_state_version=receipt.committed_state_version,
            first_event_sequence=receipt.first_event_sequence,
            last_event_sequence=receipt.last_event_sequence,
            created_at=receipt.created_at,
        )
    )
    db_session.add(
        AgendaStepExecutionRecord(
            execution_id=execution_id,
            room_id=room_id,
            origin_turn_id=turn.turn_id,
            execution_turn_id=turn.turn_id,
            agenda_id="agenda-1",
            source_event_id="event-1",
            rule_id="rule-1",
            branch_id="branch-1",
            step_id="step-1",
            execution_kind="passive_check",
            schema_version=1,
            request_schema_version=1,
            request_json={"profile": "coc7.skill"},
            result_schema_version=1,
            result_json={"passed": True},
            committed_state_version=3,
            created_at=created_at,
        )
    )
    await db_session.commit()

    store = engine_store_factory()
    loaded = await store.find_agenda_step_execution(
        room_id=room_id,
        execution_id=execution_id,
    )
    assert loaded == AgendaStepExecution(
        execution_id=execution_id,
        room_id=room_id,
        origin_turn_id=turn.turn_id,
        execution_turn_id=turn.turn_id,
        agenda_id="agenda-1",
        source_event_id="event-1",
        rule_id="rule-1",
        branch_id="branch-1",
        step_id="step-1",
        execution_kind="passive_check",
        request={"profile": "coc7.skill"},
        result={"passed": True},
        committed_state_version=3,
        created_at=created_at,
    )
    assert (
        await store.find_agenda_step_execution(room_id=str(uuid4()), execution_id=execution_id)
        is None
    )
