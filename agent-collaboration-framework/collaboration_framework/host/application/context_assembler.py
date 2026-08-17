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
        visible_ids = visible_actor_ids | {
            entity.id for entity in player_view.scene.visible_entities
        }
        current_focus = tuple(
            dict.fromkeys(
                entity_id for entity_id in focus_entity_ids if entity_id in visible_ids
            )
        )
        if not current_focus and recent_history is not None:
            # 只有最近同场景存在唯一可见 NPC 时才继承焦点；多个参与者时保持
            # 空值，避免编译器替玩家猜测说话者或交互对象。
            for turn in reversed(recent_history.turns):
                if turn.scene_id != player_view.scene.id:
                    continue
                candidates = tuple(
                    dict.fromkeys(
                        participant
                        for participant in turn.participants
                        if participant in visible_actor_ids
                        and participant != player_view.actor_id
                    )
                )
                if len(candidates) == 1:
                    current_focus = candidates
                break

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
