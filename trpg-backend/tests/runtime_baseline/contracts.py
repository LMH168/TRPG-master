"""定义 AI 主持运行时基线场景、执行结果和指标的稳定数据契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCENARIO_SCHEMA_VERSION = 1


class StrictModel(BaseModel):
    """禁止未声明字段，避免场景拼写错误被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class BaselineInitialState(StrictModel):
    """描述场景需要加载的模组和逻辑实体别名。"""

    module: str = "paper-chase"
    aliases: dict[str, str] = Field(default_factory=dict)
    state: dict[str, object] = Field(default_factory=dict)


class BaselineTurn(StrictModel):
    """描述一次玩家输入及其受控模型响应。"""

    client_action_id: str = Field(min_length=1)
    utterance: str = Field(min_length=1)
    host_output: dict[str, object] | None = None
    narrator_output: dict[str, object] | None = None
    repeat: int = Field(default=1, ge=1, le=3)


class BaselineFault(StrictModel):
    """描述故障发生的阶段、提交边界和触发次数。"""

    point: str = Field(min_length=1)
    timing: Literal["before", "after"] = "before"
    occurrence: int = Field(default=1, ge=1)
    retryable: bool = True


class BaselineExpectation(StrictModel):
    """声明结构化预期；不对完整自然语言进行逐字快照。"""

    terminal_statuses: tuple[str, ...] = ("completed",)
    required_event_types: tuple[str, ...] = ()
    forbidden_event_types: tuple[str, ...] = ()
    required_state: dict[str, object] = Field(default_factory=dict)
    required_narration_evidence: tuple[str, ...] = ()
    forbidden_narration_claims: tuple[str, ...] = ()
    known_gaps: tuple[str, ...] = ()


class BaselineScenario(StrictModel):
    """一个可独立执行、可审计的基线回放场景。"""

    schema_version: int
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    category: Literal[
        "conversation",
        "rules",
        "persistent-state",
        "dynamic-content",
        "narrative-texture",
        "idempotency",
        "fault-recovery",
    ]
    description: str = Field(min_length=1)
    initial_state: BaselineInitialState = Field(default_factory=BaselineInitialState)
    turns: tuple[BaselineTurn, ...] = Field(min_length=1)
    faults: tuple[BaselineFault, ...] = ()
    expectation: BaselineExpectation
    contains_private_data: bool = False

    @model_validator(mode="after")
    def validate_supported_schema(self) -> BaselineScenario:
        """只接受当前冻结的 schema，并阻止私人数据进入回放。"""

        if self.schema_version != SCENARIO_SCHEMA_VERSION:
            raise ValueError(
                f"不支持的场景 schema_version={self.schema_version}；"
                f"当前仅支持 {SCENARIO_SCHEMA_VERSION}"
            )
        if self.contains_private_data:
            raise ValueError("基线场景必须完成脱敏，contains_private_data 不能为 true")
        return self


class BaselineTurnResult(StrictModel):
    """单次输入经过运行时后产生的规范化结构结果。"""

    client_action_id: str
    status: str
    phases: tuple[str, ...] = ()
    event_types: tuple[str, ...] = ()
    state: dict[str, object] = Field(default_factory=dict)
    narration_evidence: tuple[str, ...] = ()
    narration_claims: tuple[str, ...] = ()
    error_phase: str | None = None
    commit_known: bool = True
    roll_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    state_versions: tuple[int, ...] = ()


class BaselineResult(StrictModel):
    """场景结果；字段已排除时间戳和随机数据库标识。"""

    scenario_id: str
    category: str
    passed: bool
    turns: tuple[BaselineTurnResult, ...]
    hard_failures: tuple[str, ...] = ()
    known_gaps: tuple[str, ...] = ()


class BaselineMetrics(StrictModel):
    """用于不可恶化门槛比较的确定性指标。"""

    scenarios_total: int = 0
    scenarios_passed: int = 0
    turns_without_terminal_status: int = 0
    errors_by_phase: dict[str, int] = Field(default_factory=dict)
    duplicate_rolls: int = 0
    duplicate_events: int = 0
    duplicate_state_changes: int = 0
    state_narration_mismatches: int = 0
    unknown_commit_state: int = 0
    known_gaps: int = 0

    @property
    def success_rate(self) -> float:
        """返回稳定的场景成功率；空集合按 100% 处理。"""

        if self.scenarios_total == 0:
            return 1.0
        return self.scenarios_passed / self.scenarios_total
