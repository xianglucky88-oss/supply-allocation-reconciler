"""Idempotent, recoverable and incremental recomputation.

Write-ahead journal semantics
-----------------------------
Every recomputation moves through three persisted phases::

    projected -> solved -> committed

* ``projected``  -- input fingerprint + parsed snapshot are durable;
* ``solved``     -- the full plan dict and its content hash are durable;
* ``committed``  -- terminal marker; the plan is the answer.

A crash (simulated by the ``fail_after`` hook) can be retried with the same
``journal_id``: identical input fingerprints return the stored plan without
re-solving, and an interrupted run resumes at the next phase.  No allocation
is ever applied twice.

Incremental recomputation
-------------------------
When only a subset of the timeline changes, an *impact set* is derived from
the previous plan (orders touching changed batches/shipments/plants, the
cancelled order, and every previously unmet order).  Unimpacted allocation
lines are frozen and their supply reserved; only the impact set is re-solved.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .engine import SolveInput, solve, token_id_of
from .errors import InterruptedRecompute, JournalCorrupted
from .models import (
    AllocationLine,
    AllocationPlan,
    CommitmentState,
    RuleVersion,
    StrategyMetrics,
)
from .timeline import Event, Projection

PHASE_PROJECTED = "projected"
PHASE_SOLVED = "solved"
PHASE_COMMITTED = "committed"
PHASES = (PHASE_PROJECTED, PHASE_SOLVED, PHASE_COMMITTED)


def canonical_fingerprint(payload: dict, rule_version: str,
                          strategy: str) -> str:
    body = json.dumps(
        {"payload": payload, "rule_version": rule_version,
         "strategy": strategy},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def plan_fingerprint(plan: AllocationPlan) -> str:
    # The fingerprint covers plan content only; transient run markers
    # (journal id, resume flag) must not change the identity of the answer.
    doc = plan.to_dict()
    doc.pop("resumed", None)
    doc.pop("journal_id", None)
    body = json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Journal storage
# ---------------------------------------------------------------------------
@dataclass
class JournalEntry:
    journal_id: str
    fingerprint: str
    phase: str
    rule_version: str
    strategy: str
    plan: Optional[Dict[str, Any]] = None
    result_hash: Optional[str] = None


class JournalStore:
    """Process-local WAL; swap in :class:`FileJournalStore` for durability."""

    def __init__(self) -> None:
        self._entries: Dict[str, JournalEntry] = {}

    def save(self, entry: JournalEntry) -> None:
        self._entries[journal_key(entry.journal_id, entry.fingerprint)] = entry

    def load(self, journal_id: str, fingerprint: str) -> Optional[JournalEntry]:
        return self._entries.get(journal_key(journal_id, fingerprint))

    def ids(self) -> List[str]:
        return sorted({e.journal_id for e in self._entries.values()})


def journal_key(journal_id: str, fingerprint: str) -> str:
    return f"{journal_id}:{fingerprint[:12]}"


class FileJournalStore(JournalStore):
    """JSON-backed WAL; every phase transition is an atomic file replace."""

    def __init__(self, directory: str) -> None:
        super().__init__()
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self.directory, f"{key}.json")

    def save(self, entry: JournalEntry) -> None:
        super().save(entry)
        key = journal_key(entry.journal_id, entry.fingerprint)
        data = {
            "journal_id": entry.journal_id,
            "fingerprint": entry.fingerprint,
            "phase": entry.phase,
            "rule_version": entry.rule_version,
            "strategy": entry.strategy,
            "plan": entry.plan,
            "result_hash": entry.result_hash,
        }
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True)
            os.replace(tmp, self._path(key))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def load(self, journal_id: str,
             fingerprint: str) -> Optional[JournalEntry]:
        key = journal_key(journal_id, fingerprint)
        path = self._path(key)
        if not os.path.exists(path):
            return super().load(journal_id, fingerprint)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            entry = JournalEntry(**data)
        except (TypeError, ValueError, OSError) as exc:
            raise JournalCorrupted(f"journal {key} unreadable: {exc}") from exc
        if entry.phase not in PHASES:
            raise JournalCorrupted(f"journal {key}: bad phase {entry.phase!r}")
        if entry.phase in (PHASE_SOLVED, PHASE_COMMITTED):
            if not entry.plan or not entry.result_hash:
                raise JournalCorrupted(f"journal {key}: missing plan/hash")
        return entry


# ---------------------------------------------------------------------------
# Full recomputation with crash/resume semantics
# ---------------------------------------------------------------------------
def recompute(
    *,
    journal_id: str,
    payload: dict,
    build_input: Callable[[], SolveInput],
    rule_version: str,
    strategy: str,
    store: Optional[JournalStore] = None,
    fail_after: Optional[str] = None,
) -> AllocationPlan:
    """Idempotent full recompute.

    ``build_input`` parses and projects the payload lazily so a retried run
    that can be answered from the journal never re-parses.
    """
    store = store or JournalStore()
    fingerprint = canonical_fingerprint(payload, rule_version, strategy)
    entry = store.load(journal_id, fingerprint)

    if entry is not None and entry.phase == PHASE_COMMITTED:
        plan = _plan_from_entry(entry)
        return replace(plan, journal_id=journal_id, resumed=True)

    # A fresh fingerprint means a brand-new run, not a resume.
    started_from = entry.phase if entry is not None else None

    # Phase 1: projection -------------------------------------------------
    if entry is None or entry.phase not in PHASES:
        inp = build_input()
        store.save(
            JournalEntry(
                journal_id=journal_id, fingerprint=fingerprint,
                phase=PHASE_PROJECTED, rule_version=rule_version,
                strategy=strategy,
            )
        )
        entry = store.load(journal_id, fingerprint)
    else:
        inp = build_input()
    if fail_after == PHASE_PROJECTED:
        raise InterruptedRecompute(
            f"journal {journal_id}: crash injected after {PHASE_PROJECTED}"
        )

    # Phase 2: solve ------------------------------------------------------
    if entry.phase == PHASE_PROJECTED:
        plan = solve(inp, journal_id=journal_id)
        store.save(
            JournalEntry(
                journal_id=journal_id, fingerprint=fingerprint,
                phase=PHASE_SOLVED, rule_version=rule_version,
                strategy=strategy, plan=plan.to_dict(),
                result_hash=plan_fingerprint(plan),
            )
        )
        entry = store.load(journal_id, fingerprint)
    if fail_after == PHASE_SOLVED:
        raise InterruptedRecompute(
            f"journal {journal_id}: crash injected after {PHASE_SOLVED}"
        )

    # Phase 3: commit -----------------------------------------------------
    if entry.phase == PHASE_SOLVED:
        plan = _plan_from_entry(entry)
        _verify_hash(entry, plan)
        store.save(
            JournalEntry(
                journal_id=journal_id, fingerprint=fingerprint,
                phase=PHASE_COMMITTED, rule_version=rule_version,
                strategy=strategy, plan=entry.plan,
                result_hash=entry.result_hash,
            )
        )
    final_entry = store.load(journal_id, fingerprint)
    plan = _plan_from_entry(final_entry)
    _verify_hash(final_entry, plan)
    was_resume = started_from in (PHASE_PROJECTED, PHASE_SOLVED)
    return replace(plan, journal_id=journal_id, resumed=was_resume)


def _verify_hash(entry: JournalEntry, plan: AllocationPlan) -> None:
    if plan_fingerprint(plan) != entry.result_hash:
        raise JournalCorrupted(
            f"journal {entry.journal_id}: plan content hash mismatch"
        )


def _plan_from_entry(entry: JournalEntry) -> AllocationPlan:
    from .pipeline import plan_from_dict
    if entry.plan is None:
        raise JournalCorrupted(
            f"journal {entry.journal_id}: no persisted plan"
        )
    return plan_from_dict(entry.plan)


# ---------------------------------------------------------------------------
# Impact set for incremental recomputation
# ---------------------------------------------------------------------------
SUPPLY_EVENT_TYPES = frozenset(
    {
        "shipment.partial_arrival",
        "shipment.delayed",
        "shipment.cancelled",
        "batch.expired",
        "batch.adjusted",
        "plant.offline",
        "plant.online",
    }
)


def impact_set(
    prior: AllocationPlan,
    prior_projection: Projection,
    new_events: Sequence[Event],
    new_projection: Projection,
) -> Set[str]:
    impacted: Set[str] = set()

    token_owners: Dict[str, Set[str]] = {}
    for line in prior.lines:
        token_owners.setdefault(token_id_of(line.batch_id), set()).add(line.order)

    for event in new_events:
        payload = event.payload
        etype = event.type
        if etype == "demand.cancelled":
            impacted.add(payload["order"])
            # A cancellation releases every token the order held; all
            # previously short orders must compete for the released supply.
            for summary in prior.orders:
                if summary.requested - summary.allocated > 0:
                    impacted.add(summary.order)
            continue
        if etype.startswith("shipment."):
            token_id = payload["shipment_id"]
            impacted.update(token_owners.get(token_id, set()))
        elif etype.startswith("batch."):
            token_id = payload["batch_id"]
            impacted.update(token_owners.get(token_id, set()))
        elif etype.startswith("plant."):
            plant = payload["plant"]
            for line in prior.lines:
                if line.plant == plant:
                    impacted.add(line.order)
            for demand in new_projection.demands.values():
                if plant in demand.plant_pref:
                    impacted.add(demand.order)

    # A shrinking token must reopen every owner even if the event mapping
    # above missed it (defensive: batch.adjusted on a shared batch).
    prior_tokens = {t.token_id: t.qty for t in prior_projection.tokens()}
    for token in new_projection.tokens():
        if token.qty < prior_tokens.get(token.token_id, 0):
            impacted.update(token_owners.get(token.token_id, set()))

    # Every previously unmet order competes again whenever supply changes.
    if any(e.type in SUPPLY_EVENT_TYPES for e in new_events):
        for summary in prior.orders:
            if summary.requested - summary.allocated > 0:
                impacted.add(summary.order)

    # Only orders that still exist and are not cancelled remain solvable;
    # cancelled orders stay in the set so their release is explained.
    return {o for o in impacted if o in new_projection.demands}


# ---------------------------------------------------------------------------
# Incremental recompute
# ---------------------------------------------------------------------------
def incremental_recompute(
    *,
    prior: AllocationPlan,
    prior_projection: Projection,
    full_projection: Projection,
    new_events: Sequence[Event],
    rules: RuleVersion,
    base_strategy: str = "hierarchical",
) -> Tuple[AllocationPlan, Set[str]]:
    """Re-solve only the impact set; freeze every other allocation line.

    ``full_projection`` is the projection of base state folded with the
    complete (prior + new) event log; ``prior_projection`` is the projection
    the prior plan was built from.  Both are produced by :func:`timeline.project`,
    so this function never re-implements the event fold.
    """
    impacted = impact_set(prior, prior_projection, new_events, full_projection)

    frozen_lines = [
        line for line in prior.lines if line.order not in impacted
    ]
    # Guard: frozen quantities must still exist; otherwise reopen the order.
    token_totals = {t.token_id: t.qty for t in full_projection.tokens()}
    frozen_usage: Dict[str, int] = {}
    for line in frozen_lines:
        token_id = token_id_of(line.batch_id)
        frozen_usage[token_id] = frozen_usage.get(token_id, 0) + line.qty
    reopened: Set[str] = set()
    for token_id, used in frozen_usage.items():
        if used > token_totals.get(token_id, 0):
            owners = {line.order for line in frozen_lines
                      if token_id_of(line.batch_id) == token_id}
            reopened.update(owners)
    impacted |= reopened
    frozen_lines = [
        line for line in frozen_lines if line.order not in reopened
    ]

    sub_demands = {
        order: demand
        for order, demand in full_projection.demands.items()
        if order in impacted
    }
    sub_commitments = {
        order: commitment
        for order, commitment in full_projection.commitments.items()
        if order in impacted
    }
    # Freeze the capacity consumed by untouched orders so the sub-solve
    # cannot double-allocate a token.
    capacity_override = {t.token_id: t.qty for t in full_projection.tokens()}
    for token_id, used in frozen_usage.items():
        capacity_override[token_id] = (
            capacity_override.get(token_id, 0) - used
        )
    sub_projection = replace(
        full_projection,
        demands=sub_demands,
        commitments=sub_commitments,
        token_capacity_override=capacity_override,
    )

    sub_plan = solve(
        SolveInput(
            projection=sub_projection, rules=rules, strategy=base_strategy
        )
    )

    merged_lines = list(frozen_lines) + list(sub_plan.lines)
    remaining = {t.token_id: t.qty for t in full_projection.tokens()}
    for line in merged_lines:
        token_id = token_id_of(line.batch_id)
        remaining[token_id] = remaining.get(token_id, 0) - line.qty
        if remaining[token_id] < 0:  # pragma: no cover - guarded above
            raise JournalCorrupted(
                f"incremental merge over-allocated token {token_id}"
            )

    metrics = StrategyMetrics(
        strategy=f"incremental:{base_strategy}",
        demand_count=len(sub_demands),
        token_count=len(full_projection.tokens()),
        layers_opened=sub_plan.metrics.layers_opened,
        solver_iterations=sub_plan.metrics.solver_iterations,
        comparisons=sub_plan.metrics.comparisons,
        big_o=(
            "O(I x S x L) with I=impacted orders; full solve is O(D x S x L); "
            "journal replay is O(1)"
        ),
    )
    # Re-assemble over the full projection so summaries cover every order.
    from .engine import _assemble  # avoid circular import at module load

    eligible = {summary.order: True for summary in prior.orders
                if summary.allocated > 0 and summary.order not in impacted}
    for summary in sub_plan.orders:
        eligible[summary.order] = summary.allocated > 0
    audit = {
        order: affected.state.value
        for order, affected in
        ((a.order, a) for a in sub_plan.affected_commitments)
    }
    for affected in sub_plan.affected_commitments:
        if affected.state == CommitmentState.REVOKED:
            audit[affected.order + ":reason"] = affected.reason
    protected_by_order: Dict[str, List[AllocationLine]] = {}
    for line in merged_lines:
        if line.commitment_state == CommitmentState.HONOURED:
            protected_by_order.setdefault(line.order, []).append(line)

    merged = _assemble(
        SolveInput(projection=full_projection, rules=rules,
                   strategy=metrics.strategy),
        merged_lines, remaining, eligible, metrics, audit,
        protected_by_order,
    )
    return merged, impacted
