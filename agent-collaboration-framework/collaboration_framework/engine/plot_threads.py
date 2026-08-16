"""剧情线程权威状态机：集中校验合法迁移并生成不可变的新状态。"""

from __future__ import annotations

from collaboration_framework.contracts import ContractError

from .models import PlotThreadState

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
    current: PlotThreadState,
    *,
    to_status: str,
    event_id: str,
) -> PlotThreadState:
    """执行一次幂等迁移；相同事件重放返回原状态，其他非法迁移直接拒绝。"""

    if current.status == to_status and current.last_transition_event_id == event_id:
        return current
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
