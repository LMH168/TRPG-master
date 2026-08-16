"""调度可靠回合的 Memory 投影、失败恢复与按房间重建。"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

import structlog
from collaboration_framework.contracts import ContractError
from collaboration_framework.memory import (
    MemoryProjectionSource,
    MemoryStore,
    new_memory_projection_run,
    project_memory_entries,
)

from app.adapters import SqlAlchemyMemoryProjectionSource, SqlAlchemyMemoryStore
from app.core.db import async_session_factory

logger = structlog.get_logger()
_MAX_ATTEMPTS = 5
_LEASE_SECONDS = 30


class MemoryProjectionSourcePort(Protocol):
    """Supervisor 读取终态 Turn 来源所需的最小只读端口。"""

    async def list_unregistered_turn_ids(
        self, *, room_id: str | None = None, limit: int = 20
    ) -> tuple[str, ...]: ...

    async def load(self, turn_id: str) -> MemoryProjectionSource | None: ...


@dataclass(frozen=True)
class MemoryProjectionReport:
    """一次扫描或重建的确定性计数，供日志、脚本和测试使用。"""

    registered: int = 0
    projected: int = 0
    skipped: int = 0
    duplicate: int = 0
    retry: int = 0
    dead_letter: int = 0

    def add(self, other: MemoryProjectionReport) -> MemoryProjectionReport:
        return MemoryProjectionReport(
            registered=self.registered + other.registered,
            projected=self.projected + other.projected,
            skipped=self.skipped + other.skipped,
            duplicate=self.duplicate + other.duplicate,
            retry=self.retry + other.retry,
            dead_letter=self.dead_letter + other.dead_letter,
        )


class MemoryProjectionSupervisor:
    """在 Engine 事务之外持续投影终态 Turn，并恢复过期 lease。"""

    def __init__(
        self,
        *,
        store: MemoryStore,
        source: MemoryProjectionSourcePort,
        worker_id: str | None = None,
        interval_seconds: float = 2.0,
    ) -> None:
        self._store = store
        self._source = source
        self._worker_id = worker_id or f"memory-{uuid4()}"
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            try:
                await self.run_once()
            except Exception as exc:
                # 首次补投影失败也不能阻止 API 启动，后台循环会在下一周期重试。
                logger.warning(
                    "memory_projection_initial_scan_failed",
                    error_type=type(exc).__name__,
                )
            self._task = asyncio.create_task(self._run(), name="memory-projection-supervisor")

    async def shutdown(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self.run_once()
            except Exception as exc:
                # 投影是派生读模型，后台故障不得影响可靠回合和 Outbox。
                logger.warning(
                    "memory_projection_supervisor_failed",
                    error_type=type(exc).__name__,
                )

    async def run_once(self, *, limit: int = 20) -> MemoryProjectionReport:
        """注册新终态 Turn，再处理到期任务；单个坏来源不会阻塞整批。"""

        report = MemoryProjectionReport()
        turn_ids = await self._source.list_unregistered_turn_ids(limit=limit)
        for turn_id in turn_ids:
            try:
                report = report.add(await self._register(turn_id))
            except Exception as exc:
                # 一条损坏的历史来源不能阻止同批其他终态 Turn 建立投影任务。
                logger.warning(
                    "memory_projection_registration_failed",
                    turn_id=turn_id,
                    error_type=type(exc).__name__,
                )
                report = report.add(MemoryProjectionReport(skipped=1))

        now = datetime.now(UTC)
        due = await self._store.list_due_runs(now=now, limit=limit)
        for run in due:
            try:
                report = report.add(await self._project(run.turn_id, now=now))
            except Exception as exc:
                # lease 竞争或 Store 短暂故障只影响当前任务，后续任务仍可继续。
                logger.warning(
                    "memory_projection_task_failed",
                    turn_id=run.turn_id,
                    error_type=type(exc).__name__,
                )
                report = report.add(MemoryProjectionReport(retry=1))
        self._log_report("memory_projection_scan", report)
        return report

    async def rebuild_room(
        self,
        room_id: str,
        *,
        batch_size: int = 100,
    ) -> MemoryProjectionReport:
        """清空并用同一投影函数重建一个房间，不修改任何权威来源。"""

        await self._store.reset_room(room_id)
        report = MemoryProjectionReport()
        processed: set[str] = set()
        while True:
            turn_ids = await self._source.list_unregistered_turn_ids(
                room_id=room_id,
                limit=batch_size,
            )
            pending = tuple(turn_id for turn_id in turn_ids if turn_id not in processed)
            if not pending:
                break
            for turn_id in pending:
                processed.add(turn_id)
                report = report.add(await self._register(turn_id))
                report = report.add(await self._project(turn_id, now=datetime.now(UTC)))
        self._log_report("memory_projection_rebuild", report, room_id=room_id)
        return report

    async def _register(self, turn_id: str) -> MemoryProjectionReport:
        source = await self._source.load(turn_id)
        if source is None:
            logger.info("memory_projection_skipped", turn_id=turn_id, reason="source_unavailable")
            return MemoryProjectionReport(skipped=1)
        _, created = await self._store.create_or_get_run(
            new_memory_projection_run(
                turn_id=source.turn_id,
                room_id=source.room_id,
                source_fingerprint=source.fingerprint(),
                now=datetime.now(UTC),
            )
        )
        return MemoryProjectionReport(registered=int(created), duplicate=int(not created))

    async def _project(
        self,
        turn_id: str,
        *,
        now: datetime,
    ) -> MemoryProjectionReport:
        try:
            claimed = await self._store.claim_run(
                turn_id=turn_id,
                worker_id=self._worker_id,
                now=now,
                lease_expires_at=now + timedelta(seconds=_LEASE_SECONDS),
            )
        except ContractError:
            return MemoryProjectionReport(duplicate=1)

        try:
            source = await self._source.load(turn_id)
            if source is None:
                return await self._fail(
                    claimed=claimed,
                    now=now,
                    error_code="SOURCE_UNAVAILABLE",
                    retryable=True,
                )
            if source.fingerprint() != claimed.source_fingerprint:
                return await self._fail(
                    claimed=claimed,
                    now=now,
                    error_code="SOURCE_FINGERPRINT_CHANGED",
                    retryable=False,
                )
            entries = project_memory_entries(source) if source.is_reliably_projectable else ()
            await self._store.complete_run(
                turn_id=turn_id,
                worker_id=self._worker_id,
                expected_version=claimed.version,
                entries=entries,
                supersessions=(),
                now=datetime.now(UTC),
            )
            if not source.is_reliably_projectable:
                logger.info("memory_projection_skipped", turn_id=turn_id, reason="no_receipt")
                return MemoryProjectionReport(skipped=1)
            logger.info(
                "memory_projection_projected",
                turn_id=turn_id,
                entry_count=len(entries),
            )
            return MemoryProjectionReport(projected=1)
        except Exception as exc:
            logger.warning(
                "memory_projection_failed",
                turn_id=turn_id,
                error_type=type(exc).__name__,
            )
            return await self._fail(
                claimed=claimed,
                now=datetime.now(UTC),
                error_code="PROJECTION_FAILED",
                retryable=True,
            )

    async def _fail(
        self,
        *,
        claimed,  # noqa: ANN001
        now: datetime,
        error_code: str,
        retryable: bool,
    ) -> MemoryProjectionReport:
        """按最多五次策略释放 lease；第五次失败转为 dead letter。"""

        next_attempt = claimed.attempt_count + 1
        can_retry = retryable and next_attempt < _MAX_ATTEMPTS
        delay = min(60, 2**claimed.attempt_count)
        await self._store.fail_run(
            turn_id=claimed.turn_id,
            worker_id=self._worker_id,
            expected_version=claimed.version,
            error_code=error_code,
            retryable=can_retry,
            next_attempt_at=now + timedelta(seconds=delay),
            now=now,
        )
        logger.warning(
            "memory_projection_retry" if can_retry else "memory_projection_dead_letter",
            turn_id=claimed.turn_id,
            attempt_count=next_attempt,
            error_code=error_code,
        )
        return MemoryProjectionReport(retry=int(can_retry), dead_letter=int(not can_retry))

    @staticmethod
    def _log_report(
        event: str,
        report: MemoryProjectionReport,
        *,
        room_id: str | None = None,
    ) -> None:
        logger.info(
            event,
            room_id=room_id,
            registered=report.registered,
            projected=report.projected,
            skipped=report.skipped,
            duplicate=report.duplicate,
            retry=report.retry,
            dead_letter=report.dead_letter,
        )


memory_store = SqlAlchemyMemoryStore(async_session_factory)
memory_projection_source = SqlAlchemyMemoryProjectionSource(async_session_factory)
memory_projection_supervisor = MemoryProjectionSupervisor(
    store=memory_store,
    source=memory_projection_source,
)

__all__ = [
    "MemoryProjectionReport",
    "MemoryProjectionSupervisor",
    "memory_projection_source",
    "memory_projection_supervisor",
    "memory_store",
]
