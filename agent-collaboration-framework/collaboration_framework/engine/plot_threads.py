"""剧情线程权威状态机：集中校验合法迁移并生成不可变的新状态。"""

from __future__ import annotations

from collaboration_framework.contracts import ContractError, ModuleContentV3

from .models import GameState, PlotThreadState

_ALLOWED_TRANSITIONS = frozenset(
    {
        ("locked", "available"),
        ("available", "in_progress"),
        ("available", "failed"),
        ("in_progress", "resolved"),
        ("in_progress", "failed"),
    }
)


def transition_plot_thread(
    module: ModuleContentV3,
    state: GameState,
    *,
    thread_id: str,
    to_status: str,
    event_id: str,
) -> PlotThreadState:
    """执行一次幂等迁移，并在解锁前强制检查全部剧情依赖。"""

    spec = next((item for item in module.plot_threads if item.id == thread_id), None)
    current = state.plot_threads.get(thread_id)
    if spec is None or current is None:
        raise ContractError(f"PlotThread 不存在: {thread_id}")
    if current.status == to_status and current.last_transition_event_id == event_id:
        return current
    if (
        current.status == "locked"
        and to_status == "available"
        and not all(
            (dependency := state.plot_threads.get(dependency_id)) is not None
            and dependency.status == "resolved"
            for dependency_id in spec.dependency_thread_ids
        )
    ):
        raise ContractError(f"PlotThread 依赖尚未完成: {current.thread_id}")
    if (current.status, to_status) not in _ALLOWED_TRANSITIONS:
        raise ContractError(
            f"PlotThread 非法迁移: {current.thread_id} {current.status} -> {to_status}"
        )
    return current.model_copy(
        update={
            "status": to_status,
            "version": current.version + 1,
            "last_transition_event_id": event_id,
        },
        deep=True,
    )


__all__ = ["transition_plot_thread"]
