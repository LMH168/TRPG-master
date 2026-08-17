"""将已提交 DomainEvent 编译为 Narrator 可消费的玩家安全证据。

本模块是普通裁决、ActionPlan 与 RuleAgenda 的唯一证据入口。它只读取固定
ModuleVersion、提交前后 GameState 和最终 PlayerView，不解释玩家文本，也不把
keeper-only 事件、内部 ID 或任意状态路径交给 Narrator。
"""

from __future__ import annotations

import json

from collaboration_framework.contracts import (
    NarrationEvidence,
    ProjectionEntity,
    ProjectionKnownInformation,
)

from .models import DomainEvent, EngineRuntimeSnapshot, GameState
from .persistent_results import public_state_change_description
from .projection_v3 import project_v3


class EvidenceAssembler:
    """按权威事件顺序生成玩家安全叙事证据。"""

    @classmethod
    def from_committed_events(
        cls,
        runtime: EngineRuntimeSnapshot,
        *,
        final_state: GameState,
        events: tuple[DomainEvent, ...],
        player_id: str,
        actor_id: str,
    ) -> tuple[NarrationEvidence, ...]:
        """编译本次提交产生的公开事件，保持事件顺序且不扩大可见性。"""

        if not runtime.is_v3:
            return ()
        before_entities: dict[str, ProjectionEntity] = {}
        final_entities: dict[str, ProjectionEntity] = {}
        final_information: dict[str, ProjectionKnownInformation] = {}
        requires_projection = any(
            event.visibility == "public"
            and event.type in {"entity.state_changed", "information.revealed"}
            for event in events
        )
        if requires_projection:
            # 仅依赖最终可见性的事件需要投影前后 PlayerView；
            # 时间、旅行和 Presentation 等受控 payload 直接编译。
            final_runtime = runtime.model_copy(
                update={
                    "game_state": final_state,
                    "revision": str(final_state.event_sequence),
                },
                deep=True,
            )
            before_view = project_v3(runtime, player_id=player_id, actor_id=actor_id)
            final_view = project_v3(
                final_runtime, player_id=player_id, actor_id=actor_id
            )
            before_entities = {
                item.id: item for item in before_view.scene.visible_entities
            }
            final_entities = {
                item.id: item for item in final_view.scene.visible_entities
            }
            final_information = {item.id: item for item in final_view.known_information}
        evidence: list[NarrationEvidence] = []
        discovered_ids: set[str] = set()

        for event in events:
            if event.visibility != "public":
                continue
            item = cls._from_event(
                event,
                runtime=runtime,
                final_state=final_state,
                final_entities=final_entities,
                final_information=final_information,
            )
            if item is not None:
                evidence.append(item)

            # 可见性可能由 identified、found 等任意模组状态触发。这里比较最终
            # PlayerView，而不是维护状态键词表，从而适用于解析出的其他模组。
            entity_id = event.payload.get("entity_id")
            if (
                event.type == "entity.state_changed"
                and isinstance(entity_id, str)
                and entity_id not in before_entities
                and entity_id in final_entities
                and entity_id not in discovered_ids
            ):
                entity = final_entities[entity_id]
                evidence.append(
                    NarrationEvidence(
                        ref=event.event_id,
                        kind="entity_discovered",
                        subject_id=entity.id,
                        subject_name=entity.name,
                        subject_aliases=entity.aliases,
                        description=entity.description,
                        required_in_narration=True,
                    )
                )
                discovered_ids.add(entity_id)
        return tuple(evidence)

    @classmethod
    def _from_event(
        cls,
        event: DomainEvent,
        *,
        runtime: EngineRuntimeSnapshot,
        final_state: GameState,
        final_entities: dict[str, ProjectionEntity],
        final_information: dict[str, ProjectionKnownInformation],
    ) -> NarrationEvidence | None:
        """转换单个已确认公开事件；无法安全命名的事件不产生证据。"""

        payload = event.payload
        if event.type == "information.revealed":
            information_id = payload.get("information_id")
            information = (
                final_information.get(information_id)
                if isinstance(information_id, str)
                else None
            )
            if information is not None:
                return NarrationEvidence(
                    ref=event.event_id,
                    kind="information_revealed",
                    subject_id=information.id,
                    subject_name=information.title,
                    description=information.content,
                    required_in_narration=True,
                )
            return None
        if event.type == "entity.state_changed":
            entity_id = payload.get("entity_id")
            key = payload.get("key")
            entity = (
                final_entities.get(entity_id) if isinstance(entity_id, str) else None
            )
            observable = next(
                (
                    state
                    for state in getattr(entity, "observable_state", ())
                    if state.key == key
                ),
                None,
            )
            if entity is not None and observable is not None:
                return NarrationEvidence(
                    ref=event.event_id,
                    kind="entity_state_change",
                    subject_id=entity.id,
                    subject_name=entity.name,
                    subject_aliases=entity.aliases,
                    # key/value 是 Engine 协议标识，玩家安全证据必须使用统一的
                    # 自然语言句式，避免 fallback 泄露 consciousness=dead。
                    description=public_state_change_description(
                        entity.name,
                        observable.key,
                        observable.value,
                    ),
                    required_in_narration=True,
                )
            return None
        if event.type == "entity.moved":
            entity_id = payload.get("entity_id")
            entity = cls._entity_identity(entity_id, runtime, final_state)
            location_id = payload.get("location_id")
            location_name = cls._location_name(location_id, runtime, final_state)
            if (
                isinstance(entity_id, str)
                and entity is not None
                and isinstance(location_id, str)
                and location_name is not None
            ):
                name, aliases = entity
                return NarrationEvidence(
                    ref=event.event_id,
                    kind="entity_moved",
                    subject_id=entity_id,
                    subject_name=name,
                    subject_aliases=aliases,
                    description=f"{name}来到{location_name}。",
                    required_in_narration=True,
                )
            return None
        if event.type in {
            "actor.condition_applied",
            "actor.condition_expired",
            "actor.temporary_insanity",
        }:
            return cls._condition_evidence(event)
        if event.type == "time.point_entered":
            hour = payload.get("hour_of_day")
            day_index = payload.get("day_index")
            point_id = payload.get("point_id")
            if (
                isinstance(hour, int)
                and isinstance(day_index, int)
                and isinstance(point_id, str)
            ):
                hour_text = f"{hour:02d}点"
                return NarrationEvidence(
                    ref=event.event_id,
                    kind="world_time",
                    subject_id=point_id,
                    subject_name=hour_text,
                    description=f"时间推进到第{day_index + 1}天{hour_text}。",
                    required_in_narration=True,
                )
            return None
        if event.type in {"travel.resolved", "location.entered"}:
            location_id = payload.get("destination_id") or payload.get("location_id")
            location_name = cls._location_name(location_id, runtime, final_state)
            if isinstance(location_id, str) and location_name is not None:
                return NarrationEvidence(
                    ref=event.event_id,
                    kind="location_transition",
                    subject_id=location_id,
                    subject_name=location_name,
                    description=f"你来到{location_name}。",
                    required_in_narration=True,
                )
            return None
        if event.type == "travel.interrupted":
            boundary = payload.get("reached_boundary")
            if isinstance(boundary, dict):
                boundary_id = boundary.get("id")
                label = boundary.get("label")
                if isinstance(boundary_id, str) and isinstance(label, str):
                    return NarrationEvidence(
                        ref=event.event_id,
                        kind="travel_interrupted",
                        subject_id=boundary_id,
                        subject_name=label,
                        description=f"你抵达{label}，但通路仍被阻挡，尚未进入目标地点。",
                        required_in_narration=True,
                    )
            return None
        if event.type == "npc.action_opportunity":
            entity_id = payload.get("entity_id")
            entity = cls._entity_identity(entity_id, runtime, final_state)
            if isinstance(entity_id, str) and entity is not None:
                name, aliases = entity
                return NarrationEvidence(
                    ref=event.event_id,
                    kind="npc_opportunity",
                    subject_id=entity_id,
                    subject_name=name,
                    subject_aliases=aliases,
                    description=f"{name}现在就在你眼前。",
                    required_in_narration=True,
                )
            return None
        if event.type == "rule.check_resolved":
            return cls._passive_check_evidence(event)
        if event.type in {"actor.sanity_changed", "actor.mythos_changed"}:
            return cls._resource_evidence(event)
        if event.type == "rule.presentation":
            presentation_id = payload.get("presentation_id")
            summary = payload.get("player_safe_summary")
            if isinstance(presentation_id, str) and isinstance(summary, str):
                return NarrationEvidence(
                    ref=event.event_id,
                    kind="rule_presentation",
                    subject_id=presentation_id,
                    subject_name=summary,
                    description=summary,
                    required_in_narration=True,
                )
            return None
        if event.type == "plot_thread.transitioned":
            thread_id = payload.get("thread_id")
            summary = payload.get("player_safe_summary")
            if isinstance(thread_id, str) and isinstance(summary, str):
                return NarrationEvidence(
                    ref=event.event_id,
                    kind="plot_thread_transition",
                    subject_id=thread_id,
                    subject_name=summary,
                    description=summary,
                    required_in_narration=False,
                )
        return None

    @staticmethod
    def _condition_evidence(event: DomainEvent) -> NarrationEvidence | None:
        condition = event.payload.get("condition")
        event_type = (
            "actor.condition_applied"
            if event.type == "actor.temporary_insanity"
            else event.type
        )
        text = {
            ("unconscious", "actor.condition_applied"): ("失去意识", "你失去了意识。"),
            ("unconscious", "actor.condition_expired"): ("恢复意识", "你恢复了意识。"),
            ("unconscious_until_night", "actor.condition_applied"): (
                "失去意识",
                "你失去了意识。",
            ),
            ("unconscious_until_night", "actor.condition_expired"): (
                "恢复意识",
                "你恢复了意识。",
            ),
            ("temporary_insanity", "actor.condition_applied"): (
                "临时疯狂",
                "你陷入了临时疯狂。",
            ),
            ("temporary_insanity", "actor.condition_expired"): (
                "恢复清醒",
                "你从临时疯狂中恢复清醒。",
            ),
            ("detained", "actor.condition_applied"): ("受到拘留", "你受到了拘留。"),
            ("detained", "actor.condition_expired"): (
                "结束拘留",
                "你的拘留状态已经结束。",
            ),
        }.get((condition, event_type))
        if text is None:
            return None
        name, description = text
        return NarrationEvidence(
            ref=event.event_id,
            kind="actor_condition",
            subject_id=event.actor_id,
            subject_name=name,
            description=description,
            required_in_narration=True,
        )

    @staticmethod
    def _passive_check_evidence(event: DomainEvent) -> NarrationEvidence | None:
        profile_id = event.payload.get("profile_id")
        profile_name = {"coc7.sanity": "理智检定", "coc7.skill": "技能检定"}.get(
            profile_id
        )
        degree_name = {
            "critical_success": "大成功",
            "extreme_success": "极难成功",
            "hard_success": "困难成功",
            "regular_success": "成功",
            "critical": "大成功",
            "extreme": "极难成功",
            "hard": "困难成功",
            "regular": "成功",
            "failure": "失败",
            "fumble": "大失败",
        }.get(event.payload.get("degree"))
        roll = event.payload.get("roll")
        target = event.payload.get("target")
        if (
            profile_name is None
            or degree_name is None
            or not isinstance(roll, int)
            or not isinstance(target, int)
        ):
            return None
        return NarrationEvidence(
            ref=event.event_id,
            kind="passive_check",
            subject_id=str(profile_id),
            subject_name=profile_name,
            description=f"{profile_name} D100 {roll}/{target}：{degree_name}。",
            required_in_narration=True,
        )

    @staticmethod
    def _resource_evidence(event: DomainEvent) -> NarrationEvidence | None:
        before = event.payload.get("from")
        after = event.payload.get("to")
        delta_key = "loss" if event.type == "actor.sanity_changed" else "gain"
        delta = event.payload.get(delta_key)
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (before, after, delta)
        ):
            return None
        if event.type == "actor.sanity_changed":
            description = (
                f"你的理智值保持在{after}点。"
                if delta == 0
                else f"你的理智值从{before}点降至{after}点，损失{delta}点。"
            )
            name = "理智值"
        else:
            description = (
                f"你的克苏鲁神话技能从{before}点升至{after}点，增加{delta}点。"
            )
            name = "克苏鲁神话"
        return NarrationEvidence(
            ref=event.event_id,
            kind="actor_resource_change",
            subject_id=event.actor_id,
            subject_name=name,
            description=description,
            required_in_narration=True,
        )

    @staticmethod
    def _entity_identity(
        entity_id: object,
        runtime: EngineRuntimeSnapshot,
        final_state: GameState,
    ) -> tuple[str, tuple[str, ...]] | None:
        if not isinstance(entity_id, str):
            return None
        spec = next(
            (item for item in runtime.v3.entities if item.id == entity_id), None
        )
        if spec is not None and spec.visibility in {"public", "party"}:
            return spec.player_visible_name or spec.name, spec.player_visible_aliases
        payload = final_state.runtime_entities.get(entity_id)
        name = payload.get("name") if isinstance(payload, dict) else None
        return (name, ()) if isinstance(name, str) and name.strip() else None

    @staticmethod
    def _location_name(
        location_id: object,
        runtime: EngineRuntimeSnapshot,
        final_state: GameState,
    ) -> str | None:
        if not isinstance(location_id, str):
            return None
        spec = next(
            (item for item in runtime.v3.locations if item.id == location_id), None
        )
        if spec is not None:
            return spec.player_visible_name or spec.name
        payload = final_state.runtime_locations.get(location_id)
        name = payload.get("name") if isinstance(payload, dict) else None
        return name if isinstance(name, str) and name.strip() else None

    @staticmethod
    def _display_value(value: object) -> str:
        if value is True:
            return "是"
        if value is False:
            return "否"
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


__all__ = ["EvidenceAssembler"]
