"""定义 Memory 投影写入、恢复和玩家安全读取所依赖的存储端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .contracts import (
    MemoryBudget,
    MemoryContext,
    MemoryEntry,
    MemoryProjectionRun,
    MemoryQuery,
    MemoryReadScope,
)


class MemoryStore(Protocol):
    """隔离投影运行状态、原子写入和玩家安全查询的统一端口。"""

    async def create_or_get_run(
        self, proposed: MemoryProjectionRun
    ) -> tuple[MemoryProjectionRun, bool]: ...

    async def get_run(self, turn_id: str) -> MemoryProjectionRun | None: ...

    async def claim_run(
        self,
        *,
        turn_id: str,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MemoryProjectionRun: ...

    async def complete_run(
        self,
        *,
        turn_id: str,
        worker_id: str,
        expected_version: int,
        entries: tuple[MemoryEntry, ...],
        supersessions: tuple[tuple[str, str], ...],
        now: datetime,
    ) -> MemoryProjectionRun: ...

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
    ) -> MemoryProjectionRun: ...

    async def list_due_runs(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[MemoryProjectionRun, ...]: ...

    async def read_context(
        self,
        *,
        scope: MemoryReadScope,
        query: MemoryQuery,
        budget: MemoryBudget,
    ) -> MemoryContext: ...

    async def reset_room(self, room_id: str) -> None: ...
