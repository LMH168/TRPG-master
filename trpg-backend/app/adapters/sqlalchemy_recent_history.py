"""SQLAlchemy projection of transport history into player-safe model context."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import cast

import structlog
from collaboration_framework.contracts import ActionRequest, PlayerInput, PlayerView, VisibleFact
from collaboration_framework.engine import EngineExecutionResult
from collaboration_framework.host.schemas import (
    ActionPlanRun,
    HistoryVisibility,
    RecentHistoryBudget,
    RecentSafeResult,
    RecentTurn,
    RecentTurnContext,
    VisibleHistoryText,
)
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.engine import (
    ActionExecution,
    ActionPlanRunRecord,
    AdjudicationCommandExecution,
    AgendaStepExecutionRecord,
)
from app.models.event import Event

_CANDIDATE_LIMIT = 24
_UTTERANCE_LIMIT = 800
_NARRATION_LIMIT = 1200
_INTENT_LIMIT = 400
_RESULT_TEXT_LIMIT = 1200
logger = structlog.get_logger()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"
    return text[: limit - 1] + "…"


def _turn_chars(turn: RecentTurn) -> int:
    return sum(
        len(text)
        for text in (
            turn.player_utterance.text,
            turn.accepted_intent_summary or "",
            turn.published_narration.text if turn.published_narration else "",
            *(
                fact.text
                for fact in (
                    turn.player_safe_result.visible_facts if turn.player_safe_result else ()
                )
            ),
        )
    )


def _bounded_facts(facts: Iterable[VisibleFact]) -> tuple[VisibleFact, ...]:
    remaining = _RESULT_TEXT_LIMIT
    bounded: list[VisibleFact] = []
    for fact in facts:
        if remaining <= 0:
            break
        text = _truncate(fact.text, remaining)
        bounded.append(fact.model_copy(update={"text": text}))
        remaining -= len(text)
    return tuple(bounded)


def _select_turns(
    turns_newest_first: list[RecentTurn],
    *,
    scene_id: str,
    budget: RecentHistoryBudget,
) -> tuple[RecentTurn, ...]:
    if not turns_newest_first:
        return ()

    adjacent = turns_newest_first[0]
    selected: list[RecentTurn] = [adjacent]
    same_scene = [
        turn
        for turn in turns_newest_first[1:]
        if turn.scene_id is not None and turn.scene_id == scene_id
    ]
    # RecentTurnContext 只负责相邻指代与当前场景节奏；跨场景召回已经由
    # MemoryContext 承担，继续拼接其他场景会重复占用 token 并模糊 NPC 认知边界。
    for turn in same_scene:
        if len(selected) >= budget.max_turns:
            break
        selected.append(turn)

    while sum(_turn_chars(turn) for turn in selected) > budget.max_chars:
        removable = max(
            (turn for turn in selected if turn is not adjacent),
            key=turns_newest_first.index,
            default=None,
        )
        if removable is None:
            break
        selected.remove(removable)

    # The adjacent turn must remain. If it alone exceeds the global budget, retain
    # its utterance and narration while first dropping lower-priority semantic/result
    # text and then shrinking presentation tails deterministically.
    total = sum(_turn_chars(turn) for turn in selected)
    if total > budget.max_chars:
        adjacent = adjacent.model_copy(
            update={
                "accepted_intent_summary": None,
                "player_safe_result": (
                    adjacent.player_safe_result.model_copy(update={"visible_facts": ()})
                    if adjacent.player_safe_result is not None
                    else None
                ),
            }
        )
        selected[0] = adjacent
        total = sum(_turn_chars(turn) for turn in selected)
    if total > budget.max_chars:
        overflow = total - budget.max_chars
        narration = adjacent.published_narration
        if narration is not None and len(narration.text) > 1:
            new_length = max(1, len(narration.text) - overflow)
            adjacent = adjacent.model_copy(
                update={
                    "published_narration": narration.model_copy(
                        update={"text": _truncate(narration.text, new_length)}
                    )
                }
            )
            selected[0] = adjacent
        total = sum(_turn_chars(turn) for turn in selected)
        if total > budget.max_chars and len(adjacent.player_utterance.text) > 1:
            new_length = max(
                1,
                len(adjacent.player_utterance.text) - (total - budget.max_chars),
            )
            adjacent = adjacent.model_copy(
                update={
                    "player_utterance": adjacent.player_utterance.model_copy(
                        update={
                            "text": _truncate(
                                adjacent.player_utterance.text,
                                new_length,
                            )
                        }
                    )
                }
            )
            selected[0] = adjacent

    return tuple(reversed(selected))


class SqlAlchemyRecentHistorySource:
    """Read only the existing transport and execution tables."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def read(
        self,
        *,
        player_input: PlayerInput,
        player_view: PlayerView,
        exclude_correlation_id: str,
        budget: RecentHistoryBudget,
    ) -> RecentTurnContext:
        async with self._session_factory() as session:
            cutoff = await session.scalar(
                select(Event).where(
                    Event.room_id == player_input.room_id,
                    Event.event_type == "action.broadcast",
                    Event.correlation_id == exclude_correlation_id,
                )
            )
            conditions = [
                Event.room_id == player_input.room_id,
                Event.event_type == "action.broadcast",
                Event.correlation_id != exclude_correlation_id,
                or_(
                    Event.visibility == "public",
                    and_(
                        Event.visibility == "player_scoped",
                        Event.player_id == player_input.player_id,
                    ),
                ),
            ]
            if cutoff is not None:
                conditions.append(
                    or_(
                        Event.created_at < cutoff.created_at,
                        and_(
                            Event.created_at == cutoff.created_at,
                            Event.id < cutoff.id,
                        ),
                    )
                )
            action_events = list(
                (
                    await session.scalars(
                        select(Event)
                        .where(*conditions)
                        .order_by(Event.created_at.desc(), Event.id.desc())
                        .limit(_CANDIDATE_LIMIT)
                    )
                ).all()
            )
            correlations = [
                event.correlation_id for event in action_events if event.correlation_id is not None
            ]
            related_conditions = [
                Event.room_id == player_input.room_id,
                Event.correlation_id.in_(correlations),
                Event.event_type.in_(["narration.push", "check.result"]),
                or_(
                    Event.visibility == "public",
                    and_(
                        Event.visibility == "player_scoped",
                        Event.player_id == player_input.player_id,
                    ),
                ),
            ]
            execution_conditions = [
                ActionExecution.room_id == player_input.room_id,
                ActionExecution.request_id.in_(correlations),
            ]
            if cutoff is not None:
                related_conditions.append(
                    or_(
                        Event.created_at < cutoff.created_at,
                        and_(
                            Event.created_at == cutoff.created_at,
                            Event.id < cutoff.id,
                        ),
                    )
                )
                execution_conditions.append(ActionExecution.created_at < cutoff.created_at)
            related_events = (
                list((await session.scalars(select(Event).where(*related_conditions))).all())
                if correlations
                else []
            )
            executions = (
                list(
                    (
                        await session.scalars(select(ActionExecution).where(*execution_conditions))
                    ).all()
                )
                if correlations
                else []
            )
            own_correlations = [
                event.correlation_id
                for event in action_events
                if event.player_id == player_input.player_id and event.correlation_id is not None
            ]
            adjudication_records = (
                list(
                    (
                        await session.scalars(
                            select(AdjudicationCommandExecution)
                            .where(
                                AdjudicationCommandExecution.room_id == player_input.room_id,
                                AdjudicationCommandExecution.action_request_id.in_(
                                    own_correlations
                                ),
                            )
                            .order_by(
                                AdjudicationCommandExecution.committed_state_version,
                                AdjudicationCommandExecution.created_at,
                                AdjudicationCommandExecution.request_id,
                            )
                        )
                    ).all()
                )
                if own_correlations
                else []
            )
            plan_records = (
                list(
                    (
                        await session.scalars(
                            select(ActionPlanRunRecord).where(
                                ActionPlanRunRecord.room_id == player_input.room_id,
                                ActionPlanRunRecord.parent_action_id.in_(correlations),
                            )
                        )
                    ).all()
                )
                if correlations
                else []
            )
            # Agenda execution 内含玩家回合的私有语义摘要，只查询当前玩家自己的
            # Turn；公开旁观历史仍由 transport Event 提供，不能借此读取他人证明。
            turn_ids = [
                event.turn_id
                for event in action_events
                if event.turn_id is not None and event.player_id == player_input.player_id
            ]
            agenda_execution_records = (
                list(
                    (
                        await session.scalars(
                            select(AgendaStepExecutionRecord)
                            .where(AgendaStepExecutionRecord.execution_turn_id.in_(turn_ids))
                            .order_by(
                                AgendaStepExecutionRecord.committed_state_version,
                                AgendaStepExecutionRecord.execution_id,
                            )
                        )
                    ).all()
                )
                if turn_ids
                else []
            )

        event_by_key = {(event.correlation_id, event.event_type): event for event in related_events}
        execution_by_correlation = {execution.request_id: execution for execution in executions}
        adjudication_by_action: dict[str, list[AdjudicationCommandExecution]] = {}
        for record in adjudication_records:
            if record.action_request_id is not None:
                adjudication_by_action.setdefault(record.action_request_id, []).append(record)
        plan_by_correlation = {record.parent_action_id: record for record in plan_records}
        agenda_by_turn: dict[str, list[AgendaStepExecutionRecord]] = {}
        for record in agenda_execution_records:
            agenda_by_turn.setdefault(record.execution_turn_id, []).append(record)
        safe_participant_ids = {
            player_view.actor_id,
            *(item.id for item in player_view.scene.visible_entities),
            *(item.id for item in player_view.scene.visible_actors),
        }
        projected: list[RecentTurn] = []
        truncated_field_count = 0
        for action_event in action_events:
            correlation_id = action_event.correlation_id
            if correlation_id is None or action_event.player_id is None:
                continue
            own_turn = action_event.player_id == player_input.player_id
            execution = execution_by_correlation.get(correlation_id)
            request: ActionRequest | None = None
            engine_result: EngineExecutionResult | None = None
            if execution is not None and own_turn:
                request = ActionRequest.model_validate(execution.request_json)
                engine_result = EngineExecutionResult.model_validate(execution.result_json)
            legacy_actor_id = (
                execution.request_json.get("actor_id")
                if execution is not None and isinstance(execution.request_json.get("actor_id"), str)
                else None
            )
            legacy_source_revision = (
                execution.request_json.get("source_view_revision")
                if execution is not None
                and isinstance(
                    execution.request_json.get("source_view_revision"),
                    str,
                )
                else None
            )
            source_actor_id = action_event.actor_id or legacy_actor_id
            if source_actor_id is None:
                continue
            utterance = action_event.payload.get("utterance")
            if not isinstance(utterance, str) or not utterance.strip():
                continue

            narration_event = event_by_key.get((correlation_id, "narration.push"))
            narration: VisibleHistoryText | None = None
            if narration_event is not None and (
                narration_event.visibility == "public"
                or (
                    own_turn
                    and narration_event.visibility == "player_scoped"
                    and narration_event.player_id == player_input.player_id
                )
            ):
                narration_text = narration_event.payload.get("text")
                if isinstance(narration_text, str) and narration_text.strip():
                    truncated_field_count += int(len(narration_text) > _NARRATION_LIMIT)
                    narration = VisibleHistoryText(
                        text=_truncate(narration_text, _NARRATION_LIMIT),
                        visibility=narration_event.visibility,
                    )

            accepted_summary = None
            safe_result = None
            committed_revision: str | None = None
            participants: list[str] = []
            if source_actor_id in safe_participant_ids or own_turn:
                participants.append(source_actor_id)
            if own_turn and request is not None and engine_result is not None:
                truncated_field_count += int(len(request.intent.summary) > _INTENT_LIMIT)
                accepted_summary = _truncate(request.intent.summary, _INTENT_LIMIT)
                action_result = engine_result.action_result
                safe_result = RecentSafeResult(
                    resolution=action_result.resolution,
                    outcome=action_result.outcome,
                    check_result=action_result.check_result,
                    visible_facts=_bounded_facts(action_result.visible_facts),
                )
                target = request.intent.target
                target_id = getattr(target, "id", None)
                if target_id in safe_participant_ids and target_id not in participants:
                    participants.append(target_id)
            # 当前生产 writer 保存 Proposal 信封和最终 execution。只从当前玩家
            # 的首次请求恢复可信目标与焦点，后续检定命令不能覆盖原动作语义。
            command_records = adjudication_by_action.get(correlation_id, [])
            if own_turn and command_records:
                initial = next(
                    (
                        record
                        for record in command_records
                        if isinstance(record.request_json.get("proposal"), dict)
                    ),
                    None,
                )
                latest = command_records[-1]
                if initial is not None:
                    proposal = initial.request_json.get("proposal")
                    assert isinstance(proposal, dict)
                    requested_goal = initial.request_json.get("requested_goal")
                    semantic_goal = proposal.get("semantic_goal")
                    summary = (
                        requested_goal
                        if isinstance(requested_goal, str)
                        else semantic_goal
                        if isinstance(semantic_goal, str)
                        else None
                    )
                    if summary:
                        truncated_field_count += int(len(summary) > _INTENT_LIMIT)
                        accepted_summary = _truncate(summary, _INTENT_LIMIT)
                    focus = proposal.get("semantic_focus")
                    focus_id = focus.get("id") if isinstance(focus, dict) else None
                    if (
                        isinstance(focus_id, str)
                        and focus_id in safe_participant_ids
                        and focus_id not in participants
                    ):
                        participants.append(focus_id)
                    source_revision = initial.request_json.get("source_revision")
                    if isinstance(source_revision, str):
                        legacy_source_revision = source_revision
                execution_payload = latest.result_json.get("execution")
                if isinstance(execution_payload, dict):
                    view_revision = execution_payload.get("view_revision")
                    if isinstance(view_revision, str):
                        committed_revision = view_revision
            if own_turn and (record := plan_by_correlation.get(correlation_id)) is not None:
                try:
                    plan_run = ActionPlanRun.from_persistence_json_dict(record.run_json)
                except (TypeError, ValueError):
                    logger.warning(
                        "recent_history_plan_projection_skipped",
                        correlation_id=correlation_id,
                    )
                else:
                    # 只投影当前 PlayerView 仍允许看到的交互实体，不能把历史
                    # Proposal 中的隐藏或已离场对象重新暴露给模型。
                    for step in reversed(plan_run.steps):
                        proposal = step.proposal
                        if proposal is None or proposal.semantic_focus.kind != "entity":
                            continue
                        focus_id = proposal.semantic_focus.id
                        if focus_id in safe_participant_ids and focus_id not in participants:
                            participants.append(focus_id)
                        break
            # Proposal 只能记录初始目标；规则执行期间新出现的 NPC 必须从已提交
            # Agenda 玩家安全证据恢复，不能依赖 Narrator 文本猜测参与者。
            if own_turn and action_event.turn_id is not None:
                for agenda_record in agenda_by_turn.get(action_event.turn_id, []):
                    raw_evidence = agenda_record.result_json.get("narration_evidence", [])
                    if not isinstance(raw_evidence, list):
                        continue
                    for item in raw_evidence:
                        if not isinstance(item, dict) or item.get("kind") != "npc_opportunity":
                            continue
                        subject_id = item.get("subject_id")
                        if (
                            isinstance(subject_id, str)
                            and subject_id in safe_participant_ids
                            and subject_id not in participants
                        ):
                            participants.append(subject_id)

            evidence = [f"transport_event:{action_event.id}"]
            if execution is not None and own_turn:
                evidence.append(f"action_execution:{correlation_id}")
            if own_turn:
                evidence.extend(
                    f"adjudication_execution:{record.request_id}" for record in command_records
                )
            if narration_event is not None and narration is not None:
                evidence.append(f"transport_event:{narration_event.id}")
            if own_turn and action_event.turn_id is not None:
                evidence.extend(
                    f"agenda_execution:{record.execution_id}"
                    for record in agenda_by_turn.get(action_event.turn_id, [])
                )
            check_event = event_by_key.get((correlation_id, "check.result"))
            if (
                check_event is not None
                and own_turn
                and check_event.player_id == player_input.player_id
            ):
                evidence.append(f"transport_event:{check_event.id}")

            projected.append(
                RecentTurn(
                    correlation_id=correlation_id,
                    source_player_id=action_event.player_id,
                    source_actor_id=source_actor_id,
                    scene_id=action_event.scene_id,
                    source_view_revision=(action_event.view_revision or legacy_source_revision),
                    committed_view_revision=(
                        narration_event.view_revision
                        if narration_event is not None
                        else (
                            committed_revision
                            or (
                                engine_result.action_result.view_revision
                                if engine_result is not None
                                else None
                            )
                        )
                    ),
                    participants=tuple(participants),
                    player_utterance=VisibleHistoryText(
                        text=_truncate(utterance, _UTTERANCE_LIMIT),
                        visibility=cast(
                            HistoryVisibility,
                            action_event.visibility,
                        ),
                    ),
                    accepted_intent_summary=accepted_summary,
                    player_safe_result=safe_result,
                    published_narration=narration,
                    evidence_refs=tuple(evidence),
                )
            )
            truncated_field_count += int(len(utterance) > _UTTERANCE_LIMIT)

        selected_turns = _select_turns(
            projected,
            scene_id=player_view.scene_id,
            budget=budget,
        )
        projected_by_correlation = {turn.correlation_id: turn for turn in projected}
        globally_truncated_count = sum(
            _turn_chars(turn) < _turn_chars(projected_by_correlation[turn.correlation_id])
            for turn in selected_turns
        )
        context = RecentTurnContext(
            room_id=player_input.room_id,
            viewer_player_id=player_input.player_id,
            as_of_revision=player_view.revision,
            turns=selected_turns,
        )
        logger.info(
            "recent_history_projection",
            room_ref=hashlib.sha256(player_input.room_id.encode("utf-8")).hexdigest()[:12],
            correlation_id=exclude_correlation_id,
            candidate_turn_count=len(action_events),
            projected_turn_count=len(projected),
            selected_turn_count=len(selected_turns),
            character_count=sum(_turn_chars(turn) for turn in selected_turns),
            truncated_count=(
                truncated_field_count
                + max(0, len(projected) - len(selected_turns))
                + globally_truncated_count
            ),
        )
        return context.validate_for(
            player_input=player_input,
            player_view=player_view,
        )
