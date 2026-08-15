"""在一次异步回合任务内传播跨层的服务端 ``turn_id``。

该中立上下文供 Host 与 Engine 共同读取，只负责调用链身份传播；权威提交结果仍
以数据库中的 execution、DomainEvent 与 receipt 为准，不能把 ContextVar 当作
恢复证据。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


_CURRENT_TURN_ID: ContextVar[str | None] = ContextVar("runtime_turn_id", default=None)


def current_turn_id() -> str | None:
    """返回当前异步任务绑定的回合身份；旧执行链返回 ``None``。"""

    return _CURRENT_TURN_ID.get()


@contextmanager
def engine_turn_context(turn_id: str) -> Iterator[None]:
    """在 Host、ActionPlan 与 Engine 的整条调用链中绑定同一个 ``turn_id``。"""

    if not turn_id.strip():
        raise ValueError("turn_id 不能为空")
    token = _CURRENT_TURN_ID.set(turn_id)
    try:
        yield
    finally:
        _CURRENT_TURN_ID.reset(token)


__all__ = ["current_turn_id", "engine_turn_context"]
