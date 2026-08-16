"""Deterministic game-rule execution boundary."""

from collaboration_framework.runtime_context import current_turn_id, engine_turn_context

from .adapters import InMemoryEngineStore
from .adjudication import AdjudicationEngineService
from .capabilities import (
    RuntimeCapabilityIssue,
    audit_runtime_capabilities,
    require_runtime_capabilities,
)
from .dice import DiceRoller, SequenceDiceSource, SystemDiceSource
from .initialization import create_initial_game_state
from .models import (
    ActorResources,
    ActorState,
    AgendaItem,
    AgendaSource,
    AgendaStepExecution,
    CheckRun,
    CompletedAction,
    CompletedAdjudicationCommand,
    DomainEvent,
    EngineExecutionResult,
    EngineRuntimeSnapshot,
    GameState,
    LocationKnowledge,
    PendingCheckDecision,
    PlotThreadState,
    RuleAgenda,
    StateModifiedEvent,
    ValidatedActionCommand,
    WorldTimePoint,
    WorldTimeState,
)
from .navigation import effective_location_knowledge, resolve_location_target
from .persistent_results import (
    CHARACTER_STATE_VALUES,
    OBJECT_STATE_VALUES,
    PUBLIC_STATE_KEYS,
    committed_results_from_events,
    validate_persistent_effects,
)
from .plot_threads import transition_plot_thread
from .ports import EngineStore, EngineTransaction, RevisionConflictError
from .proposal_compiler import (
    ProposalCompiler,
    ProposalShadowComparison,
    ProposalShadowCompiler,
    derive_runtime_object_id,
)
from .service import RuleEngineService

__all__ = [
    "CHARACTER_STATE_VALUES",
    "OBJECT_STATE_VALUES",
    "PUBLIC_STATE_KEYS",
    "ActorResources",
    "ActorState",
    "AdjudicationEngineService",
    "AgendaItem",
    "AgendaSource",
    "AgendaStepExecution",
    "CheckRun",
    "CompletedAction",
    "CompletedAdjudicationCommand",
    "DiceRoller",
    "DomainEvent",
    "EngineExecutionResult",
    "EngineRuntimeSnapshot",
    "EngineStore",
    "EngineTransaction",
    "GameState",
    "InMemoryEngineStore",
    "LocationKnowledge",
    "PendingCheckDecision",
    "PlotThreadState",
    "ProposalCompiler",
    "ProposalShadowComparison",
    "ProposalShadowCompiler",
    "RevisionConflictError",
    "RuleAgenda",
    "RuleEngineService",
    "RuntimeCapabilityIssue",
    "SequenceDiceSource",
    "StateModifiedEvent",
    "SystemDiceSource",
    "ValidatedActionCommand",
    "WorldTimePoint",
    "WorldTimeState",
    "audit_runtime_capabilities",
    "committed_results_from_events",
    "create_initial_game_state",
    "current_turn_id",
    "derive_runtime_object_id",
    "effective_location_knowledge",
    "engine_turn_context",
    "require_runtime_capabilities",
    "resolve_location_target",
    "transition_plot_thread",
    "validate_persistent_effects",
]
