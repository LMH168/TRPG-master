"""定义事件驱动跨回合记忆及投影运行状态的纯数据契约。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from collaboration_framework.contracts import ContractModel, PlayerInput, PlayerView

MEMORY_PROJECTION_VERSION = 1

MemoryKind = Literal[
    "completed_action",
    "location_visit",
    "discovered_information",
    "world_event",
    "conversation",
    "relationship_change",
    "unresolved_goal",
]
MemoryScope = Literal["campaign", "player", "entity"]
MemoryVisibility = Literal["public", "player_scoped"]
MemoryEpistemicStatus = Literal[
    "confirmed",
    "experienced",
    "heard",
    "asserted",
    "presentation_only",
]
MemorySourceKind = Literal[
    "turn",
    "game_event",
    "action_execution",
    "adjudication_execution",
    "replay_event",
]
MemoryProjectionStatus = Literal[
    "pending",
    "leased",
    "completed",
    "retryable_failure",
    "dead_letter",
]


def stable_memory_id(
    *,
    room_id: str,
    turn_id: str,
    source_kind: MemorySourceKind,
    source_id: str,
    kind: MemoryKind,
    scope: MemoryScope,
    scope_owner_id: str | None,
    ordinal: int,
    projection_version: int = MEMORY_PROJECTION_VERSION,
) -> str:
    """由稳定来源身份生成可重复计算的 SHA-256 Memory ID。"""

    payload = {
        "kind": kind,
        "ordinal": ordinal,
        "projection_version": projection_version,
        "room_id": room_id,
        "scope": scope,
        "scope_owner_id": scope_owner_id,
        "source_id": source_id,
        "source_kind": source_kind,
        "turn_id": turn_id,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MemoryBudget(ContractModel):
    """限制一次模型上下文能够读取的 Memory 数量与字符数。"""

    max_entries: int = Field(default=8, ge=1, le=32)
    max_chars: int = Field(default=4000, ge=1, le=20000)


class MemoryEntry(ContractModel):
    """一条有来源、作用域和认知等级的可重建长期记忆。"""

    schema_version: Literal[1] = 1
    projection_version: Literal[1] = MEMORY_PROJECTION_VERSION
    memory_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    room_id: str = Field(min_length=1)
    kind: MemoryKind
    subject_id: str = Field(min_length=1)
    object_id: str | None = Field(default=None, min_length=1)
    location_id: str | None = Field(default=None, min_length=1)
    source_turn_id: str = Field(min_length=1)
    source_kind: MemorySourceKind
    source_id: str = Field(min_length=1)
    source_event_id: str | None = Field(default=None, min_length=1)
    source_sequence: int | None = Field(default=None, ge=1)
    source_ordinal: int = Field(ge=0)
    scope: MemoryScope
    scope_owner_id: str | None = Field(default=None, min_length=1)
    visibility: MemoryVisibility
    viewer_player_id: str | None = Field(default=None, min_length=1)
    epistemic_status: MemoryEpistemicStatus
    topic_key: str | None = Field(default=None, min_length=1, max_length=500)
    content: dict[str, JsonValue] = Field(default_factory=dict)
    search_text: str = Field(default="", max_length=4000)
    created_at: datetime
    superseded_by: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_scope_and_source(self) -> MemoryEntry:
        """拒绝无 owner 的私有作用域以及不完整的事件来源。"""

        if self.scope == "campaign" and self.scope_owner_id is not None:
            raise ValueError("campaign Memory 不得设置 scope_owner_id")
        if self.scope != "campaign" and self.scope_owner_id is None:
            raise ValueError("player/entity Memory 必须设置 scope_owner_id")
        if self.visibility == "public" and self.viewer_player_id is not None:
            raise ValueError("public Memory 不得绑定 viewer_player_id")
        if self.visibility == "player_scoped" and self.viewer_player_id is None:
            raise ValueError("player_scoped Memory 必须绑定 viewer_player_id")
        if self.source_kind == "game_event" and (
            self.source_event_id is None or self.source_sequence is None
        ):
            raise ValueError("game_event Memory 必须记录 event id 与 sequence")
        if self.superseded_by == self.memory_id:
            raise ValueError("Memory 不得 supersede 自己")
        expected = stable_memory_id(
            room_id=self.room_id,
            turn_id=self.source_turn_id,
            source_kind=self.source_kind,
            source_id=self.source_id,
            kind=self.kind,
            scope=self.scope,
            scope_owner_id=self.scope_owner_id,
            ordinal=self.source_ordinal,
            projection_version=self.projection_version,
        )
        if self.memory_id != expected:
            raise ValueError("memory_id 与稳定来源身份不一致")
        return self


class MemoryReadScope(ContractModel):
    """由应用绑定的可信读取身份，模型不能提供这些字段。"""

    room_id: str = Field(min_length=1)
    viewer_player_id: str = Field(min_length=1)
    viewer_actor_id: str = Field(min_length=1)
    as_of_revision: str = Field(min_length=1)
    current_location_id: str | None = Field(default=None, min_length=1)
    visible_entity_ids: tuple[str, ...] = ()

    @classmethod
    def from_view(
        cls,
        *,
        player_input: PlayerInput,
        player_view: PlayerView,
    ) -> MemoryReadScope:
        """从同一玩家作用域的输入和最终视图构造可信读取范围。"""

        if (
            player_input.room_id != player_view.room_id
            or player_input.player_id != player_view.player_id
            or player_input.actor_id != player_view.actor_id
        ):
            raise ValueError("MemoryReadScope 输入与 PlayerView 作用域不一致")
        return cls(
            room_id=player_view.room_id,
            viewer_player_id=player_view.player_id,
            viewer_actor_id=player_view.actor_id,
            as_of_revision=player_view.revision,
            current_location_id=player_view.scene_id,
            visible_entity_ids=tuple(
                dict.fromkeys(
                    (
                        player_view.actor_id,
                        *(item.id for item in player_view.scene.visible_actors),
                        *(item.id for item in player_view.scene.visible_entities),
                    )
                )
            ),
        )


class MemoryQuery(ContractModel):
    """确定性 Memory 检索条件；可信身份始终由 MemoryReadScope 提供。"""

    text: str | None = Field(default=None, min_length=1, max_length=500)
    kinds: tuple[MemoryKind, ...] = ()
    subject_ids: tuple[str, ...] = ()
    location_ids: tuple[str, ...] = ()
    include_superseded: bool = False

    @model_validator(mode="after")
    def validate_unique_filters(self) -> MemoryQuery:
        for field_name in ("kinds", "subject_ids", "location_ids"):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"MemoryQuery {field_name} 不得重复")
        return self


class MemoryContext(ContractModel):
    """绑定当前 PlayerView revision 的有限玩家安全长期记忆。"""

    room_id: str = Field(min_length=1)
    viewer_player_id: str = Field(min_length=1)
    viewer_actor_id: str = Field(min_length=1)
    as_of_revision: str = Field(min_length=1)
    entries: tuple[MemoryEntry, ...] = ()
    truncated_count: int = Field(default=0, ge=0)

    @classmethod
    def empty(
        cls,
        *,
        player_input: PlayerInput,
        player_view: PlayerView,
    ) -> MemoryContext:
        scope = MemoryReadScope.from_view(
            player_input=player_input,
            player_view=player_view,
        )
        return cls(
            room_id=scope.room_id,
            viewer_player_id=scope.viewer_player_id,
            viewer_actor_id=scope.viewer_actor_id,
            as_of_revision=scope.as_of_revision,
        )

    def validate_for(
        self,
        *,
        player_input: PlayerInput,
        player_view: PlayerView,
    ) -> MemoryContext:
        scope = MemoryReadScope.from_view(
            player_input=player_input,
            player_view=player_view,
        )
        if (
            self.room_id != scope.room_id
            or self.viewer_player_id != scope.viewer_player_id
            or self.viewer_actor_id != scope.viewer_actor_id
            or self.as_of_revision != scope.as_of_revision
        ):
            raise ValueError("MemoryContext 与当前玩家作用域或 revision 不一致")
        visible_entity_ids = set(scope.visible_entity_ids)
        if any(
            entry.scope == "entity" and entry.scope_owner_id not in visible_entity_ids
            for entry in self.entries
        ):
            raise ValueError("MemoryContext 不得包含当前不可见实体的 Memory")
        return self

    @model_validator(mode="after")
    def validate_entry_visibility(self) -> MemoryContext:
        for entry in self.entries:
            if entry.room_id != self.room_id:
                raise ValueError("MemoryContext 不得包含其他房间记忆")
            if (
                entry.visibility == "player_scoped"
                and entry.viewer_player_id != self.viewer_player_id
            ):
                raise ValueError("MemoryContext 不得包含其他玩家私有记忆")
            if entry.scope == "player" and entry.scope_owner_id != self.viewer_actor_id:
                raise ValueError("MemoryContext 不得包含其他角色的 player Memory")
        return self


class MemoryProjectionRun(ContractModel):
    """一个可靠 Turn 的可恢复 Memory 投影游标。"""

    schema_version: Literal[1] = 1
    projection_version: Literal[1] = MEMORY_PROJECTION_VERSION
    turn_id: str = Field(min_length=1)
    room_id: str = Field(min_length=1)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: MemoryProjectionStatus = "pending"
    version: int = Field(default=1, ge=1)
    attempt_count: int = Field(default=0, ge=0)
    lease_owner: str | None = Field(default=None, min_length=1, max_length=200)
    lease_expires_at: datetime | None = None
    next_attempt_at: datetime
    last_error_code: str | None = Field(default=None, min_length=1, max_length=100)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_state(self) -> MemoryProjectionRun:
        leased = self.status == "leased"
        if leased != (self.lease_owner is not None):
            raise ValueError("只有 leased Projection Run 可以持有 lease_owner")
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("Projection Run lease 必须完整存在或为空")
        if self.status in {"completed", "dead_letter"}:
            if self.completed_at is None:
                raise ValueError("终态 Projection Run 必须记录 completed_at")
        elif self.completed_at is not None:
            raise ValueError("非终态 Projection Run 不得记录 completed_at")
        if self.status in {"retryable_failure", "dead_letter"} and self.last_error_code is None:
            raise ValueError("失败 Projection Run 必须记录稳定错误码")
        return self


def new_memory_projection_run(
    *,
    turn_id: str,
    room_id: str,
    source_fingerprint: str,
    now: datetime,
) -> MemoryProjectionRun:
    """为一个可靠 Turn 建立尚未处理的初始投影游标。"""

    return MemoryProjectionRun(
        turn_id=turn_id,
        room_id=room_id,
        source_fingerprint=source_fingerprint,
        next_attempt_at=now,
        created_at=now,
        updated_at=now,
    )
