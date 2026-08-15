"""基于 SQLAlchemy 的可靠回合、提交回执与叙事 Outbox Store。

所有写方法都在短事务中完成；房间占用、CAS 和唯一键由数据库约束兜底，避免
多 worker 并发时依赖进程内锁。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.turn_runtime import (
    TERMINAL_TURN_STATUSES,
    NarrationOutboxMessage,
    TurnCommitReceipt,
    TurnConflictError,
    TurnContractError,
    TurnFailureSnapshot,
    TurnInputSnapshot,
    TurnNotFoundError,
    TurnOutboxStatus,
    TurnRecord,
    TurnReplayEvent,
    TurnResultSnapshot,
    TurnResumePoint,
    TurnStatus,
    new_turn_record,
    validate_turn_cas_update,
)
from app.models.engine import ActionPlanRunRecord, CheckRunRecord, PendingCheckDecisionRecord
from app.models.event import Event
from app.models.turn import (
    NarrationOutboxRecord,
    RoomTurnReservation,
    TurnCommitReceiptRecord,
    TurnRecordModel,
)


class SqlAlchemyTurnStore:
    """可靠回合协议的生产数据库适配器。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_or_get(self, proposed: TurnRecord) -> tuple[TurnRecord, bool]:
        """原子创建回合与房间占用；并发冲突后按幂等键重新判定。"""

        try:
            async with self._session_factory() as session, session.begin():
                existing = await self._get_by_client_action(session, proposed)
                if existing is not None:
                    self._require_same_idempotent_request(existing, proposed)
                    return existing, False
                reservation = await session.get(RoomTurnReservation, proposed.room_id)
                if reservation is not None:
                    raise TurnConflictError("TURN_IN_PROGRESS", "当前房间已有未完成回合")
                if proposed.is_terminal:
                    raise TurnContractError("新建 TurnRecord 必须处于非终态")
                session.add(self._record_from_turn(proposed))
                await session.flush()
                session.add(
                    RoomTurnReservation(
                        room_id=proposed.room_id,
                        turn_id=proposed.turn_id,
                        created_at=proposed.created_at,
                        updated_at=proposed.updated_at,
                    )
                )
                await session.flush()
                return proposed.model_copy(deep=True), True
        except TurnConflictError:
            raise
        except IntegrityError as exc:
            # 唯一键竞争可能是同请求重试，也可能是另一个活动回合；回滚后重查，
            # 不能把两类冲突都伪装成幂等成功。
            existing = await self.get_by_client_action(
                proposed.room_id,
                proposed.client_action_id,
            )
            if existing is not None:
                self._require_same_idempotent_request(existing, proposed)
                return existing, False
            raise TurnConflictError("TURN_IN_PROGRESS", "当前房间已有未完成回合") from exc

    async def adopt_legacy_inflight_turns(self, *, limit: int = 20) -> tuple[TurnRecord, ...]:
        """把信息完整的旧非终态计划或检定原子绑定到新的 Turn。

        已完成历史不会进入扫描；缺少原始计划输入或玩家安全检定摘要的记录也保持
        原样，避免为不完整历史伪造回合身份。收养与 ``turn_id`` 回写位于同一事务，
        进程退出后不会留下“有 Turn、旧计划却未归属”的半迁移状态。
        """

        if limit < 1:
            raise TurnContractError("limit 必须大于 0")
        adopted: list[TurnRecord] = []
        async with self._session_factory() as session, session.begin():
            plan_records = tuple(
                await session.scalars(
                    select(ActionPlanRunRecord)
                    .where(
                        ActionPlanRunRecord.turn_id.is_(None),
                        ActionPlanRunRecord.status.in_(
                            (
                                "active",
                                "checkpointed",
                                "waiting_for_player",
                                "needs_clarification",
                                "retryable_failure",
                                "awaiting_narration",
                            )
                        ),
                    )
                    .order_by(ActionPlanRunRecord.updated_at, ActionPlanRunRecord.plan_id)
                    .limit(limit)
                )
            )
            for record in plan_records:
                payload = deepcopy(record.run_json)
                utterance = payload.get("parent_utterance")
                if not isinstance(utterance, str) or not utterance.strip():
                    continue
                turn = await self._adopt_snapshot(
                    session,
                    TurnInputSnapshot(
                        room_id=record.room_id,
                        player_id=record.player_id,
                        actor_id=record.actor_id,
                        client_action_id=record.parent_action_id,
                        utterance=utterance,
                    ),
                    created_at=record.created_at,
                )
                if turn is None:
                    continue
                payload["turn_id"] = turn.turn_id
                record.turn_id = turn.turn_id
                record.run_json = payload
                adopted.append(turn)

            remaining = limit - len(adopted)
            if remaining > 0:
                # 旧单动作没有 ActionPlanRun；只有仍等待选择且保存了玩家安全摘要的
                # 记录才具备完整恢复信息。已 resolved 的历史不会被回填。
                decisions = tuple(
                    await session.scalars(
                        select(PendingCheckDecisionRecord)
                        .where(PendingCheckDecisionRecord.status == "awaiting_skill_choice")
                        .order_by(PendingCheckDecisionRecord.updated_at)
                        .limit(remaining)
                    )
                )
                checks = tuple(
                    await session.scalars(
                        select(CheckRunRecord)
                        .where(CheckRunRecord.status == "awaiting_post_roll_decision")
                        .order_by(CheckRunRecord.updated_at)
                        .limit(remaining)
                    )
                )
                standalone = [
                    (
                        item.room_id,
                        item.player_id,
                        item.actor_id,
                        item.action_request_id,
                        item.decision_json,
                        item.created_at,
                    )
                    for item in decisions
                ]
                standalone.extend(
                    (
                        item.room_id,
                        item.player_id,
                        item.actor_id,
                        item.action_request_id,
                        item.check_json,
                        item.created_at,
                    )
                    for item in checks
                )
                seen_actions: set[tuple[str, str]] = set()
                for room_id, player_id, actor_id, action_id, payload, created_at in standalone:
                    key = (room_id, action_id)
                    if key in seen_actions or len(adopted) >= limit:
                        continue
                    seen_actions.add(key)
                    summary = payload.get("summary")
                    if not isinstance(summary, str) or not summary.strip():
                        continue
                    turn = await self._adopt_snapshot(
                        session,
                        TurnInputSnapshot(
                            room_id=room_id,
                            player_id=player_id,
                            actor_id=actor_id,
                            client_action_id=action_id,
                            utterance=summary,
                        ),
                        created_at=created_at,
                    )
                    if turn is not None:
                        adopted.append(turn)
            await session.flush()
        return tuple(adopted)

    async def _adopt_snapshot(
        self,
        session: AsyncSession,
        request: TurnInputSnapshot,
        *,
        created_at: datetime,
    ) -> TurnRecord | None:
        """在调用方事务内创建收养 Turn；已有 Turn 或房间占用时保持幂等跳过。"""

        existing = await session.scalar(
            select(TurnRecordModel).where(
                TurnRecordModel.room_id == request.room_id,
                TurnRecordModel.client_action_id == request.client_action_id,
            )
        )
        if existing is not None:
            return None
        if await session.get(RoomTurnReservation, request.room_id) is not None:
            return None
        turn = new_turn_record(request, now=created_at)
        session.add(self._record_from_turn(turn))
        await session.flush()
        session.add(
            RoomTurnReservation(
                room_id=turn.room_id,
                turn_id=turn.turn_id,
                created_at=turn.created_at,
                updated_at=turn.updated_at,
            )
        )
        return turn

    async def get(self, turn_id: str) -> TurnRecord | None:
        async with self._session_factory() as session:
            record = await session.get(TurnRecordModel, turn_id)
            return self._turn_from_record(record) if record is not None else None

    async def get_by_client_action(
        self,
        room_id: str,
        client_action_id: str,
    ) -> TurnRecord | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TurnRecordModel).where(
                    TurnRecordModel.room_id == room_id,
                    TurnRecordModel.client_action_id == client_action_id,
                )
            )
            record = result.scalar_one_or_none()
            return self._turn_from_record(record) if record is not None else None

    async def list_for_player(
        self,
        *,
        room_id: str,
        player_id: str,
        active_only: bool,
        limit: int,
    ) -> tuple[TurnRecord, ...]:
        if limit < 1:
            raise TurnContractError("limit 必须大于 0")
        statement = select(TurnRecordModel).where(
            TurnRecordModel.room_id == room_id,
            TurnRecordModel.player_id == player_id,
        )
        if active_only:
            statement = statement.where(
                TurnRecordModel.status.not_in([status.value for status in TERMINAL_TURN_STATUSES])
            )
        statement = statement.order_by(
            TurnRecordModel.created_at.desc(), TurnRecordModel.turn_id.desc()
        ).limit(limit)
        async with self._session_factory() as session:
            result = await session.execute(statement)
            return tuple(self._turn_from_record(record) for record in result.scalars())

    async def compare_and_swap(
        self,
        *,
        expected_phase_version: int,
        updated: TurnRecord,
    ) -> TurnRecord:
        """在同一事务内校验身份、更新回合并按终态释放房间占用。"""

        async with self._session_factory() as session, session.begin():
            current_record = await session.get(TurnRecordModel, updated.turn_id)
            if current_record is None:
                raise TurnNotFoundError("TurnRecord 不存在")
            current = self._turn_from_record(current_record)
            validate_turn_cas_update(
                current,
                updated,
                expected_phase_version=expected_phase_version,
            )
            reservation = await session.get(RoomTurnReservation, updated.room_id)
            if not current.is_terminal and (
                reservation is None or reservation.turn_id != updated.turn_id
            ):
                raise TurnConflictError("TURN_RESERVATION_LOST", "回合已失去房间占用")
            result = await session.execute(
                update(TurnRecordModel)
                .where(
                    TurnRecordModel.turn_id == updated.turn_id,
                    TurnRecordModel.phase_version == expected_phase_version,
                )
                .values(**self._record_values(updated))
                .execution_options(synchronize_session=False)
            )
            if getattr(result, "rowcount", None) != 1:
                raise TurnConflictError("TURN_VERSION_CONFLICT", "回合已被其他 worker 更新")
            if updated.is_terminal:
                if reservation is not None and reservation.turn_id == updated.turn_id:
                    await session.delete(reservation)
            elif reservation is not None:
                reservation.updated_at = updated.updated_at
            await session.flush()
            return updated.model_copy(deep=True)

    async def claim(
        self,
        *,
        turn_id: str,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> TurnRecord:
        """用 phase_version CAS 领取或续租，过期 lease 可由其他 worker 接管。"""

        if not worker_id or lease_expires_at <= now:
            raise TurnContractError("worker lease 必须具有有效 owner 和未来截止时间")
        now_utc = _required_utc(now)
        async with self._session_factory() as session, session.begin():
            record = await session.get(TurnRecordModel, turn_id)
            if record is None:
                raise TurnNotFoundError("TurnRecord 不存在")
            current = self._turn_from_record(record)
            if current.is_terminal:
                return current
            lease_deadline = _as_utc(current.lease_expires_at)
            if (
                current.lease_owner is not None
                and current.lease_owner != worker_id
                and lease_deadline is not None
                and lease_deadline > now_utc
            ):
                raise TurnConflictError("TURN_WORKER_BUSY", "回合正由其他 worker 推进")
            claimed = TurnRecord.model_validate(
                {
                    **current.model_dump(),
                    "phase_version": current.phase_version + 1,
                    "lease_owner": worker_id,
                    "lease_expires_at": lease_expires_at,
                    "updated_at": now,
                }
            )
            result = await session.execute(
                update(TurnRecordModel)
                .where(
                    TurnRecordModel.turn_id == turn_id,
                    TurnRecordModel.phase_version == current.phase_version,
                )
                .values(**self._record_values(claimed))
                .execution_options(synchronize_session=False)
            )
            if getattr(result, "rowcount", None) != 1:
                raise TurnConflictError("TURN_VERSION_CONFLICT", "回合已被其他 worker 更新")
            return claimed.model_copy(deep=True)

    async def release_claim(
        self,
        *,
        turn_id: str,
        worker_id: str,
        expected_phase_version: int,
        now: datetime,
    ) -> TurnRecord:
        """只有当前 lease owner 能按版本释放自己的领取。"""

        async with self._session_factory() as session, session.begin():
            record = await session.get(TurnRecordModel, turn_id)
            if record is None:
                raise TurnNotFoundError("TurnRecord 不存在")
            current = self._turn_from_record(record)
            if current.phase_version != expected_phase_version:
                raise TurnConflictError("TURN_VERSION_CONFLICT", "回合已被其他 worker 更新")
            if current.lease_owner != worker_id:
                raise TurnConflictError("TURN_LEASE_LOST", "回合 worker lease 已失效")
            released = TurnRecord.model_validate(
                {
                    **current.model_dump(),
                    "phase_version": current.phase_version + 1,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "updated_at": now,
                }
            )
            result = await session.execute(
                update(TurnRecordModel)
                .where(
                    TurnRecordModel.turn_id == turn_id,
                    TurnRecordModel.phase_version == expected_phase_version,
                    TurnRecordModel.lease_owner == worker_id,
                )
                .values(**self._record_values(released))
                .execution_options(synchronize_session=False)
            )
            if getattr(result, "rowcount", None) != 1:
                raise TurnConflictError("TURN_LEASE_LOST", "回合 worker lease 已失效")
            return released.model_copy(deep=True)

    async def append_receipt(self, receipt: TurnCommitReceipt) -> TurnCommitReceipt:
        """追加 Engine 提交证明；同请求只允许完全相同的幂等重放。"""

        try:
            async with self._session_factory() as session, session.begin():
                session.add(
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
                await session.flush()
                return receipt.model_copy(deep=True)
        except IntegrityError as exc:
            existing = await self.get_receipt(receipt.room_id, receipt.engine_request_id)
            if existing is not None and existing == receipt:
                return existing
            raise TurnConflictError(
                "TURN_RECEIPT_CONFLICT",
                "Engine request 已存在不同提交证明",
            ) from exc

    async def get_receipt(
        self,
        room_id: str,
        engine_request_id: str,
    ) -> TurnCommitReceipt | None:
        async with self._session_factory() as session:
            record = await session.get(
                TurnCommitReceiptRecord,
                (room_id, engine_request_id),
            )
            if record is None:
                return None
            return TurnCommitReceipt(
                turn_id=record.turn_id,
                room_id=record.room_id,
                engine_request_id=record.engine_request_id,
                action_request_id=record.action_request_id,
                committed_state_version=record.committed_state_version,
                first_event_sequence=record.first_event_sequence,
                last_event_sequence=record.last_event_sequence,
                created_at=_required_utc(record.created_at),
            )

    async def list_receipts(self, turn_id: str) -> tuple[TurnCommitReceipt, ...]:
        """按提交顺序返回一个回合的全部 Engine receipt。"""

        async with self._session_factory() as session:
            result = await session.execute(
                select(TurnCommitReceiptRecord)
                .where(TurnCommitReceiptRecord.turn_id == turn_id)
                .order_by(
                    TurnCommitReceiptRecord.created_at,
                    TurnCommitReceiptRecord.engine_request_id,
                )
            )
            return tuple(self._receipt_from_record(record) for record in result.scalars())

    async def list_recoverable_turns(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[TurnRecord, ...]:
        """扫描启动后可自动接管的回合，不替玩家做选择或无限重试模型。"""

        if limit < 1:
            raise TurnContractError("limit 必须大于 0")
        statement = (
            select(TurnRecordModel)
            .where(
                TurnRecordModel.status.not_in([status.value for status in TERMINAL_TURN_STATUSES]),
                TurnRecordModel.resume_point != TurnResumePoint.AWAITING_PLAYER.value,
                or_(
                    TurnRecordModel.error_json.is_(None),
                    # 可重试错误仍然绑定原 Turn；租约过期后必须重新进入恢复队列，
                    # 否则房间 reservation 会永久阻塞新的玩家动作。
                    TurnRecordModel.error_json["retryable"].as_boolean().is_(True),
                    TurnRecordModel.status == TurnStatus.DELIVERING.value,
                ),
                or_(
                    and_(
                        TurnRecordModel.lease_owner.is_(None),
                        TurnRecordModel.updated_at <= now - timedelta(seconds=60),
                    ),
                    TurnRecordModel.lease_expires_at <= now,
                ),
            )
            .order_by(TurnRecordModel.updated_at, TurnRecordModel.turn_id)
            .limit(limit)
        )
        async with self._session_factory() as session:
            result = await session.scalars(statement)
            return tuple(self._turn_from_record(record) for record in result)

    async def put_outbox(
        self,
        message: NarrationOutboxMessage,
    ) -> tuple[NarrationOutboxMessage, bool]:
        """写入稳定最终叙事；同一 Turn 和消息类型只允许一条。"""

        try:
            async with self._session_factory() as session, session.begin():
                session.add(self._outbox_record(message))
                await session.flush()
                return message.model_copy(deep=True), True
        except IntegrityError as exc:
            existing = await self.get_outbox(message.turn_id, message.message_type)
            if existing is not None and existing == message:
                return existing, False
            raise TurnConflictError(
                "TURN_OUTBOX_CONFLICT",
                "回合已存在不同的最终叙事消息",
            ) from exc

    async def get_outbox(
        self,
        turn_id: str,
        message_type: str = "narration.push",
    ) -> NarrationOutboxMessage | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(NarrationOutboxRecord).where(
                    NarrationOutboxRecord.turn_id == turn_id,
                    NarrationOutboxRecord.message_type == message_type,
                )
            )
            record = result.scalar_one_or_none()
            return self._outbox_from_record(record) if record is not None else None

    async def publish_narration(
        self,
        *,
        expected_phase_version: int,
        updated_turn: TurnRecord,
        message: NarrationOutboxMessage,
        replay_event: TurnReplayEvent,
    ) -> tuple[TurnRecord, NarrationOutboxMessage, bool]:
        """原子持久化 ResultSnapshot、Outbox、回放事件与 delivering 状态。"""

        if updated_turn.status.value != "delivering" or updated_turn.result is None:
            raise TurnContractError("叙事发布必须把带结果的回合推进到 delivering")
        if not (
            updated_turn.turn_id == message.turn_id == replay_event.turn_id
            and updated_turn.room_id == message.room_id == replay_event.room_id
        ):
            raise TurnContractError("叙事发布的 Turn、Outbox 与回放事件身份不一致")
        async with self._session_factory() as session, session.begin():
            current_record = await session.get(TurnRecordModel, updated_turn.turn_id)
            if current_record is None:
                raise TurnNotFoundError("TurnRecord 不存在")
            existing = await session.scalar(
                select(NarrationOutboxRecord).where(
                    NarrationOutboxRecord.turn_id == message.turn_id,
                    NarrationOutboxRecord.message_type == message.message_type,
                )
            )
            if existing is not None:
                persisted = self._outbox_from_record(existing)
                if (
                    persisted.message_id != message.message_id
                    or persisted.payload != message.payload
                ):
                    raise TurnConflictError(
                        "TURN_OUTBOX_CONFLICT",
                        "回合已存在不同的最终叙事消息",
                    )
                return self._turn_from_record(current_record), persisted, False
            current = self._turn_from_record(current_record)
            validate_turn_cas_update(
                current,
                updated_turn,
                expected_phase_version=expected_phase_version,
            )
            reservation = await session.get(RoomTurnReservation, updated_turn.room_id)
            if reservation is None or reservation.turn_id != updated_turn.turn_id:
                raise TurnConflictError("TURN_RESERVATION_LOST", "回合已失去房间占用")
            result = await session.execute(
                update(TurnRecordModel)
                .where(
                    TurnRecordModel.turn_id == updated_turn.turn_id,
                    TurnRecordModel.phase_version == expected_phase_version,
                )
                .values(**self._record_values(updated_turn))
                .execution_options(synchronize_session=False)
            )
            if getattr(result, "rowcount", None) != 1:
                raise TurnConflictError("TURN_VERSION_CONFLICT", "回合已被其他 worker 更新")
            reservation.updated_at = updated_turn.updated_at
            session.add(self._outbox_record(message))
            session.add(
                Event(
                    id=replay_event.event_id,
                    turn_id=replay_event.turn_id,
                    room_id=replay_event.room_id,
                    player_id=replay_event.player_id,
                    event_type=replay_event.event_type,
                    correlation_id=replay_event.correlation_id,
                    visibility=replay_event.visibility,
                    actor_id=replay_event.actor_id,
                    scene_id=replay_event.scene_id,
                    view_revision=replay_event.view_revision,
                    payload=replay_event.payload,
                    created_at=replay_event.created_at,
                )
            )
            await session.flush()
            return updated_turn.model_copy(deep=True), message.model_copy(deep=True), True

    async def claim_due_outbox(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[NarrationOutboxMessage, ...]:
        """用条件更新领取到期 Outbox，过期 lease 可由其他 worker 接管。"""

        if not worker_id or lease_expires_at <= now or limit < 1:
            raise TurnContractError("Outbox worker、lease 与 limit 必须有效")
        claimed: list[NarrationOutboxMessage] = []
        async with self._session_factory() as session, session.begin():
            candidates = await session.scalars(
                select(NarrationOutboxRecord)
                .where(
                    NarrationOutboxRecord.next_attempt_at <= now,
                    or_(
                        NarrationOutboxRecord.status == TurnOutboxStatus.PENDING.value,
                        and_(
                            NarrationOutboxRecord.status == TurnOutboxStatus.LEASED.value,
                            NarrationOutboxRecord.lease_expires_at <= now,
                        ),
                    ),
                )
                .order_by(NarrationOutboxRecord.created_at, NarrationOutboxRecord.outbox_id)
                .limit(limit)
            )
            for record in candidates:
                result = await session.execute(
                    update(NarrationOutboxRecord)
                    .where(
                        NarrationOutboxRecord.outbox_id == record.outbox_id,
                        or_(
                            NarrationOutboxRecord.status == TurnOutboxStatus.PENDING.value,
                            and_(
                                NarrationOutboxRecord.status == TurnOutboxStatus.LEASED.value,
                                NarrationOutboxRecord.lease_expires_at <= now,
                            ),
                        ),
                    )
                    .values(
                        status=TurnOutboxStatus.LEASED.value,
                        lease_owner=worker_id,
                        lease_expires_at=lease_expires_at,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if getattr(result, "rowcount", None) != 1:
                    continue
                claimed.append(
                    NarrationOutboxMessage.model_validate(
                        {
                            **self._outbox_from_record(record).model_dump(),
                            "status": TurnOutboxStatus.LEASED,
                            "lease_owner": worker_id,
                            "lease_expires_at": lease_expires_at,
                            "updated_at": now,
                        }
                    )
                )
        return tuple(claimed)

    async def settle_outbox(
        self,
        *,
        outbox_id: str,
        worker_id: str,
        outcome: str,
        now: datetime,
        next_attempt_at: datetime,
        error_code: str | None = None,
        max_attempts: int = 5,
    ) -> NarrationOutboxMessage:
        """按 lease 结束投递；无在线接收者不会消耗失败次数。"""

        async with self._session_factory() as session, session.begin():
            record = await session.get(NarrationOutboxRecord, outbox_id)
            if (
                record is None
                or record.status != TurnOutboxStatus.LEASED.value
                or record.lease_owner != worker_id
            ):
                raise TurnConflictError("TURN_OUTBOX_LEASE_LOST", "Outbox lease 已失效")
            attempts = record.attempt_count + (0 if outcome == "no_recipient" else 1)
            if outcome == "dispatched":
                status = TurnOutboxStatus.DISPATCHED
            elif outcome == "failed" and attempts >= max_attempts:
                status = TurnOutboxStatus.DEAD_LETTER
            elif outcome in {"failed", "no_recipient"}:
                status = TurnOutboxStatus.PENDING
            else:
                raise TurnContractError("未知 Outbox 投递结果")
            values = {
                "status": status.value,
                "attempt_count": attempts,
                "lease_owner": None,
                "lease_expires_at": None,
                "next_attempt_at": next_attempt_at,
                "last_error_code": error_code if outcome == "failed" else None,
                "updated_at": now,
                "last_dispatched_at": now if outcome == "dispatched" else record.last_dispatched_at,
            }
            result = await session.execute(
                update(NarrationOutboxRecord)
                .where(
                    NarrationOutboxRecord.outbox_id == outbox_id,
                    NarrationOutboxRecord.status == TurnOutboxStatus.LEASED.value,
                    NarrationOutboxRecord.lease_owner == worker_id,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if getattr(result, "rowcount", None) != 1:
                raise TurnConflictError("TURN_OUTBOX_LEASE_LOST", "Outbox lease 已失效")
            return NarrationOutboxMessage.model_validate(
                {**self._outbox_from_record(record).model_dump(), **values}
            )

    @staticmethod
    async def _get_by_client_action(
        session: AsyncSession,
        proposed: TurnRecord,
    ) -> TurnRecord | None:
        result = await session.execute(
            select(TurnRecordModel).where(
                TurnRecordModel.room_id == proposed.room_id,
                TurnRecordModel.client_action_id == proposed.client_action_id,
            )
        )
        record = result.scalar_one_or_none()
        return SqlAlchemyTurnStore._turn_from_record(record) if record is not None else None

    @staticmethod
    def _record_from_turn(turn: TurnRecord) -> TurnRecordModel:
        return TurnRecordModel(
            turn_id=turn.turn_id,
            room_id=turn.room_id,
            client_action_id=turn.client_action_id,
            input_fingerprint=turn.input_fingerprint,
            player_id=turn.player_id,
            actor_id=turn.actor_id,
            request_schema_version=turn.request.schema_version,
            request_json=turn.request.model_dump(mode="json"),
            status=turn.status.value,
            phase_version=turn.phase_version,
            resume_point=turn.resume_point.value,
            waiting_reason=turn.waiting_reason.value,
            commit_state=turn.commit_state.value,
            recovery_action=turn.recovery_action.value,
            pending_decision_json=turn.pending_decision,
            error_schema_version=turn.last_error.schema_version if turn.last_error else None,
            error_json=turn.last_error.model_dump(mode="json") if turn.last_error else None,
            result_schema_version=turn.result.schema_version if turn.result else None,
            result_json=turn.result.model_dump(mode="json") if turn.result else None,
            lease_owner=turn.lease_owner,
            lease_expires_at=turn.lease_expires_at,
            created_at=turn.created_at,
            updated_at=turn.updated_at,
            completed_at=turn.completed_at,
        )

    @staticmethod
    def _record_values(turn: TurnRecord) -> dict[str, object]:
        return {
            "status": turn.status.value,
            "phase_version": turn.phase_version,
            "resume_point": turn.resume_point.value,
            "waiting_reason": turn.waiting_reason.value,
            "commit_state": turn.commit_state.value,
            "recovery_action": turn.recovery_action.value,
            "pending_decision_json": turn.pending_decision,
            "error_schema_version": turn.last_error.schema_version if turn.last_error else None,
            "error_json": turn.last_error.model_dump(mode="json") if turn.last_error else None,
            "result_schema_version": turn.result.schema_version if turn.result else None,
            "result_json": turn.result.model_dump(mode="json") if turn.result else None,
            "lease_owner": turn.lease_owner,
            "lease_expires_at": turn.lease_expires_at,
            "updated_at": turn.updated_at,
            "completed_at": turn.completed_at,
        }

    @staticmethod
    def _turn_from_record(record: TurnRecordModel) -> TurnRecord:
        return TurnRecord.model_validate(
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
                "last_error": TurnFailureSnapshot.model_validate(record.error_json)
                if record.error_json
                else None,
                "result": TurnResultSnapshot.model_validate(record.result_json)
                if record.result_json
                else None,
                "lease_owner": record.lease_owner,
                "lease_expires_at": _as_utc(record.lease_expires_at),
                "created_at": _as_utc(record.created_at),
                "updated_at": _as_utc(record.updated_at),
                "completed_at": _as_utc(record.completed_at),
            }
        )

    @staticmethod
    def _outbox_record(message: NarrationOutboxMessage) -> NarrationOutboxRecord:
        return NarrationOutboxRecord(
            outbox_id=message.outbox_id,
            turn_id=message.turn_id,
            room_id=message.room_id,
            player_id=message.player_id,
            message_id=message.message_id,
            message_type=message.message_type,
            visibility=message.visibility,
            payload_schema_version=message.payload_schema_version,
            payload_json=message.payload,
            status=message.status.value,
            attempt_count=message.attempt_count,
            lease_owner=message.lease_owner,
            lease_expires_at=message.lease_expires_at,
            next_attempt_at=message.next_attempt_at,
            last_error_code=message.last_error_code,
            created_at=message.created_at,
            updated_at=message.updated_at,
            last_dispatched_at=message.last_dispatched_at,
        )

    @staticmethod
    def _outbox_from_record(record: NarrationOutboxRecord) -> NarrationOutboxMessage:
        return NarrationOutboxMessage(
            outbox_id=record.outbox_id,
            turn_id=record.turn_id,
            room_id=record.room_id,
            player_id=record.player_id,
            message_id=record.message_id,
            message_type=record.message_type,
            visibility=record.visibility,
            payload_schema_version=record.payload_schema_version,
            payload=record.payload_json,
            status=record.status,
            attempt_count=record.attempt_count,
            lease_owner=record.lease_owner,
            lease_expires_at=_as_utc(record.lease_expires_at),
            next_attempt_at=_required_utc(record.next_attempt_at),
            last_error_code=record.last_error_code,
            created_at=_required_utc(record.created_at),
            updated_at=_required_utc(record.updated_at),
            last_dispatched_at=_as_utc(record.last_dispatched_at),
        )

    @staticmethod
    def _receipt_from_record(record: TurnCommitReceiptRecord) -> TurnCommitReceipt:
        """把 ORM receipt 转换为跨 Store 共用的核心契约。"""

        return TurnCommitReceipt(
            turn_id=record.turn_id,
            room_id=record.room_id,
            engine_request_id=record.engine_request_id,
            action_request_id=record.action_request_id,
            committed_state_version=record.committed_state_version,
            first_event_sequence=record.first_event_sequence,
            last_event_sequence=record.last_event_sequence,
            created_at=_required_utc(record.created_at),
        )

    @staticmethod
    def _require_same_idempotent_request(current: TurnRecord, proposed: TurnRecord) -> None:
        if (
            current.room_id != proposed.room_id
            or current.client_action_id != proposed.client_action_id
            or current.input_fingerprint != proposed.input_fingerprint
            or current.player_id != proposed.player_id
            or current.actor_id != proposed.actor_id
        ):
            raise TurnConflictError(
                "TURN_IDEMPOTENCY_CONFLICT",
                "回合幂等键已被不同输入占用",
            )


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 会丢失 timezone 标记，读出后统一恢复为 UTC。"""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _required_utc(value: datetime) -> datetime:
    """把数据库非空时间转换为 UTC，并向类型检查器保留非空信息。"""

    converted = _as_utc(value)
    if converted is None:  # pragma: no cover - 仅防御错误 ORM 数据
        raise TurnContractError("数据库必填时间字段为空")
    return converted
