"""通过可靠 Turn 恢复入口调度 RuleAgenda，禁止后台绕过 Coordinator 写状态。"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

import structlog

from app.core.engine import engine_store
from app.service.reliable_turn_runtime import resume_turn_by_id

logger = structlog.get_logger()


class RecoverableAgendaSource(Protocol):
    """后台扫描只需要读取 Agenda 与非终态 Turn 的绑定。"""

    async def list_recoverable_rule_agenda_bindings(
        self,
        *,
        now: datetime,
        limit: int = 20,
    ) -> tuple[tuple[str, str], ...]: ...


class RuleAgendaSupervisor:
    """定期唤醒拥有可执行 Agenda 的可靠 Turn。"""

    def __init__(
        self,
        *,
        source: RecoverableAgendaSource,
        resume: Callable[[str], Awaitable[object]] = resume_turn_by_id,
        interval_seconds: float = 2.0,
    ) -> None:
        self._source = source
        self._resume = resume
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            await self.run_once()
            self._task = asyncio.create_task(self._run(), name="rule-agenda-supervisor")

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
                logger.warning(
                    "rule_agenda_supervisor_failed",
                    error_type=type(exc).__name__,
                )

    async def run_once(self, *, limit: int = 20) -> None:
        """逐个恢复 Turn；单个坏 Agenda 不能阻塞同批其他房间。"""

        bindings = await self._source.list_recoverable_rule_agenda_bindings(
            now=datetime.now(UTC),
            limit=limit,
        )
        for room_id, turn_id in bindings:
            try:
                await self._resume(turn_id)
            except Exception as exc:
                logger.warning(
                    "rule_agenda_turn_recovery_failed",
                    room_id=room_id,
                    turn_id=turn_id,
                    error_type=type(exc).__name__,
                )


rule_agenda_supervisor = RuleAgendaSupervisor(source=engine_store)

__all__ = ["RuleAgendaSupervisor", "rule_agenda_supervisor"]
