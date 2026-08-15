"""可靠回合 REST 查询、玩家安全待决策投影与 v2 恢复服务。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.turn_runtime import TERMINAL_TURN_STATUSES, TurnRecord
from app.dto.turn import TurnErrorRead, TurnRead
from app.models.turn import TurnRecordModel


class TurnReadNotFoundError(LookupError):
    """指定房间中不存在该回合。"""


class TurnReadAuthorizationError(PermissionError):
    """回合结果不属于当前玩家。"""


class TurnResumeUnavailableError(RuntimeError):
    """可靠回合 Coordinator 尚未启用，当前不能推进恢复。"""


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
    return _safe_turn_read(record)


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
    return [_safe_turn_read(record) for record in result.scalars()]


async def resume_turn(
    db: AsyncSession,
    *,
    room_id: str,
    turn_id: str,
    player_id: str,
    runtime_mode: str,
) -> TurnRead:
    """校验 owner 后，通过 v2 Coordinator 按唯一恢复点推进。"""

    await get_turn(db, room_id=room_id, turn_id=turn_id, player_id=player_id)
    if runtime_mode != "v2":
        raise TurnResumeUnavailableError(f"可靠回合恢复协调器尚未启用（当前模式：{runtime_mode}）")
    # 延迟导入避免 REST 查询服务与生产组合根在模块加载时形成循环依赖。
    from app.service.reliable_turn_runtime import resume_turn_by_id

    turn = await resume_turn_by_id(turn_id)
    if turn.room_id != room_id or turn.player_id != player_id:
        raise TurnReadAuthorizationError("不能恢复其他玩家的回合")
    return _safe_turn_projection(turn)


def _safe_turn_read(record: TurnRecordModel) -> TurnRead:
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
    return _safe_turn_projection(turn)


def _safe_turn_projection(turn: TurnRecord) -> TurnRead:
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
        pending_decision=turn.pending_decision,
        narration=result.narration if result else None,
        message_id=result.message_id if result else None,
        player_view=result.player_view if result else None,
        view_revision=result.view_revision if result else None,
        created_at=turn.created_at,
        updated_at=turn.updated_at,
        completed_at=turn.completed_at,
    )
