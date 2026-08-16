"""使用 SQLAlchemy 持久化 Memory 投影游标、条目与玩家安全查询。"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime

from collaboration_framework.contracts import ContractError
from collaboration_framework.memory import (
    MemoryBudget,
    MemoryContext,
    MemoryEntry,
    MemoryProjectionRun,
    MemoryQuery,
    MemoryReadScope,
)
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.memory import MemoryEntryRecord, MemoryProjectionRunRecord

_CANDIDATE_LIMIT = 256


def _required_utc(value: datetime) -> datetime:
    """SQLite 可能返回 naive datetime，统一按 UTC 解释后进入领域契约。"""

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _entry_chars(entry: MemoryEntry) -> int:
    return len(entry.search_text) + sum(len(str(value)) for value in entry.content.values())


class SqlAlchemyMemoryStore:
    """以数据库约束和事务实现 MemoryStore 的 CAS 与原子投影。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_or_get_run(
        self, proposed: MemoryProjectionRun
    ) -> tuple[MemoryProjectionRun, bool]:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(self._run_record(proposed))
                await session.flush()
            return proposed, True
        except IntegrityError as exc:
            existing = await self.get_run(proposed.turn_id)
            if existing is None:
                raise ContractError("Memory Projection Run 创建冲突") from exc
            if (
                existing.room_id != proposed.room_id
                or existing.source_fingerprint != proposed.source_fingerprint
                or existing.projection_version != proposed.projection_version
            ):
                raise ContractError("同一 Turn 已存在不同 Memory 投影来源") from exc
            return existing, False

    async def get_run(self, turn_id: str) -> MemoryProjectionRun | None:
        async with self._session_factory() as session:
            record = await session.get(MemoryProjectionRunRecord, turn_id)
            return self._run_from_record(record) if record is not None else None

    async def claim_run(
        self,
        *,
        turn_id: str,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MemoryProjectionRun:
        """通过单条条件 UPDATE 领取到期任务，跨 worker 只允许一个成功。"""

        statement = (
            update(MemoryProjectionRunRecord)
            .where(
                MemoryProjectionRunRecord.turn_id == turn_id,
                MemoryProjectionRunRecord.next_attempt_at <= now,
                or_(
                    MemoryProjectionRunRecord.status.in_(["pending", "retryable_failure"]),
                    and_(
                        MemoryProjectionRunRecord.status == "leased",
                        MemoryProjectionRunRecord.lease_expires_at <= now,
                    ),
                ),
            )
            .values(
                status="leased",
                version=MemoryProjectionRunRecord.version + 1,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                last_error_code=None,
                updated_at=now,
            )
        )
        async with self._session_factory() as session, session.begin():
            result = await session.execute(statement)
            if getattr(result, "rowcount", None) != 1:
                raise ContractError("Memory Projection Run 当前不可领取")
            record = await session.get(MemoryProjectionRunRecord, turn_id)
            if record is None:
                raise ContractError("Memory Projection Run 不存在")
            return self._run_from_record(record)

    async def complete_run(
        self,
        *,
        turn_id: str,
        worker_id: str,
        expected_version: int,
        entries: tuple[MemoryEntry, ...],
        supersessions: tuple[tuple[str, str], ...],
        now: datetime,
    ) -> MemoryProjectionRun:
        """在同一事务写入全部 Memory、supersede 关系和完成游标。"""

        async with self._session_factory() as session, session.begin():
            record = await session.get(
                MemoryProjectionRunRecord,
                turn_id,
                with_for_update=True,
            )
            self._require_owned_run(record, worker_id, expected_version)
            assert record is not None
            self._validate_entries(record, entries)
            session.add_all(self._entry_record(entry, projected_at=now) for entry in entries)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise ContractError("Memory 批次包含冲突的稳定 ID") from exc

            for previous_id, replacement_id in supersessions:
                previous = await session.get(MemoryEntryRecord, previous_id)
                replacement = await session.get(MemoryEntryRecord, replacement_id)
                if previous is None or replacement is None:
                    raise ContractError("Memory supersede 引用了不存在的记录")
                if previous.room_id != record.room_id or replacement.room_id != record.room_id:
                    raise ContractError("Memory supersede 不得跨房间")
                previous.superseded_by = replacement_id

            record.status = "completed"
            record.version += 1
            record.lease_owner = None
            record.lease_expires_at = None
            record.updated_at = now
            record.completed_at = now
            await session.flush()
            return self._run_from_record(record)

    async def fail_run(
        self,
        *,
        turn_id: str,
        worker_id: str,
        expected_version: int,
        error_code: str,
        retryable: bool,
        next_attempt_at: datetime,
        now: datetime,
    ) -> MemoryProjectionRun:
        async with self._session_factory() as session, session.begin():
            record = await session.get(
                MemoryProjectionRunRecord,
                turn_id,
                with_for_update=True,
            )
            self._require_owned_run(record, worker_id, expected_version)
            assert record is not None
            record.status = "retryable_failure" if retryable else "dead_letter"
            record.version += 1
            record.attempt_count += 1
            record.lease_owner = None
            record.lease_expires_at = None
            record.next_attempt_at = next_attempt_at
            record.last_error_code = error_code
            record.updated_at = now
            record.completed_at = None if retryable else now
            await session.flush()
            return self._run_from_record(record)

    async def list_due_runs(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[MemoryProjectionRun, ...]:
        if limit < 1:
            raise ContractError("Memory Projection limit 必须大于 0")
        statement = (
            select(MemoryProjectionRunRecord)
            .where(
                or_(
                    and_(
                        MemoryProjectionRunRecord.status.in_(["pending", "retryable_failure"]),
                        MemoryProjectionRunRecord.next_attempt_at <= now,
                    ),
                    and_(
                        MemoryProjectionRunRecord.status == "leased",
                        MemoryProjectionRunRecord.lease_expires_at <= now,
                    ),
                )
            )
            .order_by(
                MemoryProjectionRunRecord.next_attempt_at,
                MemoryProjectionRunRecord.created_at,
                MemoryProjectionRunRecord.turn_id,
            )
            .limit(limit)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
            return tuple(self._run_from_record(record) for record in records)

    async def read_context(
        self,
        *,
        scope: MemoryReadScope,
        query: MemoryQuery,
        budget: MemoryBudget,
    ) -> MemoryContext:
        conditions = [
            MemoryEntryRecord.room_id == scope.room_id,
            or_(
                MemoryEntryRecord.visibility == "public",
                and_(
                    MemoryEntryRecord.visibility == "player_scoped",
                    MemoryEntryRecord.viewer_player_id == scope.viewer_player_id,
                ),
            ),
            or_(
                MemoryEntryRecord.scope != "player",
                MemoryEntryRecord.scope_owner_id == scope.viewer_actor_id,
            ),
        ]
        if scope.visible_entity_ids:
            conditions.append(
                or_(
                    MemoryEntryRecord.scope != "entity",
                    MemoryEntryRecord.scope_owner_id.in_(scope.visible_entity_ids),
                )
            )
        else:
            conditions.append(MemoryEntryRecord.scope != "entity")
        if not query.include_superseded:
            conditions.append(MemoryEntryRecord.superseded_by.is_(None))
        if query.kinds:
            conditions.append(MemoryEntryRecord.kind.in_(query.kinds))
        if query.subject_ids:
            conditions.append(MemoryEntryRecord.subject_id.in_(query.subject_ids))
        if query.location_ids:
            conditions.append(MemoryEntryRecord.location_id.in_(query.location_ids))

        statement = (
            select(MemoryEntryRecord)
            .where(*conditions)
            .order_by(
                MemoryEntryRecord.source_sequence.desc().nullslast(),
                MemoryEntryRecord.created_at.desc(),
                MemoryEntryRecord.memory_id,
            )
            .limit(_CANDIDATE_LIMIT)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()

        query_text = _normalize(query.text) if query.text is not None else None
        candidates = [
            self._entry_from_record(record)
            for record in records
            if query_text is None or query_text in _normalize(record.search_text)
        ]
        candidates.sort(
            key=lambda item: (
                item.location_id != scope.current_location_id,
                -(item.source_sequence or 0),
                -item.created_at.timestamp(),
                item.memory_id,
            )
        )
        selected: list[MemoryEntry] = []
        used_chars = 0
        for entry in candidates:
            chars = _entry_chars(entry)
            if len(selected) >= budget.max_entries or used_chars + chars > budget.max_chars:
                continue
            selected.append(entry)
            used_chars += chars
        return MemoryContext(
            room_id=scope.room_id,
            viewer_player_id=scope.viewer_player_id,
            viewer_actor_id=scope.viewer_actor_id,
            as_of_revision=scope.as_of_revision,
            entries=tuple(selected),
            truncated_count=len(candidates) - len(selected),
        )

    async def reset_room(self, room_id: str) -> None:
        """仅删除可重建投影；Engine、Turn、Event 和 Outbox 保持不变。"""

        async with self._session_factory() as session, session.begin():
            await session.execute(
                delete(MemoryEntryRecord).where(MemoryEntryRecord.room_id == room_id)
            )
            await session.execute(
                delete(MemoryProjectionRunRecord).where(
                    MemoryProjectionRunRecord.room_id == room_id
                )
            )

    @staticmethod
    def _require_owned_run(
        record: MemoryProjectionRunRecord | None,
        worker_id: str,
        expected_version: int,
    ) -> None:
        if record is None:
            raise ContractError("Memory Projection Run 不存在")
        if (
            record.status != "leased"
            or record.lease_owner != worker_id
            or record.version != expected_version
        ):
            raise ContractError("Memory Projection Run lease 或版本不匹配")

    @staticmethod
    def _validate_entries(
        run: MemoryProjectionRunRecord,
        entries: tuple[MemoryEntry, ...],
    ) -> None:
        ids = [entry.memory_id for entry in entries]
        if len(ids) != len(set(ids)):
            raise ContractError("同一投影批次不得包含重复 Memory ID")
        if any(
            entry.room_id != run.room_id or entry.source_turn_id != run.turn_id for entry in entries
        ):
            raise ContractError("Memory Entry 不属于当前 Projection Run")

    @staticmethod
    def _run_record(run: MemoryProjectionRun) -> MemoryProjectionRunRecord:
        return MemoryProjectionRunRecord(
            turn_id=run.turn_id,
            room_id=run.room_id,
            schema_version=run.schema_version,
            projection_version=run.projection_version,
            source_fingerprint=run.source_fingerprint,
            status=run.status,
            version=run.version,
            attempt_count=run.attempt_count,
            lease_owner=run.lease_owner,
            lease_expires_at=run.lease_expires_at,
            next_attempt_at=run.next_attempt_at,
            last_error_code=run.last_error_code,
            created_at=run.created_at,
            updated_at=run.updated_at,
            completed_at=run.completed_at,
        )

    @staticmethod
    def _run_from_record(record: MemoryProjectionRunRecord) -> MemoryProjectionRun:
        return MemoryProjectionRun.model_validate(
            {
                "schema_version": record.schema_version,
                "projection_version": record.projection_version,
                "turn_id": record.turn_id,
                "room_id": record.room_id,
                "source_fingerprint": record.source_fingerprint,
                "status": record.status,
                "version": record.version,
                "attempt_count": record.attempt_count,
                "lease_owner": record.lease_owner,
                "lease_expires_at": (
                    _required_utc(record.lease_expires_at)
                    if record.lease_expires_at is not None
                    else None
                ),
                "next_attempt_at": _required_utc(record.next_attempt_at),
                "last_error_code": record.last_error_code,
                "created_at": _required_utc(record.created_at),
                "updated_at": _required_utc(record.updated_at),
                "completed_at": (
                    _required_utc(record.completed_at) if record.completed_at is not None else None
                ),
            }
        )

    @staticmethod
    def _entry_record(entry: MemoryEntry, *, projected_at: datetime) -> MemoryEntryRecord:
        return MemoryEntryRecord(
            memory_id=entry.memory_id,
            room_id=entry.room_id,
            source_turn_id=entry.source_turn_id,
            schema_version=entry.schema_version,
            projection_version=entry.projection_version,
            kind=entry.kind,
            subject_id=entry.subject_id,
            object_id=entry.object_id,
            location_id=entry.location_id,
            source_kind=entry.source_kind,
            source_id=entry.source_id,
            source_event_id=entry.source_event_id,
            source_sequence=entry.source_sequence,
            source_ordinal=entry.source_ordinal,
            scope=entry.scope,
            scope_owner_id=entry.scope_owner_id,
            visibility=entry.visibility,
            viewer_player_id=entry.viewer_player_id,
            epistemic_status=entry.epistemic_status,
            topic_key=entry.topic_key,
            content_json=entry.content,
            search_text=entry.search_text,
            created_at=entry.created_at,
            projected_at=projected_at,
            superseded_by=entry.superseded_by,
        )

    @staticmethod
    def _entry_from_record(record: MemoryEntryRecord) -> MemoryEntry:
        return MemoryEntry.model_validate(
            {
                "schema_version": record.schema_version,
                "projection_version": record.projection_version,
                "memory_id": record.memory_id,
                "room_id": record.room_id,
                "kind": record.kind,
                "subject_id": record.subject_id,
                "object_id": record.object_id,
                "location_id": record.location_id,
                "source_turn_id": record.source_turn_id,
                "source_kind": record.source_kind,
                "source_id": record.source_id,
                "source_event_id": record.source_event_id,
                "source_sequence": record.source_sequence,
                "source_ordinal": record.source_ordinal,
                "scope": record.scope,
                "scope_owner_id": record.scope_owner_id,
                "visibility": record.visibility,
                "viewer_player_id": record.viewer_player_id,
                "epistemic_status": record.epistemic_status,
                "topic_key": record.topic_key,
                "content": record.content_json,
                "search_text": record.search_text,
                "created_at": _required_utc(record.created_at),
                "superseded_by": record.superseded_by,
            }
        )
