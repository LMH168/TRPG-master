"""PR2 兼容导出：ActionPlan 叙事已统一由 Narrator 实现。"""

from .narrator import (
    NarrationValidationError,
    Narrator,
    unsupported_focus_shift_claim,
)

# 旧调用方暂时保留名称，但不再保留第二套校验实现；PR3 清理外部引用后删除。
ActionPlanNarrationValidationError = NarrationValidationError
ActionPlanNarrator = Narrator

__all__ = [
    "ActionPlanNarrationValidationError",
    "ActionPlanNarrator",
    "unsupported_focus_shift_claim",
]
