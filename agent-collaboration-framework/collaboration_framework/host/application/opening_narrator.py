"""Validation and deterministic fallback for the authoritative game opening."""

from __future__ import annotations

from typing import Literal

from collaboration_framework.contracts import ContractError
from collaboration_framework.host.ports import OpeningNarrationModelPort
from collaboration_framework.host.schemas import (
    OpeningNarrationContext,
    OpeningNarrationOutput,
)

from .narrator import narration_text_rejection_reason, normalize_narration_text

OpeningRejectionReason = Literal[
    "outer_schema",
    "opening_contract",
    "participant_coverage",
    "protocol_tail",
    "schema_fragment",
    "world_time_conflict",
    "unsupported_dialogue",
]


class OpeningNarrationValidationError(ContractError):
    """Stable rejection category for an unsafe or malformed opening candidate."""

    def __init__(self, reason: OpeningRejectionReason) -> None:
        super().__init__("Opening NarrationOutput 未通过玩家可见输出安全校验")
        self.reason = reason


class OpeningNarrator:
    """Validate an untrusted provider candidate before it becomes player-visible."""

    def __init__(self, model: OpeningNarrationModelPort) -> None:
        self._model = model

    async def narrate(self, context: OpeningNarrationContext) -> OpeningNarrationOutput:
        """Require a narration-only result that names every public participant."""

        raw = await self._model.generate(context)
        if isinstance(raw, dict) and isinstance(raw.get("text"), str):
            raw = {**raw, "text": normalize_narration_text(raw["text"])}
        try:
            output = OpeningNarrationOutput.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise OpeningNarrationValidationError("outer_schema") from exc
        if (
            output.kind != "narration"
            or output.claimed_fact_ids
            or output.suggested_actions
        ):
            raise OpeningNarrationValidationError("opening_contract")
        rejection_reason = narration_text_rejection_reason(output.text)
        if rejection_reason is not None:
            raise OpeningNarrationValidationError(rejection_reason)
        if any(
            participant.name not in output.text for participant in context.participants
        ):
            raise OpeningNarrationValidationError("participant_coverage")
        if _opening_time_conflicts(output.text, context):
            raise OpeningNarrationValidationError("world_time_conflict")
        if any(mark in output.text for mark in ("“", "”", "「", "」", "『", "』")):
            # 开场 Context 没有任何已发生对白；允许模型补写台词就等于允许它借
            # NPC 之口创造信件、钥匙和线索。真实对话必须留给可靠 Turn。
            raise OpeningNarrationValidationError("unsupported_dialogue")
        return output


def deterministic_opening_narration(
    context: OpeningNarrationContext,
) -> OpeningNarrationOutput:
    """Build a public, deterministic opening without exposing private actor data."""

    clock = context.world_time
    clock_label = (
        f"第{clock.day_index + 1}天{clock.hour_of_day:02d}:00"
        f"（{'白天' if clock.time_of_day == 'day' else '夜晚'}）"
    )
    scene_lines = [clock_label]
    scene_lines.extend(
        line
        for line in (context.scene.name.strip(), context.scene.description.strip())
        if line
    )
    participant_labels = []
    for participant in context.participants:
        public_details = [
            detail
            for detail in (participant.occupation, participant.status_summary)
            if detail and detail.strip()
        ]
        label = participant.name
        if public_details:
            label = f"{label}（{'，'.join(public_details)}）"
        participant_labels.append(label)
    if len(participant_labels) == 1:
        scene_lines.append(f"{participant_labels[0]}此刻就在这里。")
    else:
        scene_lines.append(f"共同在场的调查员有：{'、'.join(participant_labels)}。")
    return OpeningNarrationOutput(
        kind="narration",
        text="\n".join(scene_lines),
        claimed_fact_ids=(),
        suggested_actions=(),
    )


def _opening_time_conflicts(
    text: str,
    context: OpeningNarrationContext,
) -> bool:
    """拒绝与权威时钟矛盾的中文时段声明；背景风格不能覆盖当前状态。"""

    clock = context.world_time
    required_clock = f"{clock.hour_of_day:02d}:00"
    if required_clock not in text:
        return True
    night_terms = ("夜幕", "夜色", "夜晚", "深夜", "月光", "星光")
    day_terms = ("白昼", "白天", "日光", "阳光")
    if clock.time_of_day == "day" and any(term in text for term in night_terms):
        return True
    if clock.time_of_day == "night" and any(term in text for term in day_terms):
        return True
    hour_terms = {
        "清晨": range(5, 9),
        "早晨": range(5, 11),
        "上午": range(6, 12),
        "正午": range(11, 14),
        "中午": range(11, 14),
        "午后": range(13, 18),
        "下午": range(13, 18),
        "黄昏": range(17, 20),
        "傍晚": range(17, 21),
    }
    return any(
        term in text and clock.hour_of_day not in hours
        for term, hours in hour_terms.items()
    )
