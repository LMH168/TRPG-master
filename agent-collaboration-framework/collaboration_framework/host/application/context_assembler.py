"""Pure assembly of model-visible contexts from already safe contracts."""

from typing import Literal

from collaboration_framework.contracts import (
    NarrationPlotThread,
    PlayerInput,
    PlayerView,
    WorldClockView,
)
from collaboration_framework.host.schemas import (
    CompletedPlanStepSummary,
    IntentContext,
    NarrationContext,
    OpeningNarrationContext,
    OpeningParticipant,
    OpeningSceneContext,
    RecentTurnContext,
)
from collaboration_framework.memory import MemoryContext

_NON_INTERACTIVE_CONSCIOUSNESS = frozenset({"dead", "unconscious"})


def _visible_npc_is_interactive(entity: object) -> bool:
    """只在最终 PlayerView 未证明 NPC 失去交互能力时延续对话。"""

    observable_state = getattr(entity, "observable_state", ())
    return not any(
        state.key == "consciousness" and state.value in _NON_INTERACTIVE_CONSCIOUSNESS
        for state in observable_state
    )


class ContextAssembler:
    """Build minimal model inputs from player-safe views and completed results."""

    def for_opening(self, player_view: PlayerView) -> OpeningNarrationContext:
        """Expose public scene/participant data, plus solo-only self background."""

        participants = (
            OpeningParticipant(
                actor_id=player_view.self_actor.id,
                name=player_view.self_actor.name,
                occupation=player_view.self_actor.occupation,
                status_summary=player_view.self_actor.public_status_summary,
            ),
            *(
                OpeningParticipant(
                    actor_id=actor.id,
                    name=actor.name,
                    occupation=actor.occupation,
                    status_summary=actor.status_summary,
                )
                for actor in player_view.scene.visible_actors
            ),
        )
        return OpeningNarrationContext(
            background=player_view.background,
            scene=OpeningSceneContext(
                id=player_view.scene.id,
                name=player_view.scene.name,
                description=player_view.scene.description,
                time=player_view.scene.time,
                narrative_details=player_view.scene.narrative_details,
            ),
            world_time=WorldClockView.from_world(player_view.world),
            participants=participants,
            solo_background_summary=(
                player_view.self_actor.background_summary
                if len(participants) == 1
                else ""
            ),
        )

    def for_intent(
        self,
        player_input: PlayerInput,
        player_view: PlayerView,
        recent_history: RecentTurnContext,
    ) -> IntentContext:
        """Bind the real player action to its safe view and bounded history."""

        return IntentContext(
            player_input=player_input,
            player_view=player_view,
            recent_history=recent_history,
        )

    def for_narration(
        self,
        *,
        player_input: PlayerInput,
        plan_goal: str,
        termination_status: Literal[
            "resolved",
            "needs_clarification",
            "cancelled",
            "stopped",
        ],
        completed_steps: tuple[CompletedPlanStepSummary, ...],
        player_view: PlayerView,
        recent_history: RecentTurnContext | None = None,
        memory_context: MemoryContext | None = None,
        focus_entity_ids: tuple[str, ...] = (),
        plan_id: str | None = None,
        opening_world_time: WorldClockView | None = None,
        blocked_step_goal: str | None = None,
        remaining_step_goals: tuple[str, ...] = (),
        player_safe_failure_reason: str | None = None,
        narration_retry_hint: str | None = None,
        plot_threads: tuple[NarrationPlotThread, ...] = (),
    ) -> NarrationContext:
        """从最终安全视图和已提交步骤构造统一 NarrationContext。"""

        visible_actor_ids = {actor.id for actor in player_view.scene.visible_actors}
        visible_npc_ids = visible_actor_ids | {
            entity.id
            for entity in player_view.scene.visible_entities
            if entity.kind == "npc" and _visible_npc_is_interactive(entity)
        }
        visible_ids = visible_actor_ids | {
            entity.id for entity in player_view.scene.visible_entities
        }
        current_focus = tuple(
            dict.fromkeys(
                entity_id for entity_id in focus_entity_ids if entity_id in visible_ids
            )
        )
        previous_interaction: tuple[str, ...] = ()
        interaction_source_turn_id: str | None = None
        if recent_history is not None:
            # 只有最近同场景存在唯一可见 NPC 时才继承焦点；多个参与者时保持
            # 空值。纯澄清失败轮没有接受语义，不应清空上一轮可靠交互。
            for turn in reversed(recent_history.turns):
                if turn.scene_id != player_view.scene.id:
                    continue
                candidates = tuple(
                    dict.fromkeys(
                        participant
                        for participant in turn.participants
                        if participant in visible_npc_ids
                        and participant != player_view.actor_id
                    )
                )
                if len(candidates) == 1:
                    previous_interaction = candidates
                    interaction_source_turn_id = turn.correlation_id
                    break
                non_player_participants = tuple(
                    item for item in turn.participants if item != player_view.actor_id
                )
                if non_player_participants or turn.accepted_intent_summary is not None:
                    break
        if not current_focus and previous_interaction:
            current_focus = previous_interaction

        active_interaction = tuple(
            entity_id for entity_id in current_focus if entity_id in visible_npc_ids
        )
        if active_interaction and active_interaction == previous_interaction:
            interaction_continuity = "continued"
        elif active_interaction:
            interaction_continuity = "current"
            interaction_source_turn_id = None
        else:
            interaction_continuity = "none"
            interaction_source_turn_id = None

        return NarrationContext(
            background=player_view.background,
            player_input=player_input,
            plan_id=plan_id,
            plan_goal=plan_goal,
            termination_status=termination_status,
            completed_steps=completed_steps,
            player_view=player_view,
            recent_history=recent_history,
            memory_context=(
                memory_context
                if memory_context is not None
                else MemoryContext.empty(
                    player_input=player_input,
                    player_view=player_view,
                )
            ),
            focus_entity_ids=current_focus,
            active_interaction_entity_ids=active_interaction,
            interaction_source_turn_id=interaction_source_turn_id,
            interaction_continuity=interaction_continuity,
            opening_world_time=opening_world_time,
            allowed_evidence_refs=tuple(
                ref for step in completed_steps for ref in step.event_refs
            ),
            narration_evidence=tuple(
                item for step in completed_steps for item in step.narration_evidence
            ),
            blocked_step_goal=blocked_step_goal,
            remaining_step_goals=remaining_step_goals,
            player_safe_failure_reason=player_safe_failure_reason,
            narration_retry_hint=narration_retry_hint,
            plot_threads=plot_threads,
        )
