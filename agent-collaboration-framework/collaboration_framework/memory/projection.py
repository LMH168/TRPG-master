"""将可靠回合、权威事件与玩家安全交流确定性投影为长期记忆。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from collaboration_framework.contracts import ContractModel

from .contracts import MemoryEntry, stable_memory_id


class MemoryProjectionEvent(ContractModel):
    """投影器可读取的玩家安全 DomainEvent 子集。"""

    event_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=100)
    actor_id: str = Field(min_length=1)
    visibility: Literal["public", "private", "hidden"]
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime


class MemoryProjectionStep(ContractModel):
    """由持久化 ActionPlan 或 adjudication execution 提取的步骤证据。"""

    source_id: str = Field(min_length=1, max_length=200)
    semantic_goal: str = Field(min_length=1, max_length=2000)
    status: Literal["completed", "stopped", "cancelled"]
    outcome: Literal["success", "failure", "cancelled", "legacy_unknown"]
    goal_outcome: Literal[
        "achieved",
        "partially_achieved",
        "not_achieved",
        "cancelled",
        "legacy_unknown",
    ]
    has_receipt: bool
    target_interaction: str | None = Field(default=None, max_length=100)
    focus_kind: Literal["actor", "entity", "location", "information", "none"] = "none"
    focus_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_focus(self) -> MemoryProjectionStep:
        if (self.focus_kind == "none") != (self.focus_id is None):
            raise ValueError("Memory 投影步骤的 focus kind/id 必须完整")
        return self


class MemoryProjectionNarration(ContractModel):
    """已经通过证据校验并写入回放表的玩家安全叙事。"""

    source_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=4000)
    visibility: Literal["public", "player_scoped"]
    viewer_player_id: str | None = Field(default=None, min_length=1)
    scene_id: str | None = Field(default=None, min_length=1)
    created_at: datetime

    @model_validator(mode="after")
    def validate_visibility(self) -> MemoryProjectionNarration:
        if self.visibility == "player_scoped" and self.viewer_player_id is None:
            raise ValueError("player_scoped 叙事必须绑定 viewer")
        if self.visibility == "public" and self.viewer_player_id is not None:
            raise ValueError("public 叙事不得绑定 viewer")
        return self


class MemoryProjectionSource(ContractModel):
    """一个终态 Turn 的不可变、可重建投影来源快照。"""

    turn_id: str = Field(min_length=1)
    room_id: str = Field(min_length=1)
    player_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    utterance: str = Field(min_length=1, max_length=2000)
    turn_status: Literal["completed", "failed", "cancelled"]
    commit_state: Literal["not_committed", "partially_committed", "committed"]
    receipt_ids: tuple[str, ...] = ()
    steps: tuple[MemoryProjectionStep, ...] = ()
    events: tuple[MemoryProjectionEvent, ...] = ()
    narration: MemoryProjectionNarration | None = None
    created_at: datetime
    completed_at: datetime

    @model_validator(mode="after")
    def validate_stable_order(self) -> MemoryProjectionSource:
        if len(self.receipt_ids) != len(set(self.receipt_ids)):
            raise ValueError("Memory 投影来源不得包含重复 receipt")
        if tuple(sorted(self.events, key=lambda item: item.sequence)) != self.events:
            raise ValueError("Memory 投影事件必须按 sequence 排序")
        return self

    def fingerprint(self) -> str:
        """对全部可靠来源生成稳定摘要，恢复时用于拒绝来源漂移。"""

        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def is_reliably_projectable(self) -> bool:
        """无 receipt 的成功历史不能证明执行结果，只允许失败目标进入投影。"""

        return bool(self.receipt_ids) or self.turn_status in {"failed", "cancelled"}


def project_memory_entries(source: MemoryProjectionSource) -> tuple[MemoryEntry, ...]:
    """只从 receipt、公开事件和明确 social 参与者生成 Memory。"""

    entries: list[MemoryEntry] = []
    ordinal = 0

    def append(
        *,
        source_kind: Literal[
            "turn",
            "game_event",
            "action_execution",
            "adjudication_execution",
            "replay_event",
        ],
        source_id: str,
        kind: Literal[
            "completed_action",
            "location_visit",
            "discovered_information",
            "world_event",
            "conversation",
            "relationship_change",
            "unresolved_goal",
        ],
        subject_id: str,
        scope: Literal["campaign", "player", "entity"],
        scope_owner_id: str | None,
        visibility: Literal["public", "player_scoped"],
        epistemic_status: Literal[
            "confirmed", "experienced", "heard", "asserted", "presentation_only"
        ],
        content: dict[str, JsonValue],
        search_text: str,
        created_at: datetime,
        object_id: str | None = None,
        location_id: str | None = None,
        topic_key: str | None = None,
        source_event_id: str | None = None,
        source_sequence: int | None = None,
        viewer_player_id: str | None = None,
    ) -> None:
        nonlocal ordinal
        entries.append(
            MemoryEntry(
                memory_id=stable_memory_id(
                    room_id=source.room_id,
                    turn_id=source.turn_id,
                    source_kind=source_kind,
                    source_id=source_id,
                    kind=kind,
                    scope=scope,
                    scope_owner_id=scope_owner_id,
                    ordinal=ordinal,
                ),
                room_id=source.room_id,
                kind=kind,
                subject_id=subject_id,
                object_id=object_id,
                location_id=location_id,
                source_turn_id=source.turn_id,
                source_kind=source_kind,
                source_id=source_id,
                source_event_id=source_event_id,
                source_sequence=source_sequence,
                source_ordinal=ordinal,
                scope=scope,
                scope_owner_id=scope_owner_id,
                visibility=visibility,
                viewer_player_id=viewer_player_id,
                epistemic_status=epistemic_status,
                topic_key=topic_key,
                content=content,
                search_text=search_text,
                created_at=created_at,
            )
        )
        ordinal += 1

    committed_steps = tuple(step for step in source.steps if step.has_receipt)
    for step in committed_steps:
        append(
            source_kind="adjudication_execution",
            source_id=step.source_id,
            kind="completed_action",
            subject_id=source.actor_id,
            scope="player",
            scope_owner_id=source.actor_id,
            visibility="player_scoped",
            viewer_player_id=source.player_id,
            epistemic_status="experienced",
            content={
                "summary": step.semantic_goal,
                "outcome": step.outcome,
                "goal_outcome": step.goal_outcome,
            },
            search_text=step.semantic_goal,
            created_at=source.completed_at,
            object_id=step.focus_id,
            topic_key=f"action:{step.source_id}",
        )

    # 对话只认 Validator 已接受、且 receipt 证明已执行的 social Proposal。
    social_steps = tuple(
        step
        for step in committed_steps
        if step.target_interaction == "social"
        and step.focus_kind == "entity"
        and step.focus_id is not None
    )
    for step in social_steps:
        target_id = step.focus_id
        assert target_id is not None
        topic = source.utterance if len(source.steps) == 1 else step.semantic_goal
        append(
            source_kind="adjudication_execution",
            source_id=step.source_id,
            kind="conversation",
            subject_id=source.actor_id,
            object_id=target_id,
            scope="player",
            scope_owner_id=source.actor_id,
            visibility="player_scoped",
            viewer_player_id=source.player_id,
            epistemic_status="asserted",
            content={"summary": topic, "listener_id": target_id},
            search_text=topic,
            created_at=source.completed_at,
            topic_key=f"conversation:{target_id}",
        )
        append(
            source_kind="adjudication_execution",
            source_id=step.source_id,
            kind="conversation",
            subject_id=target_id,
            object_id=source.actor_id,
            scope="entity",
            scope_owner_id=target_id,
            visibility="player_scoped",
            viewer_player_id=source.player_id,
            epistemic_status="heard",
            content={"summary": topic, "speaker_id": source.actor_id},
            search_text=topic,
            created_at=source.completed_at,
            topic_key=f"conversation:{source.actor_id}",
        )
        if source.narration is not None:
            append(
                source_kind="replay_event",
                source_id=source.narration.source_id,
                kind="conversation",
                subject_id=target_id,
                object_id=source.actor_id,
                scope="player",
                scope_owner_id=source.actor_id,
                visibility="player_scoped",
                viewer_player_id=source.player_id,
                epistemic_status="presentation_only",
                content={"summary": source.narration.text},
                search_text=source.narration.text,
                created_at=source.narration.created_at,
                location_id=source.narration.scene_id,
                topic_key=f"presentation:{target_id}",
            )

    for event in source.events:
        # private 事件没有可验证的 viewer，hidden 更不能进入玩家安全 Memory。
        if event.visibility != "public":
            continue
        payload = event.payload
        if event.event_type in {"location.entered", "travel.resolved"}:
            location_id = _string(payload, "location_id") or _string(
                payload, "destination_id"
            )
            if location_id is None:
                continue
            append(
                source_kind="game_event",
                source_id=event.event_id,
                source_event_id=event.event_id,
                source_sequence=event.sequence,
                kind="location_visit",
                subject_id=source.actor_id,
                object_id=location_id,
                location_id=location_id,
                scope="player",
                scope_owner_id=source.actor_id,
                visibility="player_scoped",
                viewer_player_id=source.player_id,
                epistemic_status="experienced",
                content={"location_id": location_id},
                search_text=location_id,
                created_at=event.created_at,
                topic_key=f"location:{location_id}",
            )
        elif event.event_type == "information.revealed":
            information_id = _string(payload, "information_id")
            if information_id is None:
                continue
            append(
                source_kind="game_event",
                source_id=event.event_id,
                source_event_id=event.event_id,
                source_sequence=event.sequence,
                kind="discovered_information",
                subject_id=source.actor_id,
                object_id=information_id,
                scope="campaign",
                scope_owner_id=None,
                visibility="public",
                epistemic_status="confirmed",
                content={"information_id": information_id},
                search_text=information_id,
                created_at=event.created_at,
                topic_key=f"information:{information_id}",
            )
        elif event.event_type == "relationship.changed":
            entity_id = _string(payload, "entity_id")
            if entity_id is None:
                continue
            append(
                source_kind="game_event",
                source_id=event.event_id,
                source_event_id=event.event_id,
                source_sequence=event.sequence,
                kind="relationship_change",
                subject_id=entity_id,
                object_id=source.actor_id,
                scope="entity",
                scope_owner_id=entity_id,
                visibility="public",
                epistemic_status="experienced",
                content={"change": payload},
                search_text=f"{entity_id} relationship",
                created_at=event.created_at,
                topic_key=f"relationship:{source.actor_id}",
            )
        elif event.event_type == "plot_thread.transitioned":
            thread_id = _string(payload, "thread_id")
            summary = _string(payload, "player_safe_summary")
            to_status = _string(payload, "to_status")
            if thread_id is None or summary is None or to_status is None:
                continue
            # 只有 Engine 已标记为 public 的安全摘要会到达这里；Memory 不保存
            # 隐藏条件、Rule ID 或 Agenda 游标，也不能反向推进 PlotThread。
            append(
                source_kind="game_event",
                source_id=event.event_id,
                source_event_id=event.event_id,
                source_sequence=event.sequence,
                kind="world_event",
                subject_id=thread_id,
                scope="campaign",
                scope_owner_id=None,
                visibility="public",
                epistemic_status="confirmed",
                content={
                    "event_type": event.event_type,
                    "thread_id": thread_id,
                    "status": to_status,
                    "summary": summary,
                },
                search_text=summary,
                created_at=event.created_at,
                topic_key=f"plot_thread:{thread_id}",
            )
        elif event.event_type.startswith(("world.", "time.")):
            append(
                source_kind="game_event",
                source_id=event.event_id,
                source_event_id=event.event_id,
                source_sequence=event.sequence,
                kind="world_event",
                subject_id=event.actor_id,
                scope="campaign",
                scope_owner_id=None,
                visibility="public",
                epistemic_status="confirmed",
                content={"event_type": event.event_type, "detail": payload},
                search_text=event.event_type,
                created_at=event.created_at,
                topic_key=event.event_type,
            )

    if not source.receipt_ids and source.turn_status in {"failed", "cancelled"}:
        append(
            source_kind="turn",
            source_id=source.turn_id,
            kind="unresolved_goal",
            subject_id=source.actor_id,
            scope="player",
            scope_owner_id=source.actor_id,
            visibility="player_scoped",
            viewer_player_id=source.player_id,
            epistemic_status="asserted",
            content={"summary": source.utterance, "turn_status": source.turn_status},
            search_text=source.utterance,
            created_at=source.completed_at,
            topic_key=f"unresolved:{source.turn_id}",
        )

    return tuple(entries)


def _string(payload: dict[str, JsonValue], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None
