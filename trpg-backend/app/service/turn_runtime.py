"""可靠回合 REST 查询、玩家安全待决策投影与 v2 恢复服务。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.turn_runtime import TERMINAL_TURN_STATUSES, TurnRecord
from app.dto.turn import TurnErrorRead, TurnRead
from app.models.engine import AdjudicationCommandExecution
from app.models.turn import TurnRecordModel


class TurnReadNotFoundError(LookupError):
    """指定房间中不存在该回合。"""


class TurnReadAuthorizationError(PermissionError):
    """回合结果不属于当前玩家。"""


async def get_turn(
    db: AsyncSession,
    *,
    room_id: str,
    turn_id: str,
    player_id: str,
) -> TurnRead:
    """读取并校验 owner，随后返回不含私密请求信息的投影。"""

    record = await db.get(TurnRecordModel, turn_id)
    if record is None or record.room_id != room_id:
        raise TurnReadNotFoundError("回合不存在")
    if record.player_id != player_id:
        raise TurnReadAuthorizationError("不能查看其他玩家的回合")
    return await _safe_turn_read(db, record)


async def list_turns(
    db: AsyncSession,
    *,
    room_id: str,
    player_id: str,
    client_action_id: str | None,
    active_only: bool,
    limit: int,
) -> list[TurnRead]:
    """只列出当前玩家自己的回合，并支持幂等键和活动状态筛选。"""

    statement = select(TurnRecordModel).where(
        TurnRecordModel.room_id == room_id,
        TurnRecordModel.player_id == player_id,
    )
    if client_action_id is not None:
        statement = statement.where(TurnRecordModel.client_action_id == client_action_id)
    if active_only:
        statement = statement.where(
            TurnRecordModel.status.not_in([status.value for status in TERMINAL_TURN_STATUSES])
        )
    statement = statement.order_by(
        TurnRecordModel.created_at.desc(), TurnRecordModel.turn_id.desc()
    ).limit(limit)
    result = await db.execute(statement)
    return [await _safe_turn_read(db, record) for record in result.scalars()]


async def resume_turn(
    db: AsyncSession,
    *,
    room_id: str,
    turn_id: str,
    player_id: str,
) -> TurnRead:
    """校验 owner 后，通过唯一生产 Coordinator 按持久恢复点推进。"""

    await get_turn(db, room_id=room_id, turn_id=turn_id, player_id=player_id)
    # 延迟导入避免 REST 查询服务与生产组合根在模块加载时形成循环依赖。
    from app.service.reliable_turn_runtime import resume_turn_by_id

    turn = await resume_turn_by_id(turn_id)
    if turn.room_id != room_id or turn.player_id != player_id:
        raise TurnReadAuthorizationError("不能恢复其他玩家的回合")
    revision = await _pending_command_revision(
        db,
        room_id=turn.room_id,
        pending_decision=turn.pending_decision,
    )
    return _safe_turn_projection(turn, pending_source_revision=revision)


async def _safe_turn_read(db: AsyncSession, record: TurnRecordModel) -> TurnRead:
    """复用核心模型校验数据库记录，同时只投影玩家可见字段。"""

    turn = TurnRecord.model_validate(
        {
            "turn_id": record.turn_id,
            "room_id": record.room_id,
            "client_action_id": record.client_action_id,
            "input_fingerprint": record.input_fingerprint,
            "player_id": record.player_id,
            "actor_id": record.actor_id,
            "request": record.request_json,
            "status": record.status,
            "phase_version": record.phase_version,
            "resume_point": record.resume_point,
            "waiting_reason": record.waiting_reason,
            "commit_state": record.commit_state,
            "recovery_action": record.recovery_action,
            "pending_decision": record.pending_decision_json,
            "last_error": record.error_json,
            "result": record.result_json,
            "lease_owner": record.lease_owner,
            "lease_expires_at": record.lease_expires_at,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "completed_at": record.completed_at,
        }
    )
    revision = await _pending_command_revision(
        db,
        room_id=turn.room_id,
        pending_decision=turn.pending_decision,
    )
    return _safe_turn_projection(turn, pending_source_revision=revision)


def _safe_turn_projection(
    turn: TurnRecord,
    *,
    pending_source_revision: str | None = None,
) -> TurnRead:
    """从经过核心契约校验的 TurnRecord 生成玩家安全 DTO。"""

    error = None
    if turn.last_error is not None:
        error = TurnErrorRead(
            code=turn.last_error.code,
            stage=turn.last_error.stage,
            retryable=turn.last_error.retryable,
            public_message=turn.last_error.public_message,
            occurred_at=turn.last_error.occurred_at,
        )
    result = turn.result
    pending_decision = turn.pending_decision
    if pending_decision is not None and pending_source_revision is not None:
        # 历史 Turn 可能只保存了内部 decision/check；查询时用同一 action 最新
        # execution 的输出 revision 补齐，不重新执行 Engine 或重新掷骰。
        pending_decision = {**pending_decision, "source_revision": pending_source_revision}
    return TurnRead(
        turn_id=turn.turn_id,
        room_id=turn.room_id,
        client_action_id=turn.client_action_id,
        status=turn.status,
        commit_state=turn.commit_state,
        resume_point=turn.resume_point,
        waiting_reason=turn.waiting_reason,
        recovery_action=turn.recovery_action,
        phase_version=turn.phase_version,
        error=error,
        pending_decision=pending_decision,
        narration=result.narration if result else None,
        message_id=result.message_id if result else None,
        player_view=result.player_view if result else None,
        view_revision=result.view_revision if result else None,
        created_at=turn.created_at,
        updated_at=turn.updated_at,
        completed_at=turn.completed_at,
    )


async def _pending_command_revision(
    db: AsyncSession,
    *,
    room_id: str,
    pending_decision: dict | None,
) -> str | None:
    """从同一动作最新 execution 恢复下一条选择命令必须携带的 revision。"""

    if pending_decision is None:
        return None
    action_request_id = pending_decision.get("action_request_id")
    if not isinstance(action_request_id, str) or not action_request_id:
        return None
    records = await db.scalars(
        select(AdjudicationCommandExecution)
        .where(
            AdjudicationCommandExecution.room_id == room_id,
            AdjudicationCommandExecution.action_request_id == action_request_id,
        )
        .order_by(
            AdjudicationCommandExecution.committed_state_version.desc(),
            AdjudicationCommandExecution.created_at.desc(),
            AdjudicationCommandExecution.request_id.desc(),
        )
    )
    for record in records:
        execution = record.result_json.get("execution")
        if not isinstance(execution, dict):
            continue
        revision = execution.get("view_revision")
        if isinstance(revision, str) and revision:
            return revision
    return None
