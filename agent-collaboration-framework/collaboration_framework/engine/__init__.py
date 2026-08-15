"""Deterministic game-rule execution boundary."""

# ruff: noqa: F401 -- this module intentionally re-exports the public engine API.

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
    CheckRun,
    CompletedAction,
    CompletedAdjudicationCommand,
    DomainEvent,
    EngineExecutionResult,
    EngineRuntimeSnapshot,
    GameState,
    LocationKnowledge,
    PendingCheckDecision,
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
from .ports import EngineStore, EngineTransaction, RevisionConflictError
from .proposal_compiler import ProposalCompiler
from .service import RuleEngineService

__all__ = [
    "AgendaItem",
    "AgendaSource",
    "CompletedAction",
    "CompletedAdjudicationCommand",
    "ActorResources",
    "ActorState",
    "LocationKnowledge",
    "AdjudicationEngineService",
    "WorldTimePoint",
    "WorldTimeState",
    "CheckRun",
    "DiceRoller",
    "DomainEvent",
    "EngineExecutionResult",
    "EngineRuntimeSnapshot",
    "EngineStore",
    "EngineTransaction",
    "GameState",
    "InMemoryEngineStore",
    "PendingCheckDecision",
    "ProposalCompiler",
    "RuleAgenda",
    "RevisionConflictError",
    "RuntimeCapabilityIssue",
    "RuleEngineService",
    "SequenceDiceSource",
    "StateModifiedEvent",
    "SystemDiceSource",
    "ValidatedActionCommand",
    "audit_runtime_capabilities",
    "create_initial_game_state",
    "effective_location_knowledge",
    "require_runtime_capabilities",
    "resolve_location_target",
    "CHARACTER_STATE_VALUES",
    "OBJECT_STATE_VALUES",
    "PUBLIC_STATE_KEYS",
    "committed_results_from_events",
    "validate_persistent_effects",
    "current_turn_id",
    "engine_turn_context",
]
