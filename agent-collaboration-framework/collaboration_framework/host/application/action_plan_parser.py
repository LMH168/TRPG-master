"""Deterministic validation for untrusted HostTurnDecision JSON."""

from __future__ import annotations

from pydantic import TypeAdapter, ValidationError

from collaboration_framework.contracts import (
    ActionPlan,
    ActionPlanPolicy,
    ContractError,
    HostTurnDecision,
)

_DECISION_ADAPTER = TypeAdapter(HostTurnDecision)


class HostTurnDecisionParser:
    @staticmethod
    def parse(
        raw: object,
        *,
        policy: ActionPlanPolicy | None = None,
    ) -> HostTurnDecision:
        try:
            decision = _DECISION_ADAPTER.validate_python(raw)
        except (ValidationError, TypeError, ValueError) as exc:
            # 只保留字段路径与错误类型，便于按定位号诊断且不泄露模型正文。
            if isinstance(exc, ValidationError):
                issues = exc.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
                detail = "; ".join(
                    f"{'.'.join(str(part) for part in issue.get('loc', ()))}:"
                    f"{issue.get('type', 'unknown')}"
                    for issue in issues
                )[:512]
            else:
                detail = type(exc).__name__
            raise ContractError(f"HostTurnDecision 未通过结构校验 ({detail})") from exc
        if isinstance(decision, ActionPlan):
            (policy or ActionPlanPolicy()).require_plan(decision)
        return decision
