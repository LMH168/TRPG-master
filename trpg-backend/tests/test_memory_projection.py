"""验证事件驱动 Memory 投影的幂等、隐私、认知边界与恢复。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import pytest
from collaboration_framework.memory import (
    InMemoryMemoryStore,
    MemoryBudget,
    MemoryProjectionEvent,
    MemoryProjectionNarration,
    MemoryProjectionSource,
    MemoryProjectionStep,
    MemoryQuery,
    MemoryReadScope,
    new_memory_projection_run,
    project_memory_entries,
)

from app.core.turn_runtime import (
    TurnCommitReceipt,
    TurnCommitState,
    TurnInputSnapshot,
    TurnRecoveryAction,
    TurnResumePoint,
    TurnStatus,
    new_turn_record,
    transition_turn,
)
from app.models.room import Player, Room
from app.service.memory_projection import MemoryProjectionSupervisor

NOW = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)


def _source(
    *,
    turn_id: str = "turn-1",
    status: Literal["completed", "failed", "cancelled"] = "completed",
    receipts: tuple[str, ...] = ("receipt-1",),
) -> MemoryProjectionSource:
    return MemoryProjectionSource(
        turn_id=turn_id,
        room_id="room-1",
        player_id="player-1",
        actor_id="actor-1",
        utterance="我告诉守墓人今晚见过墓碑旁的人影",
        turn_status=status,
        commit_state="committed" if receipts else "not_committed",
        receipt_ids=receipts,
        steps=(
            MemoryProjectionStep(
                source_id="action-1",
                semantic_goal="告诉守墓人今晚见过墓碑旁的人影",
                status="completed",
                outcome="success",
                goal_outcome="achieved",
                has_receipt=bool(receipts),
                target_interaction="social",
                focus_kind="entity",
                focus_id="caretaker",
            ),
        ),
        events=(
            MemoryProjectionEvent(
                event_id="event-location",
                sequence=1,
                event_type="travel.resolved",
                actor_id="actor-1",
                visibility="public",
                payload={"destination_id": "cemetery"},
                created_at=NOW,
            ),
            MemoryProjectionEvent(
                event_id="event-information",
                sequence=2,
                event_type="information.revealed",
                actor_id="actor-1",
                visibility="public",
                payload={"information_id": "night-sighting"},
                created_at=NOW + timedelta(seconds=1),
            ),
            MemoryProjectionEvent(
                event_id="event-hidden",
                sequence=3,
                event_type="world.secret_changed",
                actor_id="keeper",
                visibility="hidden",
                payload={"secret": "keeper-only"},
                created_at=NOW + timedelta(seconds=2),
            ),
        ),
        narration=MemoryProjectionNarration(
            source_id="replay-1",
            text="守墓人听完你的说法，表示自己会留意。",
            visibility="public",
            scene_id="cemetery",
            created_at=NOW + timedelta(seconds=3),
        ),
        created_at=NOW,
        completed_at=NOW + timedelta(seconds=4),
    )


def test_projection_uses_receipts_and_explicit_social_participant() -> None:
    entries = project_memory_entries(_source())

    assert {entry.kind for entry in entries} == {
        "completed_action",
        "conversation",
        "location_visit",
        "discovered_information",
    }
    heard = next(
        entry for entry in entries if entry.kind == "conversation" and entry.scope == "entity"
    )
    assert heard.scope_owner_id == "caretaker"
    assert heard.epistemic_status == "heard"
    assert heard.content["speaker_id"] == "actor-1"
    assert all("keeper-only" not in entry.search_text for entry in entries)
    assert all(entry.source_event_id != "event-hidden" for entry in entries)
    presentation = next(entry for entry in entries if entry.epistemic_status == "presentation_only")
    assert presentation.scope == "player"


def test_action_plan_conversation_does_not_store_verbatim_parent_utterance() -> None:
    source = _source().model_copy(
        update={
            "utterance": "先调查墓地，然后把秘密原话告诉守墓人",
            "steps": (
                MemoryProjectionStep(
                    source_id="action-observe",
                    semantic_goal="调查墓地",
                    status="completed",
                    outcome="success",
                    goal_outcome="achieved",
                    has_receipt=True,
                    target_interaction="observe",
                    focus_kind="location",
                    focus_id="cemetery",
                ),
                MemoryProjectionStep(
                    source_id="action-social",
                    semantic_goal="向守墓人说明调查主题",
                    status="completed",
                    outcome="success",
                    goal_outcome="achieved",
                    has_receipt=True,
                    target_interaction="social",
                    focus_kind="entity",
                    focus_id="caretaker",
                ),
            ),
        }
    )

    conversations = tuple(
        entry
        for entry in project_memory_entries(source)
        if entry.kind == "conversation" and entry.epistemic_status != "presentation_only"
    )
    assert conversations
    assert {entry.content["summary"] for entry in conversations} == {"向守墓人说明调查主题"}


def test_failed_turn_without_receipt_only_projects_unresolved_goal() -> None:
    source = _source(status="failed", receipts=()).model_copy(
        update={"steps": (), "events": (), "narration": None}
    )

    entries = project_memory_entries(source)

    assert len(entries) == 1
    assert entries[0].kind == "unresolved_goal"
    assert entries[0].epistemic_status == "asserted"
    assert entries[0].visibility == "player_scoped"


class _Source:
    def __init__(self, sources: tuple[MemoryProjectionSource, ...]) -> None:
        self.sources = {source.turn_id: source for source in sources}

    async def list_unregistered_turn_ids(
        self, *, room_id: str | None = None, limit: int = 20
    ) -> tuple[str, ...]:
        matches = (
            turn_id
            for turn_id, source in self.sources.items()
            if room_id is None or source.room_id == room_id
        )
        return tuple(matches)[:limit]

    async def load(self, turn_id: str) -> MemoryProjectionSource | None:
        return self.sources.get(turn_id)


class _UnavailableSource:
    """模拟启动阶段数据库不可用，验证派生投影不会阻止主服务启动。"""

    async def list_unregistered_turn_ids(
        self, *, room_id: str | None = None, limit: int = 20
    ) -> tuple[str, ...]:
        raise RuntimeError("source unavailable")

    async def load(self, turn_id: str) -> MemoryProjectionSource | None:
        raise RuntimeError("source unavailable")


@pytest.mark.asyncio
async def test_supervisor_start_does_not_block_on_projection_failure() -> None:
    supervisor = MemoryProjectionSupervisor(
        store=InMemoryMemoryStore(),
        source=_UnavailableSource(),
        worker_id="worker-a",
        interval_seconds=60,
    )

    await supervisor.start()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_supervisor_is_idempotent_and_keeps_player_scope_private() -> None:
    store = InMemoryMemoryStore()
    source = _Source((_source(),))
    supervisor = MemoryProjectionSupervisor(
        store=store,
        source=source,
        worker_id="worker-a",
    )

    first = await supervisor.run_once()
    second = await supervisor.run_once()

    assert first.registered == 1
    assert first.projected == 1
    assert second.projected == 0
    own = await store.read_context(
        scope=MemoryReadScope(
            room_id="room-1",
            viewer_player_id="player-1",
            viewer_actor_id="actor-1",
            as_of_revision="1",
            current_location_id="cemetery",
            visible_entity_ids=("caretaker",),
        ),
        query=MemoryQuery(),
        budget=MemoryBudget(max_entries=32, max_chars=20000),
    )
    other = await store.read_context(
        scope=MemoryReadScope(
            room_id="room-1",
            viewer_player_id="player-2",
            viewer_actor_id="actor-2",
            as_of_revision="1",
            current_location_id="cemetery",
            visible_entity_ids=("caretaker",),
        ),
        query=MemoryQuery(),
        budget=MemoryBudget(max_entries=32, max_chars=20000),
    )
    assert own.entries
    assert all(entry.viewer_player_id != "player-2" for entry in own.entries)
    assert {entry.kind for entry in other.entries} == {"discovered_information"}


@pytest.mark.asyncio
async def test_supervisor_retries_five_times_then_dead_letters() -> None:
    store = InMemoryMemoryStore()
    source = _Source((_source(),))
    supervisor = MemoryProjectionSupervisor(
        store=store,
        source=source,
        worker_id="worker-a",
    )
    await supervisor._register("turn-1")
    source.sources.clear()

    # 注册时间来自真实时钟；从其后开始推进，避免固定日期跨过后少执行一次领取。
    moment = datetime.now(UTC) + timedelta(seconds=1)
    reports = []
    for _ in range(5):
        reports.append(await supervisor._project("turn-1", now=moment))
        moment += timedelta(minutes=2)

    run = await store.get_run("turn-1")
    assert run is not None
    assert run.status == "dead_letter"
    assert run.attempt_count == 5
    assert sum(report.retry for report in reports) == 4
    assert reports[-1].dead_letter == 1


@pytest.mark.asyncio
async def test_supervisor_recovers_expired_lease_after_restart() -> None:
    store = InMemoryMemoryStore()
    source_value = _source()
    source = _Source((source_value,))
    await store.create_or_get_run(
        new_memory_projection_run(
            turn_id=source_value.turn_id,
            room_id=source_value.room_id,
            source_fingerprint=source_value.fingerprint(),
            now=NOW,
        )
    )
    await store.claim_run(
        turn_id=source_value.turn_id,
        worker_id="stopped-worker",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=1),
    )
    recovered = MemoryProjectionSupervisor(
        store=store,
        source=source,
        worker_id="restarted-worker",
    )

    report = await recovered._project(
        source_value.turn_id,
        now=NOW + timedelta(seconds=2),
    )

    run = await store.get_run(source_value.turn_id)
    assert report.projected == 1
    assert run is not None
    assert run.status == "completed"
    assert run.lease_owner is None


@pytest.mark.asyncio
async def test_completed_history_without_receipt_is_audited_but_not_projected() -> None:
    store = InMemoryMemoryStore()
    source = _Source((_source(receipts=()),))
    supervisor = MemoryProjectionSupervisor(
        store=store,
        source=source,
        worker_id="worker-a",
    )

    report = await supervisor.run_once()

    run = await store.get_run("turn-1")
    assert report.skipped == 1
    assert run is not None
    assert run.status == "completed"
    context = await store.read_context(
        scope=MemoryReadScope(
            room_id="room-1",
            viewer_player_id="player-1",
            viewer_actor_id="actor-1",
            as_of_revision="1",
            current_location_id="cemetery",
            visible_entity_ids=("caretaker",),
        ),
        query=MemoryQuery(),
        budget=MemoryBudget(max_entries=32, max_chars=20000),
    )
    assert context.entries == ()


@pytest.mark.asyncio
async def test_rebuild_uses_same_projection_without_duplicates() -> None:
    store = InMemoryMemoryStore()
    source = _Source((_source(),))
    supervisor = MemoryProjectionSupervisor(
        store=store,
        source=source,
        worker_id="worker-a",
    )

    first = await supervisor.rebuild_room("room-1")
    second = await supervisor.rebuild_room("room-1")

    assert first.projected == 1
    assert second.projected == 1
    context = await store.read_context(
        scope=MemoryReadScope(
            room_id="room-1",
            viewer_player_id="player-1",
            viewer_actor_id="actor-1",
            as_of_revision="1",
            current_location_id="cemetery",
            visible_entity_ids=("caretaker",),
        ),
        query=MemoryQuery(),
        budget=MemoryBudget(max_entries=32, max_chars=20000),
    )
    assert len(context.entries) == len({entry.memory_id for entry in context.entries})


@pytest.mark.asyncio
async def test_sql_source_discovers_terminal_turn_and_receipt(
    db_session,
    turn_store_factory,
    memory_projection_source_factory,
) -> None:
    """真实 SQL 读取必须保留 Turn 身份、receipt 与稳定来源摘要。"""

    room_id = str(uuid4())
    player_id = str(uuid4())
    db_session.add(
        Room(
            id=room_id,
            room_code=uuid4().hex[:6].upper(),
            room_name="Memory Source 测试",
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
    store = turn_store_factory()
    turn, _ = await store.create_or_get(
        new_turn_record(
            TurnInputSnapshot(
                room_id=room_id,
                player_id=player_id,
                actor_id="actor-1",
                client_action_id="action-1",
                utterance="调查墓地",
            ),
            now=NOW,
        )
    )
    planning = transition_turn(
        turn,
        status=TurnStatus.PLANNING,
        resume_point=TurnResumePoint.PLANNING,
        recovery_action=TurnRecoveryAction.WAIT,
    )
    planning = await store.compare_and_swap(
        expected_phase_version=turn.phase_version,
        updated=planning,
    )
    failed = transition_turn(
        planning,
        status=TurnStatus.FAILED,
        resume_point=TurnResumePoint.NONE,
        commit_state=TurnCommitState.COMMITTED,
        recovery_action=TurnRecoveryAction.SUBMIT_NEW_INPUT,
    )
    await store.compare_and_swap(
        expected_phase_version=planning.phase_version,
        updated=failed,
    )
    await store.append_receipt(
        TurnCommitReceipt(
            turn_id=turn.turn_id,
            room_id=room_id,
            engine_request_id="engine-1",
            action_request_id="action-1",
            committed_state_version=1,
            created_at=NOW,
        )
    )

    source_store = memory_projection_source_factory()
    discovered = await source_store.list_unregistered_turn_ids(room_id=room_id)
    source = await source_store.load(turn.turn_id)

    assert discovered == (turn.turn_id,)
    assert source is not None
    assert source.receipt_ids == ("engine-1",)
    assert source.is_reliably_projectable is True
    assert source.fingerprint() == source.fingerprint()
