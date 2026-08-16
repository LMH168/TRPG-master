"""提供用于领域测试的确定性、并发安全内存 Memory Store。"""

from __future__ import annotations

import asyncio
import unicodedata
from datetime import datetime

from collaboration_framework.contracts import ContractError

from .contracts import (
    MemoryBudget,
    MemoryContext,
    MemoryEntry,
    MemoryProjectionRun,
    MemoryQuery,
    MemoryReadScope,
)


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _entry_chars(entry: MemoryEntry) -> int:
    return len(entry.search_text) + sum(
        len(str(value)) for value in entry.content.values()
    )


class InMemoryMemoryStore:
    """模拟 SQL Store 的 CAS、原子完成和玩家可见性约束。"""

    def __init__(self) -> None:
        self._runs: dict[str, MemoryProjectionRun] = {}
        self._entries: dict[str, MemoryEntry] = {}
        self._lock = asyncio.Lock()

    async def create_or_get_run(
        self, proposed: MemoryProjectionRun
    ) -> tuple[MemoryProjectionRun, bool]:
        async with self._lock:
            existing = self._runs.get(proposed.turn_id)
            if existing is not None:
                if (
                    existing.room_id != proposed.room_id
                    or existing.source_fingerprint != proposed.source_fingerprint
                    or existing.projection_version != proposed.projection_version
                ):
                    raise ContractError("同一 Turn 已存在不同 Memory 投影来源")
                return existing.model_copy(deep=True), False
            self._runs[proposed.turn_id] = proposed.model_copy(deep=True)
            return proposed.model_copy(deep=True), True

    async def get_run(self, turn_id: str) -> MemoryProjectionRun | None:
        run = self._runs.get(turn_id)
        return run.model_copy(deep=True) if run is not None else None

    async def claim_run(
        self,
        *,
        turn_id: str,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MemoryProjectionRun:
        async with self._lock:
            current = self._required_run(turn_id)
            claimable = current.status in {"pending", "retryable_failure"} or (
                current.status == "leased"
                and current.lease_expires_at is not None
                and current.lease_expires_at <= now
            )
            if not claimable or current.next_attempt_at > now:
                raise ContractError("Memory Projection Run 当前不可领取")
            updated = MemoryProjectionRun.model_validate(
                {
                    **current.model_dump(),
                    "status": "leased",
                    "version": current.version + 1,
                    "lease_owner": worker_id,
                    "lease_expires_at": lease_expires_at,
                    "updated_at": now,
                    "last_error_code": None,
                }
            )
            self._runs[turn_id] = updated
            return updated.model_copy(deep=True)

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
        async with self._lock:
            current = self._require_owned_lease(turn_id, worker_id, expected_version)
            self._validate_entries(current, entries)
            staged = dict(self._entries)
            for entry in entries:
                existing = staged.get(entry.memory_id)
                if existing is not None and existing != entry:
                    raise ContractError("稳定 Memory ID 已对应不同内容")
                staged[entry.memory_id] = entry.model_copy(deep=True)
            for previous_id, replacement_id in supersessions:
                previous = staged.get(previous_id)
                replacement = staged.get(replacement_id)
                if previous is None or replacement is None:
                    raise ContractError("Memory supersede 引用了不存在的记录")
                if (
                    previous.room_id != current.room_id
                    or replacement.room_id != current.room_id
                ):
                    raise ContractError("Memory supersede 不得跨房间")
                staged[previous_id] = previous.model_copy(
                    update={"superseded_by": replacement_id}
                )
            updated = MemoryProjectionRun.model_validate(
                {
                    **current.model_dump(),
                    "status": "completed",
                    "version": current.version + 1,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "updated_at": now,
                    "completed_at": now,
                }
            )
            self._entries = staged
            self._runs[turn_id] = updated
            return updated.model_copy(deep=True)

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
        async with self._lock:
            current = self._require_owned_lease(turn_id, worker_id, expected_version)
            status = "retryable_failure" if retryable else "dead_letter"
            updated = MemoryProjectionRun.model_validate(
                {
                    **current.model_dump(),
                    "status": status,
                    "version": current.version + 1,
                    "attempt_count": current.attempt_count + 1,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "next_attempt_at": next_attempt_at,
                    "last_error_code": error_code,
                    "updated_at": now,
                    "completed_at": now if status == "dead_letter" else None,
                }
            )
            self._runs[turn_id] = updated
            return updated.model_copy(deep=True)

    async def list_due_runs(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[MemoryProjectionRun, ...]:
        if limit < 1:
            raise ContractError("Memory Projection limit 必须大于 0")
        due = [
            run
            for run in self._runs.values()
            if (
                run.status in {"pending", "retryable_failure"}
                and run.next_attempt_at <= now
            )
            or (
                run.status == "leased"
                and run.lease_expires_at is not None
                and run.lease_expires_at <= now
            )
        ]
        due.sort(key=lambda item: (item.next_attempt_at, item.created_at, item.turn_id))
        return tuple(item.model_copy(deep=True) for item in due[:limit])

    async def read_context(
        self,
        *,
        scope: MemoryReadScope,
        query: MemoryQuery,
        budget: MemoryBudget,
    ) -> MemoryContext:
        query_text = _normalize(query.text) if query.text is not None else None
        candidates = [
            entry
            for entry in self._entries.values()
            if entry.room_id == scope.room_id
            and (
                entry.visibility == "public"
                or entry.viewer_player_id == scope.viewer_player_id
            )
            and (
                entry.scope != "player" or entry.scope_owner_id == scope.viewer_actor_id
            )
            and (
                entry.scope != "entity"
                or entry.scope_owner_id in scope.visible_entity_ids
            )
            and (query.include_superseded or entry.superseded_by is None)
            and (not query.kinds or entry.kind in query.kinds)
            and (not query.subject_ids or entry.subject_id in query.subject_ids)
            and (not query.location_ids or entry.location_id in query.location_ids)
            and (query_text is None or query_text in _normalize(entry.search_text))
        ]
        candidates.sort(
            key=lambda item: (
                item.subject_id not in scope.visible_entity_ids,
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
            if (
                len(selected) >= budget.max_entries
                or used_chars + chars > budget.max_chars
            ):
                continue
            selected.append(entry)
            used_chars += chars
        return MemoryContext(
            room_id=scope.room_id,
            viewer_player_id=scope.viewer_player_id,
            viewer_actor_id=scope.viewer_actor_id,
            as_of_revision=scope.as_of_revision,
            entries=tuple(item.model_copy(deep=True) for item in selected),
            truncated_count=len(candidates) - len(selected),
        )

    async def reset_room(self, room_id: str) -> None:
        async with self._lock:
            self._entries = {
                memory_id: entry
                for memory_id, entry in self._entries.items()
                if entry.room_id != room_id
            }
            self._runs = {
                turn_id: run
                for turn_id, run in self._runs.items()
                if run.room_id != room_id
            }

    def _required_run(self, turn_id: str) -> MemoryProjectionRun:
        try:
            return self._runs[turn_id]
        except KeyError as exc:
            raise ContractError("Memory Projection Run 不存在") from exc

    def _require_owned_lease(
        self,
        turn_id: str,
        worker_id: str,
        expected_version: int,
    ) -> MemoryProjectionRun:
        current = self._required_run(turn_id)
        if (
            current.status != "leased"
            or current.lease_owner != worker_id
            or current.version != expected_version
        ):
            raise ContractError("Memory Projection Run lease 或版本不匹配")
        return current

    @staticmethod
    def _validate_entries(
        run: MemoryProjectionRun,
        entries: tuple[MemoryEntry, ...],
    ) -> None:
        ids = [entry.memory_id for entry in entries]
        if len(ids) != len(set(ids)):
            raise ContractError("同一投影批次不得包含重复 Memory ID")
        if any(
            entry.room_id != run.room_id or entry.source_turn_id != run.turn_id
            for entry in entries
        ):
            raise ContractError("Memory Entry 不属于当前 Projection Run")
