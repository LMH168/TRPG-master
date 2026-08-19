"""Phase 1B 上下文、意图和叙事边界的契约测试。"""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.dto.gm import (
    ActionCandidate,
    CommandEnvelope,
    ContextSnapshot,
    IntentResult,
    IntentStep,
    NarrationDraft,
    TurnInputBody,
)
from app.models.gm import GameEvent, TurnRun
from app.models.room import Room
from app.service.gm_ai import (
    ScriptedIntentInterpreter,
    build_context_snapshot,
    guard_narration,
    validate_intent,
)
from app.service.gm_runtime import (
    GmRuntimeError,
    create_session,
    read_projection,
    set_agents_for_testing,
    submit_command,
    submit_free_text,
)
from tests.helpers import bearer, create_room, reconnect, register


class _ScriptedNarrator:
    """测试用叙事器，只表达传入的事件证据。"""

    async def narrate(self, snapshot, event_ids, facts):  # noqa: ANN001
        """返回固定的、带事件引用的安全叙事。"""

        return NarrationDraft(text="你观察了当前地点。", evidence_event_ids=list(event_ids))


def _snapshot() -> ContextSnapshot:
    """构造只包含墓园公开候选的最小玩家快照。"""

    return ContextSnapshot(
        snapshot_id="ctx-1",
        session_id="room-1",
        actor_id="actor-1",
        audience="private:actor-1",
        revision=3,
        world_time=datetime.now(UTC),
        location_id="cemetery",
        action_candidates=[
            ActionCandidate(action="inspect_target", target_id="headstone", label="检查墓碑"),
            ActionCandidate(action="talk_to_npc", target_id="gravekeeper", label="与守墓人交谈"),
        ],
    )


def test_intent_validator_accepts_visible_target_only() -> None:
    """模型只能引用快照候选，不能凭空指定 keeper 或隐藏对象。"""

    result = IntentResult(
        kind="proposal",
        summary="检查墓碑",
        source_revision=3,
        steps=[IntentStep(action="inspect_target", target_id="headstone")],
    )
    assert validate_intent(_snapshot(), result).steps[0].target_id == "headstone"

    hidden = result.model_copy(
        update={"steps": [IntentStep(action="inspect_target", target_id="keeper_secret")]}
    )
    with pytest.raises(ValueError, match="候选"):
        validate_intent(_snapshot(), hidden)


def test_ambiguous_intent_must_ask_for_target() -> None:
    """缺少目标的侦察不能被模型默认为某个隐藏对象。"""

    clarification = IntentResult(
        kind="clarification",
        summary="需要确认侦察对象",
        source_revision=3,
        clarification_question="你想观察墓碑还是守墓人？",
        clarification_options=["墓碑", "守墓人"],
    )
    assert validate_intent(_snapshot(), clarification).kind == "clarification"


def test_narration_guard_rejects_uncommitted_event_and_secret_claim() -> None:
    """叙事不能引用未提交事件，也不能借文学表达泄露 keeper 信息。"""

    with pytest.raises(ValueError, match="未提交"):
        guard_narration(
            NarrationDraft(text="你看到异常。", evidence_event_ids=["event-2"]),
            committed_event_ids=["event-1"],
            visible_facts=[],
        )
    with pytest.raises(ValueError, match="隐藏"):
        guard_narration(
            NarrationDraft(text="守墓人复活并说出模组真相。", evidence_event_ids=["event-1"]),
            committed_event_ids=["event-1"],
            visible_facts=[],
        )


def test_snapshot_rejects_unknown_fields() -> None:
    """上下文 DTO 严格拒绝模型或调用方偷偷加入 keeper 字段。"""

    with pytest.raises(ValidationError):
        ContextSnapshot.model_validate({**_snapshot().model_dump(), "keeper": {"truth": "x"}})


async def test_context_and_free_text_turn_never_expose_keeper_data(db_session) -> None:
    """自然语言回合经由 Kernel 提交后，快照和返回结果均不含 keeper 字段。"""

    room_id = "00000000-0000-0000-0000-000000000198"
    db_session.add(Room(id=room_id, room_code="P1B1", room_name="Phase 1B", max_players=1))
    await db_session.commit()
    await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id="actor-1",
        display_name="调查员",
    )
    snapshot = await build_context_snapshot(db_session, room_id=room_id, actor_id="actor-1")
    assert "keeper" not in snapshot.model_dump_json()
    set_agents_for_testing(
        ScriptedIntentInterpreter(
            [
                IntentResult(
                    kind="proposal",
                    summary="检查城镇标牌",
                    source_revision=0,
                    steps=[IntentStep(action="inspect_target", target_id="town_sign")],
                )
            ]
        ),
        _ScriptedNarrator(),
    )
    try:
        result = await submit_free_text(
            db_session,
            room_id=room_id,
            payload=TurnInputBody(
                client_request_id="turn-p1b-1",
                actor_id="actor-1",
                expected_revision=0,
                input="观察城镇标牌",
            ),
        )
    finally:
        set_agents_for_testing(None)
    assert result.status == "completed"
    assert result.narration == "你观察了当前地点。"
    assert "keeper" not in result.model_dump_json()


async def test_failed_model_turn_can_resume_without_duplicate_kernel_effect(db_session) -> None:
    """模型恢复后重试同一请求，只允许 Kernel 成功提交一次。"""

    room_id = "00000000-0000-0000-0000-000000000199"
    db_session.add(Room(id=room_id, room_code="P1B2", room_name="Phase 1B 恢复", max_players=1))
    await db_session.commit()
    await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id="actor-2",
        display_name="调查员",
    )
    payload = TurnInputBody(
        client_request_id="turn-p1b-retry",
        actor_id="actor-2",
        expected_revision=0,
        input="观察城镇标牌",
    )
    set_agents_for_testing(ScriptedIntentInterpreter([]), _ScriptedNarrator())
    with pytest.raises(GmRuntimeError, match="gm_unavailable"):
        await submit_free_text(db_session, room_id=room_id, payload=payload)
    assert await db_session.scalar(select(func.count()).select_from(GameEvent)) == 0
    failed_turn = await db_session.scalar(
        select(TurnRun).where(TurnRun.client_request_id == payload.client_request_id)
    )
    assert failed_turn is not None and failed_turn.status == "failed"

    set_agents_for_testing(
        ScriptedIntentInterpreter(
            [
                IntentResult(
                    kind="proposal",
                    summary="检查城镇标牌",
                    source_revision=0,
                    steps=[IntentStep(action="inspect_target", target_id="town_sign")],
                )
            ]
        ),
        _ScriptedNarrator(),
    )
    try:
        result = await submit_free_text(db_session, room_id=room_id, payload=payload)
        replay = await submit_free_text(db_session, room_id=room_id, payload=payload)
    finally:
        set_agents_for_testing(None)
    assert result.revision == replay.revision == 1
    assert await db_session.scalar(select(func.count()).select_from(GameEvent)) == 1


async def test_browser_credentials_can_create_session_and_submit_free_text(
    client: AsyncClient,
) -> None:
    """浏览器用账号加房间凭证建会话，随后只用房间凭证提交自己的行动。"""

    account_token = await register(client, nickname="调查员")
    room = await create_room(client, token=account_token, max_players=1)
    create_headers = {
        **bearer(account_token),
        **reconnect(room["reconnectToken"]),
    }
    created = await client.post(
        "/api/v1/gm/sessions",
        headers=create_headers,
        json={
            "roomId": room["roomId"],
            "moduleId": "paper-chase",
            "actorId": room["playerId"],
            "displayName": "调查员",
        },
    )
    assert created.status_code == 201, created.text
    set_agents_for_testing(
        ScriptedIntentInterpreter(
            [
                IntentResult(
                    kind="proposal",
                    summary="检查城镇标牌",
                    source_revision=0,
                    steps=[IntentStep(action="inspect_target", target_id="town_sign")],
                )
            ]
        ),
        _ScriptedNarrator(),
    )
    try:
        response = await client.post(
            f"/api/v1/gm/sessions/{room['roomId']}/turns/free-text",
            headers=reconnect(room["reconnectToken"]),
            json={
                "clientRequestId": "browser-turn-1",
                "actorId": room["playerId"],
                "expectedRevision": 0,
                "input": "观察城镇标牌",
            },
        )
    finally:
        set_agents_for_testing(None)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "completed"


async def test_pending_roll_is_restored_and_blocks_new_intent(db_session) -> None:
    """刷新可恢复待投骰；玩家完成它以前不能让模型发起另一个权威动作。"""

    room_id = "00000000-0000-0000-0000-000000000200"
    db_session.add(Room(id=room_id, room_code="P1B3", room_name="Phase 1B 投骰", max_players=1))
    await db_session.commit()
    await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id="actor-3",
        display_name="调查员",
    )
    started = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="start-check-p1b",
            expected_revision=0,
            actor_id="actor-3",
            command={
                "kind": "start_check",
                "check_id": "check-p1b",
                "skill_id": "spot-hidden",
                "goal": "观察墓碑",
            },
        ),
    )
    assert started.projection.pending_decisions[0].check_id == "check-p1b"
    restored = await read_projection(db_session, room_id=room_id, actor_id="actor-3")
    assert restored.checks[0].status == "awaiting_roll"
    with pytest.raises(GmRuntimeError, match="待投骰"):
        await submit_free_text(
            db_session,
            room_id=room_id,
            payload=TurnInputBody(
                client_request_id="blocked-by-roll",
                actor_id="actor-3",
                expected_revision=1,
                input="我先去图书馆",
            ),
        )
    rolled = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="roll-check-p1b",
            expected_revision=1,
            actor_id="actor-3",
            command={"kind": "roll_check", "check_id": "check-p1b"},
        ),
    )
    assert rolled.check is not None and rolled.check.roll is not None
    after_roll = await read_projection(db_session, room_id=room_id, actor_id="actor-3")
    assert after_roll.pending_decisions == []
