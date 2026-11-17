"""Versioned, explainable supply allocation with recoverable recomputation."""
from .engine import SolveInput, solve
from .errors import (
    AllocationError,
    InterruptedRecompute,
    UnknownRuleVersionError,
    ValidationError,
)
from .models import (
    AllocationLine,
    AllocationPlan,
    AllocationStatus,
    CommitmentState,
    Layer,
    RunStatus,
    UnmetReason,
)
from .pipeline import (
    compare_strategies,
    envelope,
    failure_envelope,
    plan_from_dict,
    replay,
    run,
    run_canonical,
    run_incremental,
    run_journaled,
    safe_run,
)
from .recovery import FileJournalStore, JournalStore
from .rules import RuleStore
from .timeline import Event, project

__all__ = [
    "AllocationError",
    "AllocationLine",
    "AllocationPlan",
    "AllocationStatus",
    "CommitmentState",
    "Event",
    "FileJournalStore",
    "InterruptedRecompute",
    "JournalStore",
    "Layer",
    "RuleStore",
    "RunStatus",
    "SolveInput",
    "UnmetReason",
    "UnknownRuleVersionError",
    "ValidationError",
    "compare_strategies",
    "envelope",
    "failure_envelope",
    "plan_from_dict",
    "project",
    "replay",
    "run",
    "run_canonical",
    "run_incremental",
    "run_journaled",
    "safe_run",
    "solve",
]
