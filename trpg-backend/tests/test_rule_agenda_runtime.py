"""RuleAgenda 后台恢复必须复用 TurnCoordinator，且单个失败不能阻塞整批。"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.service.rule_agenda_runtime import RuleAgendaSupervisor


class _AgendaSource:
    async def list_recoverable_rule_agenda_bindings(
        self,
        *,
        now: datetime,
        limit: int = 20,
    ) -> tuple[tuple[str, str], ...]:
        del now, limit
        return (("room-1", "turn-bad"), ("room-2", "turn-good"))


@pytest.mark.asyncio
async def test_supervisor_resumes_each_turn_and_isolates_failure() -> None:
    """恢复器只接收 turn_id，不获得直接 Engine gameplay 写入口。"""

    resumed: list[str] = []

    async def resume(turn_id: str) -> object:
        resumed.append(turn_id)
        if turn_id == "turn-bad":
            raise RuntimeError("injected")
        return object()

    supervisor = RuleAgendaSupervisor(source=_AgendaSource(), resume=resume)
    await supervisor.run_once()

    assert resumed == ["turn-bad", "turn-good"]
