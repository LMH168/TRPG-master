"""从可靠 Turn、receipt、权威事件与回放表读取 Memory 投影来源。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast

import structlog
from collaboration_framework.host.schemas import ActionPlanRun
from collaboration_framework.memory import (
    MemoryProjectionEvent,
    MemoryProjectionNarration,
    MemoryProjectionSource,
    MemoryProjectionStep,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.engine import (
    ActionPlanRunRecord,
    AdjudicationCommandExecution,
    GameEvent,
)
from app.models.event import Event
from app.models.memory import MemoryProjectionRunRecord
from app.models.turn import TurnCommitReceiptRecord, TurnRecordModel

logger = structlog.get_logger()
_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class SqlAlchemyMemoryProjectionSource:
    """只读组装投影证据，不参与 Engine 或 Turn 的写事务。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_unregistered_turn_ids(
        self,
        *,
        room_id: str | None = None,
        limit: int = 20,
    ) -> tuple[str, ...]:
        """稳定扫描尚未建立 Projection Run 的终态 Turn。"""

        statement = (
            select(TurnRecordModel.turn_id)
            .outerjoin(
                MemoryProjectionRunRecord,
                MemoryProjectionRunRecord.turn_id == TurnRecordModel.turn_id,
            )
            .where(
                TurnRecordModel.status.in_(_TERMINAL_STATUSES),
                MemoryProjectionRunRecord.turn_id.is_(None),
            )
            .order_by(TurnRecordModel.completed_at, TurnRecordModel.turn_id)
            .limit(limit)
        )
        if room_id is not None:
            statement = statement.where(TurnRecordModel.room_id == room_id)
        async with self._session_factory() as session:
            return tuple(await session.scalars(statement))

    async def load(self, turn_id: str) -> MemoryProjectionSource | None:
        """读取一个终态 Turn 的全部不可变证据并生成规范化快照。"""

        async with self._session_factory() as session:
            turn = await session.get(TurnRecordModel, turn_id)
            if turn is None or turn.status not in _TERMINAL_STATUSES:
                return None
            receipts = tuple(
                await session.scalars(
                    select(TurnCommitReceiptRecord)
                    .where(TurnCommitReceiptRecord.turn_id == turn_id)
                    .order_by(
                        TurnCommitReceiptRecord.created_at,
                        TurnCommitReceiptRecord.engine_request_id,
                    )
                )
            )
            events = tuple(
                await session.scalars(
                    select(GameEvent)
                    .where(GameEvent.turn_id == turn_id)
                    .order_by(GameEvent.sequence, GameEvent.event_id)
                )
            )
            narration_record = await session.scalar(
                select(Event)
                .where(Event.turn_id == turn_id, Event.event_type == "narration.push")
                .order_by(Event.created_at.desc(), Event.id.desc())
                .limit(1)
            )
            plan_record = await session.scalar(
                select(ActionPlanRunRecord)
                .where(ActionPlanRunRecord.turn_id == turn_id)
                .order_by(ActionPlanRunRecord.updated_at.desc())
                .limit(1)
            )
            action_ids = tuple(dict.fromkeys(item.action_request_id for item in receipts))
            executions = (
                tuple(
                    await session.scalars(
                        select(AdjudicationCommandExecution)
                        .where(
                            AdjudicationCommandExecution.room_id == turn.room_id,
                            AdjudicationCommandExecution.action_request_id.in_(action_ids),
                        )
                        .order_by(
                            AdjudicationCommandExecution.committed_state_version,
                            AdjudicationCommandExecution.created_at,
                            AdjudicationCommandExecution.request_id,
                        )
                    )
                )
                if action_ids
                else ()
            )

        steps = self._steps(
            turn_id=turn_id,
            plan_record=plan_record,
            action_ids=action_ids,
            executions=executions,
        )
        narration = self._narration(narration_record)
        assert turn.completed_at is not None
        request = turn.request_json
        utterance = request.get("utterance")
        if not isinstance(utterance, str) or not utterance.strip():
            logger.warning("memory_projection_turn_skipped", turn_id=turn_id, reason="utterance")
            return None
        return MemoryProjectionSource(
            turn_id=turn.turn_id,
            room_id=turn.room_id,
            player_id=turn.player_id,
            actor_id=turn.actor_id,
            utterance=utterance,
            turn_status=turn.status,
            commit_state=cast(
                Literal["not_committed", "partially_committed", "committed"],
                turn.commit_state,
            ),
            receipt_ids=tuple(item.engine_request_id for item in receipts),
            steps=steps,
            events=tuple(
                MemoryProjectionEvent(
                    event_id=item.event_id,
                    sequence=item.sequence,
                    event_type=item.type,
                    actor_id=item.actor_id,
                    visibility=cast(Literal["public", "private", "hidden"], item.visibility),
                    payload=item.payload,
                    created_at=_utc(item.created_at),
                )
                for item in events
            ),
            narration=narration,
            created_at=_utc(turn.created_at),
            completed_at=_utc(turn.completed_at),
        )

    @classmethod
    def _steps(
        cls,
        *,
        turn_id: str,
        plan_record: ActionPlanRunRecord | None,
        action_ids: tuple[str, ...],
        executions: tuple[AdjudicationCommandExecution, ...],
    ) -> tuple[MemoryProjectionStep, ...]:
        """优先读取版本化 ActionPlan，再用 execution 补齐单动作。"""

        projected: dict[str, MemoryProjectionStep] = {}
        if plan_record is not None:
            try:
                run = ActionPlanRun.from_persistence_json_dict(plan_record.run_json)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "memory_projection_plan_skipped",
                    turn_id=turn_id,
                    error_type=type(exc).__name__,
                )
            else:
                for step in run.steps:
                    execution = step.adjudication_execution
                    if step.status not in {"completed", "stopped"}:
                        continue
                    proposal = step.proposal
                    focus = proposal.semantic_focus if proposal is not None else None
                    focus_kind = focus.kind if focus is not None else "none"
                    if focus_kind not in {
                        "actor",
                        "entity",
                        "location",
                        "information",
                    }:
                        focus_kind = "none"
                        focus = None
                    safe_focus_kind = cast(
                        Literal["actor", "entity", "location", "information", "none"],
                        focus_kind,
                    )
                    outcome = execution.outcome if execution is not None else "legacy_unknown"
                    if outcome == "pending":
                        outcome = "legacy_unknown"
                    projected[step.step_request_id] = MemoryProjectionStep(
                        source_id=step.step_request_id,
                        semantic_goal=step.step.semantic_goal,
                        status="completed" if step.status == "completed" else "stopped",
                        outcome=outcome,
                        goal_outcome=(
                            execution.goal_outcome
                            if execution is not None and execution.goal_outcome != "pending"
                            else "legacy_unknown"
                        ),
                        has_receipt=step.step_request_id in action_ids,
                        target_interaction=(
                            proposal.target_interaction if proposal is not None else None
                        ),
                        focus_kind=safe_focus_kind,
                        focus_id=(focus.id if focus is not None else None),
                    )

        by_action: dict[str, list[AdjudicationCommandExecution]] = {}
        for record in executions:
            if record.action_request_id is not None:
                by_action.setdefault(record.action_request_id, []).append(record)
        for action_id in action_ids:
            if action_id in projected:
                continue
            records = by_action.get(action_id, [])
            if not records:
                continue
            initial = next(
                (
                    record
                    for record in records
                    if isinstance(record.request_json.get("proposal"), dict)
                ),
                records[0],
            )
            latest = records[-1]
            proposal = initial.request_json.get("proposal")
            proposal = proposal if isinstance(proposal, dict) else {}
            execution_payload = latest.result_json.get("execution")
            execution_payload = execution_payload if isinstance(execution_payload, dict) else {}
            semantic_goal = initial.request_json.get("requested_goal") or proposal.get(
                "semantic_goal"
            )
            if not isinstance(semantic_goal, str) or not semantic_goal.strip():
                continue
            focus = proposal.get("semantic_focus")
            focus = focus if isinstance(focus, dict) else {}
            focus_kind = focus.get("kind")
            focus_id = focus.get("id")
            if focus_kind not in {"actor", "entity", "location", "information"}:
                focus_kind = "none"
                focus_id = None
            outcome = execution_payload.get("outcome")
            if outcome not in {"success", "failure", "cancelled"}:
                outcome = "legacy_unknown"
            goal_outcome = execution_payload.get("goal_outcome")
            if goal_outcome not in {
                "achieved",
                "partially_achieved",
                "not_achieved",
                "cancelled",
                "legacy_unknown",
            }:
                goal_outcome = "legacy_unknown"
            projected[action_id] = MemoryProjectionStep(
                source_id=action_id,
                semantic_goal=semantic_goal,
                status="cancelled" if outcome == "cancelled" else "completed",
                outcome=outcome,
                goal_outcome=goal_outcome,
                has_receipt=True,
                target_interaction=(
                    proposal.get("target_interaction")
                    if isinstance(proposal.get("target_interaction"), str)
                    else None
                ),
                focus_kind=focus_kind,
                focus_id=focus_id if isinstance(focus_id, str) else None,
            )
        return tuple(projected.values())

    @staticmethod
    def _narration(record: Event | None) -> MemoryProjectionNarration | None:
        if record is None:
            return None
        text = record.payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        viewer = record.player_id if record.visibility == "player_scoped" else None
        return MemoryProjectionNarration(
            source_id=record.id,
            text=text[:4000],
            visibility=cast(Literal["public", "player_scoped"], record.visibility),
            viewer_player_id=viewer,
            scene_id=record.scene_id,
            created_at=_utc(record.created_at),
        )
