"""提供仅供基线回放使用的阶段故障代理和恢复适配器。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from collaboration_framework.contracts import (
    ActionTarget,
    ProposalRef,
    SingleActionDecision,
    SingleActionProposal,
)
from collaboration_framework.host.application import ActionPlanNarrator

from .application_adapter import InMemoryRuntimeAdapter
from .contracts import BaselineFault, BaselineScenario, BaselineTurn, BaselineTurnResult


class InjectedFault(RuntimeError):
    """表示可恢复的受控故障，不携带模型正文或私人上下文。"""

    def __init__(self, point: str, timing: str) -> None:
        super().__init__(f"injected fault: {point}.{timing}")
        self.point = point
        self.timing = timing


class InjectedProcessExit(BaseException):
    """绕过应用内部 Exception 恢复逻辑，模拟提交后进程退出。"""


class FaultController:
    """按场景声明的 occurrence 精确触发一次或多次故障。"""

    def __init__(self, faults: tuple[BaselineFault, ...]) -> None:
        self._faults = faults
        self._calls: dict[tuple[str, str], int] = {}
        self.fired: list[str] = []

    def consume(self, point: str, timing: str) -> bool:
        """记录边界调用，并返回本次是否命中场景故障。"""

        key = (point, timing)
        self._calls[key] = self._calls.get(key, 0) + 1
        matched = any(
            fault.point == point and fault.timing == timing and fault.occurrence == self._calls[key]
            for fault in self._faults
        )
        if matched:
            self.fired.append(point)
        return matched

    def hit(self, point: str, timing: str) -> None:
        """在命中时抛出受控故障；process 使用 BaseException 模拟硬退出。"""

        if not self.consume(point, timing):
            return
        if point == "process":
            raise InjectedProcessExit()
        raise InjectedFault(point, timing)


class _FaultingPlanner:
    """在 Host 前后注入故障，并用非法目标覆盖 Validator 拒绝场景。"""

    def __init__(self, delegate: Any, controller: FaultController) -> None:
        self._delegate = delegate
        self._controller = controller

    async def generate(self, context):
        self._controller.hit("host", "before")
        decision = await self._delegate.generate(context)
        self._controller.hit("host", "after")
        if self._controller.consume("validator", "before"):
            if isinstance(decision, SingleActionProposal):
                return decision.model_copy(
                    update={
                        "semantic_focus": ProposalRef(kind="entity", id="missing-runtime-target")
                    },
                    deep=True,
                )
            assert isinstance(decision, SingleActionDecision)
            adjudication = decision.adjudication.model_copy(
                update={"target": ActionTarget(kind="entity", id="missing-runtime-target")},
                deep=True,
            )
            return decision.model_copy(update={"adjudication": adjudication}, deep=True)
        return decision


class _FaultingNarrationModel:
    """在 Narrator 模型边界前后注入故障。"""

    def __init__(self, delegate: Any, controller: FaultController) -> None:
        self._delegate = delegate
        self._controller = controller

    async def generate(self, context):
        self._controller.hit("narrator", "before")
        result = await self._delegate.generate(context)
        self._controller.hit("narrator", "after")
        return result


class _FaultingEngine:
    """代理 Engine 提交边界，保留 get_status 对账能力。"""

    def __init__(self, delegate: Any, controller: FaultController) -> None:
        self._delegate = delegate
        self._controller = controller

    async def submit(self, request):
        self._controller.hit("engine", "before")
        result = await self._delegate.submit(request)
        self._controller.hit("engine", "after")
        self._controller.hit("process", "after")
        return result

    async def submit_proposal(self, request):
        """代理 v2 Proposal 提交边界，故障点必须覆盖唯一生产入口。"""

        self._controller.hit("engine", "before")
        result = await self._delegate.submit_proposal(request)
        self._controller.hit("engine", "after")
        self._controller.hit("process", "after")
        return result

    async def get_status(self, request):
        return await self._delegate.get_status(request)


class FaultingDelivery:
    """模拟 WebSocket 发送边界，并记录实际完成的消息 ID。"""

    def __init__(self, controller: FaultController) -> None:
        self._controller = controller
        self.delivered: list[str] = []

    async def deliver(self, message_id: str) -> None:
        self._controller.hit("websocket", "before")
        if message_id not in self.delivered:
            self.delivered.append(message_id)
        self._controller.hit("websocket", "after")


class FaultRecoveryAdapter(InMemoryRuntimeAdapter):
    """执行故障场景，并在安全边界重试或重建应用依赖。"""

    def __init__(self) -> None:
        super().__init__()
        self._controller = FaultController(())
        self._delivery = FaultingDelivery(self._controller)

    async def prepare(self, scenario: BaselineScenario) -> Mapping[str, str]:
        aliases = await super().prepare(scenario)
        self._controller = FaultController(scenario.faults)
        self._delivery = FaultingDelivery(self._controller)
        self._install_fault_wrappers()
        return aliases

    def _install_fault_wrappers(self) -> None:
        """将故障代理安装在当前重建出的 Application 上。"""

        if self._application is None or self._adjudication_engine is None:
            raise RuntimeError("adapter 尚未 prepare")
        self._application._planner = _FaultingPlanner(
            self._application._planner,
            self._controller,
        )
        self._application._narrator = ActionPlanNarrator(
            _FaultingNarrationModel(self._narration_model, self._controller)
        )
        self._application._dispatcher._executor = _FaultingEngine(
            self._adjudication_engine,
            self._controller,
        )

    async def execute_turn(
        self,
        turn: BaselineTurn,
        *,
        aliases: Mapping[str, str],
    ) -> BaselineTurnResult:
        """故障后只重试未完成阶段，并以总事件增量核对重复写入。"""

        if self._store is None:
            raise RuntimeError("adapter 尚未 prepare")
        before = len(self._store.inspect_domain_events(self.room_id))
        error_phase: str | None = None
        try:
            result = await super().execute_turn(turn, aliases=aliases)
        except InjectedProcessExit:
            error_phase = "process"
            self.rebuild_application()
            self._install_fault_wrappers()
            result = await super().execute_turn(turn, aliases=aliases)
        except Exception:
            if not self._controller.fired:
                raise
            error_phase = self._controller.fired[-1]
            result = await super().execute_turn(turn, aliases=aliases)

        if result.status == "needs_clarification" and "validator" in self._controller.fired:
            error_phase = "validator"
            result = await super().execute_turn(turn, aliases=aliases)

        try:
            await self._delivery.deliver(turn.client_action_id)
        except InjectedFault as exc:
            error_phase = exc.point
            await self._delivery.deliver(turn.client_action_id)

        if error_phase is None and self._controller.fired:
            error_phase = self._controller.fired[0]
        events = self._store.inspect_domain_events(self.room_id)[before:]
        return result.model_copy(
            update={
                "error_phase": error_phase,
                "commit_known": True,
                "event_types": tuple(event.type for event in events),
                "event_ids": tuple(
                    f"{event.client_action_id}:{event.type}:{event.sequence}" for event in events
                ),
                "state_versions": tuple(event.sequence for event in events),
            }
        )
