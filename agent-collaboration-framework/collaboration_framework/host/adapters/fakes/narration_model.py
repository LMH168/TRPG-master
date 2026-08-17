"""Deterministic no-network narration model for offline integration tests."""

from collaboration_framework.contracts import JsonObject
from collaboration_framework.host.schemas import NarrationContext


class FakeNarrationModel:
    async def generate(self, context: NarrationContext) -> JsonObject:
        """按规范 NarrationContext 生成无网络、可重复的测试叙事。"""

        if context.termination_status == "needs_clarification":
            return {
                "kind": "clarification",
                "text": context.player_safe_failure_reason
                or "我没有理解这次行动，请换一种说法。",
                "claimed_evidence_refs": [],
                "suggested_actions": [],
            }
        if context.narration_evidence:
            return {
                "kind": "narration",
                "text": " ".join(
                    item.description or item.subject_name
                    for item in context.narration_evidence
                ),
                "claimed_evidence_refs": [
                    item.ref for item in context.narration_evidence
                ],
                "suggested_actions": [],
            }
        return {
            "kind": "narration",
            "text": "这个行动已经由规则边界处理。",
            "claimed_evidence_refs": [],
            "suggested_actions": [],
        }
