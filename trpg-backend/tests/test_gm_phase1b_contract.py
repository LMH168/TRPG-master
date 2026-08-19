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
from app.models.gm import GameEvent, RuntimeActor, TurnRun
from app.models.room import Character, Player, Room
from app.service.gm_ai import (
    GmModelUnavailable,
    ScriptedIntentInterpreter,
    build_context_snapshot,
    guard_clarification,
    guard_intent_coverage,
    guard_move_target,
    guard_narration,
    intent_step_to_command,
    validate_intent,
)
from app.service.gm_runtime import (
    GmRuntimeError,
    _time_narration_forbidden_terms,
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


class _RecordingNarrator(_ScriptedNarrator):
    """记录 Narrator 实际收到的快照，用于防止未来动作泄露。"""

    snapshot = None

    async def narrate(self, snapshot, event_ids, facts):  # noqa: ANN001
        """保存玩家安全快照后返回固定叙事。"""

        self.snapshot = snapshot
        return await super().narrate(snapshot, event_ids, facts)


class _LeakingNarrator(_ScriptedNarrator):
    """测试用越界叙事器，模拟模型提前补写未解锁剧情。"""

    async def narrate(self, snapshot, event_ids, facts):  # noqa: ANN001
        """返回引用正确事件但包含未公开内容的草稿。"""

        return NarrationDraft(
            text="你踏入墓园，立即看见地穴入口旁的人影。",
            evidence_event_ids=list(event_ids),
        )


class _CountingNarrator(_ScriptedNarrator):
    """记录投骰后续写次数，验证幂等回执不重复调模型。"""

    def __init__(self) -> None:
        """初始化调用计数。"""

        self.calls = 0

    async def narrate(self, snapshot, event_ids, facts):  # noqa: ANN001
        """记录调用并返回安全续写。"""

        self.calls += 1
        return NarrationDraft(text="你从痕迹中得到了新的判断。", evidence_event_ids=list(event_ids))


class _RetryNarrator(_CountingNarrator):
    """首次失败、第二次成功，用于验证只重试无副作用续写。"""

    async def narrate(self, snapshot, event_ids, facts):  # noqa: ANN001
        """模拟一次临时的结构化输出失败。"""

        self.calls += 1
        if self.calls == 1:
            raise GmModelUnavailable("临时失败")
        return NarrationDraft(text="你从痕迹中得到了新的判断。", evidence_event_ids=list(event_ids))


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


def test_clarification_guard_replaces_spoilers_and_internal_ids() -> None:
    """澄清模型不能把未来剧情或候选内部 ID 直接展示给玩家。"""

    leaked = IntentResult(
        kind="clarification",
        source_revision=3,
        clarification_question="请从行动列表选择 talk_douglas，或者前往地穴。",
        clarification_options=["talk_douglas", "进入地穴"],
    )
    guarded = guard_clarification(
        leaked,
        forbidden_terms=["talk_douglas", "地穴"],
    )
    assert guarded.clarification_question == (
        "我还不能确定你现在想做什么，请换一种方式描述当前行动。"
    )
    assert guarded.clarification_options == []


def test_clarification_guard_keeps_safe_host_question() -> None:
    """正常的主持人澄清保持原样，不把安全门禁变成机械固定回复。"""

    clarification = IntentResult(
        kind="clarification",
        source_revision=3,
        clarification_question="你想观察墓碑，还是与守墓人交谈？",
        clarification_options=["观察墓碑", "与守墓人交谈"],
    )
    assert guard_clarification(clarification) == clarification


def test_unique_npc_question_keeps_topic_when_bound_to_command() -> None:
    """明确向唯一 NPC 提问时保留具体话题，不再退化成二次确认。"""

    result = validate_intent(
        _snapshot(),
        IntentResult(
            kind="proposal",
            source_revision=3,
            steps=[
                IntentStep(
                    action="talk_to_npc",
                    target_id="gravekeeper",
                    topic="平静地问他这一年去了哪里",
                )
            ],
        ),
    )
    envelope = intent_step_to_command(
        result.steps[0],
        client_request_id="talk-topic",
        expected_revision=3,
        actor_id="actor-1",
    )
    assert envelope.command.kind == "talk_to_npc"
    assert envelope.command.topic == "平静地问他这一年去了哪里"


def test_single_move_cannot_silently_drop_followup_action() -> None:
    """移动加调查必须停在明确边界，不能把后半句当作已经处理。"""

    snapshot = _snapshot().model_copy(
        update={
            "action_candidates": [
                ActionCandidate(action="move_actor", target_id="library", label="前往图书馆")
            ]
        }
    )
    proposal = IntentResult(
        kind="proposal",
        source_revision=3,
        steps=[IntentStep(action="move_actor", target_id="library")],
    )
    guarded = guard_intent_coverage(
        snapshot,
        proposal,
        "我先去图书馆，查找关于失踪者的旧资料",
    )
    assert guarded.kind == "clarification"
    assert "到达后的行动" in (guarded.clarification_question or "")
    assert guard_intent_coverage(snapshot, proposal, "我前往图书馆") == proposal


def test_move_target_cannot_be_replaced_by_another_reachable_location() -> None:
    """玩家明确说图书馆时，模型不能把移动目标改成阿卡姆中转点。"""

    snapshot = _snapshot().model_copy(
        update={
            "action_candidates": [
                ActionCandidate(action="move_actor", target_id="town", label="回到阿卡姆"),
                ActionCandidate(action="move_actor", target_id="library", label="前往图书馆"),
            ]
        }
    )
    proposal = IntentResult(
        kind="proposal",
        source_revision=3,
        steps=[IntentStep(action="move_actor", target_id="town")],
    )
    guarded = guard_move_target(snapshot, proposal, "前往图书馆")
    assert guarded.kind == "clarification"
    assert guarded.source_revision == 3
    assert guard_move_target(snapshot, proposal, "回去") == proposal

    library_proposal = proposal.model_copy(
        update={"steps": [IntentStep(action="move_actor", target_id="library")]}
    )
    assert guard_move_target(snapshot, library_proposal, "前往图书馆") == library_proposal


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
    with pytest.raises(ValueError, match="投骰前"):
        guard_narration(
            NarrationDraft(text="你的侦查检定成功了。", evidence_event_ids=["event-1"]),
            committed_event_ids=["event-1"],
            visible_facts=["你准备对检查周围环境使用侦查检定。"],
        )
    with pytest.raises(ValueError, match="尚未公开"):
        guard_narration(
            NarrationDraft(text="你看见地穴入口旁站着人影。", evidence_event_ids=["event-1"]),
            committed_event_ids=["event-1"],
            visible_facts=["你来到了墓园。"],
            forbidden_terms=["地穴", "人影"],
        )


def test_narration_guard_rejects_the_real_candidate_list_leak() -> None:
    """旧事故中的未来剧情和内部行动列表不得再次发给玩家。"""

    leaked = (
        "你站在墓园门前，远处的人影正走向地穴入口。"
        "你注意到行动列表中包含：与守墓人交谈、攻击人影、面对食尸鬼群。"
        "请从行动列表中选择一个行动，不能自行创建新行动。"
    )
    with pytest.raises(ValueError, match="隐藏"):
        guard_narration(
            NarrationDraft(text=leaked, evidence_event_ids=["event-1"]),
            committed_event_ids=["event-1"],
            visible_facts=["你从阿诺兹堡来到了公共墓地。"],
            forbidden_terms=["地穴", "人影", "食尸鬼"],
        )
    with pytest.raises(ValueError, match="未完成"):
        guard_narration(
            NarrationDraft(text="你听见对方的回应：", evidence_event_ids=["event-1"]),
            committed_event_ids=["event-1"],
            visible_facts=["人影停下脚步。"],
        )


def test_narration_guard_rejects_control_text_and_internal_ids() -> None:
    """控制层话术和运行包标识不能伪装成主持叙事返回。"""

    for text in (
        "你可以从选项中选择下一步行动：与守墓人交谈。",
        "当前可执行目标是 talk_douglas。",
    ):
        with pytest.raises(ValueError):
            guard_narration(
                NarrationDraft(text=text, evidence_event_ids=["event-1"]),
                committed_event_ids=["event-1"],
                visible_facts=["你站在墓园中。"],
            )


def test_narration_guard_rejects_unsupported_observation_details() -> None:
    """模型不能用文学描写增加证据中没有的物品、动作或秘密。"""

    with pytest.raises(ValueError, match="观察或动作"):
        guard_narration(
            NarrationDraft(
                text="守墓人冷淡地扫了你一眼，继续摆弄手中的铁锹，那里似乎藏着秘密。",
                evidence_event_ids=["event-1"],
            ),
            committed_event_ids=["event-1"],
            visible_facts=["魅惑检定失败。", "守墓人知道道格拉斯常坐的墓碑。"],
        )

    with pytest.raises(ValueError, match="观察或动作"):
        guard_narration(
            NarrationDraft(
                text="守墓人站在一旁，拍了拍手上的灰。",
                evidence_event_ids=["event-1"],
            ),
            committed_event_ids=["event-1"],
            visible_facts=["追踪检定失败。"],
        )

    # 规则明确给出对应证据时，正常叙事仍然可以表达该事实。
    grounded = guard_narration(
        NarrationDraft(text="你听见远处传来钟声。", evidence_event_ids=["event-1"]),
        committed_event_ids=["event-1"],
        visible_facts=["你听见远处传来钟声。"],
    )
    assert grounded.text == "你听见远处传来钟声。"


def test_narration_guard_rejects_afternoon_claim_at_half_past_five() -> None:
    """17:30 已属傍晚，不得再输出真实回放中的“时值午后”。"""

    forbidden_terms = _time_narration_forbidden_terms(
        datetime.fromisoformat("1920-09-15T17:30:00-04:00")
    )
    assert "午后" in forbidden_terms
    with pytest.raises(ValueError, match="尚未公开"):
        guard_narration(
            NarrationDraft(
                text="时值午后，秋天的余晖将墓园染成暗金色。",
                evidence_event_ids=["event-1"],
            ),
            committed_event_ids=["event-1"],
            visible_facts=["你从阿诺兹堡来到了公共墓地。"],
            forbidden_terms=forbidden_terms,
        )


def test_narration_guard_rejects_unsupported_duration_claim() -> None:
    """公开事实为失踪一年时，Narrator 不能擅自改成失踪数日。"""

    with pytest.raises(ValueError, match="时间长度"):
        guard_narration(
            NarrationDraft(
                text="你认出他就是失踪数日的道格拉斯。",
                evidence_event_ids=["event-1"],
            ),
            committed_event_ids=["event-1"],
            visible_facts=["道格拉斯已经失踪一年。", "你认出他就是道格拉斯。"],
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
    assert "道格拉斯已经失踪一年。" in snapshot.visible_facts
    narrator = _RecordingNarrator()
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
        narrator,
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
    assert narrator.snapshot is not None and narrator.snapshot.action_candidates == []


async def test_projection_does_not_restore_superseded_clarification(db_session) -> None:
    """玩家已提交后续行动时，刷新不得重新挂起更早的澄清问题。"""

    room_id = "00000000-0000-0000-0000-000000000211"
    actor_id = "actor-211"
    db_session.add(Room(id=room_id, room_code="P1B7", room_name="Phase 1B 澄清", max_players=1))
    await db_session.commit()
    await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id=actor_id,
        display_name="调查员",
    )
    set_agents_for_testing(
        ScriptedIntentInterpreter(
            [
                IntentResult(
                    kind="clarification",
                    summary="需要确认",
                    source_revision=0,
                    clarification_question="你想等到几点？",
                ),
                IntentResult(
                    kind="proposal",
                    summary="观察标牌",
                    source_revision=0,
                    steps=[IntentStep(action="inspect_target", target_id="town_sign")],
                ),
            ]
        ),
        _ScriptedNarrator(),
    )
    try:
        first = await submit_free_text(
            db_session,
            room_id=room_id,
            payload=TurnInputBody(
                client_request_id="clarify-211",
                actor_id=actor_id,
                expected_revision=0,
                input="等一会儿",
            ),
        )
        assert first.status == "clarification"
        await submit_free_text(
            db_session,
            room_id=room_id,
            payload=TurnInputBody(
                client_request_id="action-211",
                actor_id=actor_id,
                expected_revision=0,
                input="观察标牌",
            ),
        )
    finally:
        set_agents_for_testing(None)

    restored = await read_projection(db_session, room_id=room_id, actor_id=actor_id)
    assert restored.pending_clarification is None


async def test_free_text_move_with_followup_stops_before_kernel(db_session) -> None:
    """模型只返回移动时，服务端仍须识别被遗漏的后续调查并保持 revision。"""

    room_id = "00000000-0000-0000-0000-000000000214"
    actor_id = "actor-214"
    db_session.add(Room(id=room_id, room_code="P1BC", room_name="复合行动边界", max_players=1))
    await db_session.commit()
    await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id=actor_id,
        display_name="调查员",
    )
    set_agents_for_testing(
        ScriptedIntentInterpreter(
            [
                IntentResult(
                    kind="proposal",
                    source_revision=0,
                    steps=[IntentStep(action="move_actor", target_id="library")],
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
                client_request_id="composite-214",
                actor_id=actor_id,
                expected_revision=0,
                input="我先去图书馆，查找关于失踪者的旧资料",
            ),
        )
    finally:
        set_agents_for_testing(None)

    assert result.status == "clarification"
    assert result.revision == 0
    assert "到达后的行动" in (result.clarification_question or "")
    assert await db_session.scalar(select(func.count()).select_from(GameEvent)) == 0


async def test_free_text_turn_rejects_module_spoiler_before_clue_unlock(db_session) -> None:
    """到达新场景时，模型自行补写的未解锁地穴和人影不得展示。"""

    room_id = "00000000-0000-0000-0000-000000000208"
    actor_id = "actor-208"
    db_session.add(Room(id=room_id, room_code="P1BS", room_name="Phase 1B 防剧透", max_players=1))
    await db_session.commit()
    await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id=actor_id,
        display_name="调查员",
    )
    set_agents_for_testing(
        ScriptedIntentInterpreter(
            [
                IntentResult(
                    kind="proposal",
                    summary="前往墓园",
                    source_revision=0,
                    steps=[IntentStep(action="move_actor", target_id="cemetery")],
                )
            ]
        ),
        _LeakingNarrator(),
    )
    try:
        result = await submit_free_text(
            db_session,
            room_id=room_id,
            payload=TurnInputBody(
                client_request_id="turn-p1b-spoiler",
                actor_id=actor_id,
                expected_revision=0,
                input="前往墓园",
            ),
        )
    finally:
        set_agents_for_testing(None)

    assert result.status == "completed"
    assert result.narration == "你从阿诺兹堡来到了公共墓地。"
    assert result.command_result is not None
    assert all(
        term not in "".join(result.command_result.narration_facts) for term in ("地穴", "人影")
    )


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


async def test_roll_narration_is_saved_in_idempotent_receipt(db_session, monkeypatch) -> None:
    """投骰后的 AI 续写必须与权威结果一起重放，不重复调用模型。"""

    monkeypatch.setattr("app.service.gm_runtime.secrets.randbelow", lambda _limit: 0)
    room_id = "00000000-0000-0000-0000-000000000210"
    actor_id = "actor-210"
    db_session.add(Room(id=room_id, room_code="P1B5", room_name="Phase 1B 骰后续写", max_players=1))
    await db_session.commit()
    await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id=actor_id,
        display_name="调查员",
    )
    started = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="start-check-210",
            expected_revision=0,
            actor_id=actor_id,
            command={
                "kind": "start_check",
                "check_id": "check-210",
                "skill_id": "spot-hidden",
                "goal": "观察当前场景",
            },
        ),
    )
    narrator = _RetryNarrator()
    set_agents_for_testing(ScriptedIntentInterpreter([]), narrator)
    envelope = CommandEnvelope(
        client_request_id="roll-check-210",
        expected_revision=started.revision,
        actor_id=actor_id,
        command={"kind": "roll_check", "check_id": "check-210"},
    )
    try:
        resolved = await submit_command(
            db_session,
            room_id=room_id,
            envelope=envelope,
            narrate=True,
        )
        replayed = await submit_command(
            db_session,
            room_id=room_id,
            envelope=envelope,
            narrate=True,
        )
    finally:
        set_agents_for_testing(None)

    assert resolved.narration == "你从痕迹中得到了新的判断。"
    assert replayed.narration == resolved.narration
    assert narrator.calls == 2


async def test_session_actor_uses_completed_character_values(db_session) -> None:
    """GM Actor 必须使用玩家已完成角色卡，不得退回模组默认技能。"""

    room_id = "00000000-0000-0000-0000-000000000211"
    actor_id = "00000000-0000-0000-0000-000000000212"
    room = Room(id=room_id, room_code="P1B6", room_name="Phase 1B 角色卡", max_players=1)
    player = Player(id=actor_id, room_id=room_id, nickname="史蒂夫")
    character = Character(
        room_id=room_id,
        player_id=actor_id,
        status="complete",
        name="史蒂夫",
        derived_stats={"HP": 11, "SAN": 30},
        attributes={"LUCK": 45},
        skills={"charm": 63, "spot-hidden": 53},
        equipment=["手电筒"],
    )
    db_session.add_all([room, player, character])
    await db_session.commit()

    created = await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id=actor_id,
        display_name="旧昵称",
    )
    actor = await db_session.get(RuntimeActor, actor_id)

    assert created.projection.hp == 11
    assert created.projection.san == 30
    assert actor is not None and actor.display_name == "史蒂夫"
    assert actor.state_json["skills"]["charm"] == 63
    assert actor.state_json["luck"] == 45
    assert actor.state_json["items"] == ["手电筒"]

    luck_check = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="luck-check-212",
            expected_revision=0,
            actor_id=actor_id,
            command={
                "kind": "start_check",
                "check_id": "luck-check-212",
                "skill_id": "luck",
                "goal": "测试角色幸运值",
            },
        ),
    )
    assert luck_check.check is not None and luck_check.check.target_value == 45
    assert "幸运（判断不受调查员能力控制的偶然机会）" in luck_check.narration_facts[0]


async def test_module_checkpoint_forces_dice_before_applying_clues(db_session, monkeypatch) -> None:
    """模组检定目标必须进入骰子流程，且技能与结果均由冻结 checkpoint 决定。"""

    monkeypatch.setattr("app.service.gm_runtime.secrets.randbelow", lambda _limit: 0)
    room_id = "00000000-0000-0000-0000-000000000209"
    actor_id = "actor-209"
    db_session.add(Room(id=room_id, room_code="P1B4", room_name="Phase 1B 模组检定", max_players=1))
    await db_session.commit()
    await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id=actor_id,
        display_name="调查员",
    )
    moved = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="move-cemetery-209",
            expected_revision=0,
            actor_id=actor_id,
            command={"kind": "move_actor", "target_id": "cemetery"},
        ),
    )
    snapshot = await build_context_snapshot(db_session, room_id=room_id, actor_id=actor_id)
    candidate_ids = {candidate.target_id for candidate in snapshot.action_candidates}
    assert "track_grave" not in candidate_ids
    assert not candidate_ids & {
        "call_douglas",
        "attack_douglas",
        "open_crypt",
        "fight_ghouls",
        "follow_underground",
    }

    # 先通过守墓人 checkpoint 获得墓碑线索，后续检查才允许进入候选。
    prerequisite = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="start-gravekeeper-209",
            expected_revision=moved.revision,
            actor_id=actor_id,
            command={
                "kind": "start_check",
                "check_id": "gravekeeper-209",
                "skill_id": "charm",
                "goal": "gravekeeper",
            },
        ),
    )
    prerequisite = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="roll-gravekeeper-209",
            expected_revision=prerequisite.revision,
            actor_id=actor_id,
            command={"kind": "roll_check", "check_id": "gravekeeper-209"},
        ),
    )
    assert "favorite_grave" in prerequisite.projection.clues, prerequisite.projection.clues
    snapshot = await build_context_snapshot(db_session, room_id=room_id, actor_id=actor_id)
    track_candidate = next(
        (
            candidate
            for candidate in snapshot.action_candidates
            if candidate.target_id == "track_grave"
        ),
        None,
    )
    assert track_candidate is not None, snapshot.action_candidates
    assert track_candidate.action == "start_check"
    assert track_candidate.skill_id == "track"
    assert not any(candidate.target_id == "headstone" for candidate in snapshot.action_candidates)

    # 即使模型伪造技能和自由目标，Validator 也必须重新绑定模组 checkpoint。
    set_agents_for_testing(
        ScriptedIntentInterpreter(
            [
                IntentResult(
                    kind="proposal",
                    summary="检查墓碑",
                    source_revision=prerequisite.revision,
                    steps=[
                        IntentStep(
                            action="start_check",
                            target_id="track_grave",
                            skill_id="spot-hidden",
                            goal="直接判定成功",
                        )
                    ],
                )
            ]
        ),
        _ScriptedNarrator(),
    )
    try:
        started = await submit_free_text(
            db_session,
            room_id=room_id,
            payload=TurnInputBody(
                client_request_id="inspect-headstone-209",
                actor_id=actor_id,
                expected_revision=prerequisite.revision,
                input="检查墓碑",
            ),
        )
    finally:
        set_agents_for_testing(None)

    assert started.command_result is not None
    assert started.command_result.check is not None
    assert started.command_result.check.status == "awaiting_roll"
    assert started.command_result.check.skill_id == "track"
    assert started.command_result.check.skill_label == "追踪"
    assert "tunnel_hint" not in started.command_result.projection.clues
    assert started.command_result.events[0].event_type == "check_started"

    resolved = await submit_command(
        db_session,
        room_id=room_id,
        envelope=CommandEnvelope(
            client_request_id="roll-headstone-209",
            expected_revision=started.revision,
            actor_id=actor_id,
            command={"kind": "roll_check", "check_id": "check-inspect-headstone-209"},
        ),
    )
    assert resolved.check is not None and resolved.check.success is True
    assert "tunnel_hint" in resolved.projection.clues
