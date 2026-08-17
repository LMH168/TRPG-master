"""剧情线程权威状态机：集中校验合法迁移并生成不可变的新状态。"""

from __future__ import annotations

from collaboration_framework.contracts import (
    ContractError,
    ModuleContentV3,
    NarrationPlotThread,
    PlotThreadSpec,
    PlotThreadStatus,
)

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

_PLAYER_SAFE_STATUS_TEXT = {
    "available": "现已可以继续调查",
    "in_progress": "调查正在推进",
    "resolved": "这一阶段已经解决",
    "failed": "这一阶段未能继续",
}


def player_safe_plot_thread_summary(
    spec: PlotThreadSpec,
    status: PlotThreadStatus,
) -> str:
    """把模组安全摘要与通用状态含义组合，不暴露内部条件或游标。"""

    status_text = _PLAYER_SAFE_STATUS_TEXT.get(status)
    if spec.visibility != "player" or status_text is None:
        raise ContractError("PlotThread 当前状态不可投影给玩家")
    summary = spec.player_safe_summary.rstrip("。！？!?；;，,")
    return f"{summary}；{status_text}。"


def project_narration_plot_threads(
    module: ModuleContentV3,
    state: GameState,
) -> tuple[NarrationPlotThread, ...]:
    """只投影玩家可见且已解锁的剧情线程，保持模组声明顺序。"""

    projected: list[NarrationPlotThread] = []
    for spec in module.plot_threads:
        current = state.plot_threads.get(spec.id)
        if spec.visibility != "player" or current is None or current.status == "locked":
            continue
        projected.append(
            NarrationPlotThread(
                thread_id=spec.id,
                status=current.status,
                player_safe_summary=player_safe_plot_thread_summary(
                    spec, current.status
                ),
                last_transition_event_ref=current.last_transition_event_id,
            )
        )
    return tuple(projected)


def transition_plot_thread(
    module: ModuleContentV3,
    state: GameState,
    *,
    thread_id: str,
    to_status: PlotThreadStatus,
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


__all__ = [
    "player_safe_plot_thread_summary",
    "project_narration_plot_threads",
    "transition_plot_thread",
]
