"""Orchestration boundary.

The pipeline wires parsing, the versioned rule store, the timeline fold and
the engine together, and defines the failure/partial-success boundary:

* malformed input / unknown rule version / corrupted journal
  -> ``status == "failed"`` envelope, no allocation plan;
* interruptions raised by the injected crash hook propagate
  :class:`InterruptedRecompute` and are resumed on retry;
* feasible-but-short domain outcomes
  -> ``status == "partial"`` with a complete, conservation-checked plan.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .engine import SolveInput, calculate, solve
from .errors import AllocationError
from .models import (
    AffectedCommitment,
    AllocationLine,
    AllocationPlan,
    AllocationStatus,
    CommitmentState,
    Layer,
    OrderSummary,
    RuleVersion,
    RunStatus,
    StrategyMetrics,
    UnmetLine,
    UnmetReason,
)
from .parser import is_legacy, load_canonical, upgrade_legacy
from .recovery import (
    JournalStore,
    incremental_recompute,
    recompute,
)
from .rules import RuleStore
from .timeline import Event, project


# ---------------------------------------------------------------------------
# Construction helpers
# ---------------------------------------------------------------------------
def build_store_and_input(payload: dict,
                          rule_version: Optional[str] = None,
                          strategy: Optional[str] = None
                          ) -> Tuple[RuleStore, SolveInput, str, str]:
    canonical = upgrade_legacy(payload) if is_legacy(payload) else payload
    parsed = load_canonical(canonical)
    store = RuleStore.from_payload(canonical)
    if rule_version is None:
        rule_version = parsed["rule_version"]
    if rule_version is None:
        rule_version = store.latest().version if store.versions() else "default"
    if not store.versions():
        store.publish(RuleVersion(version="default"))
    rules = store.get(rule_version)
    strategy = strategy or parsed["strategy"]

    proj = project(
        as_of=parsed["as_of"],
        batches=parsed["batches"],
        shipments=parsed["shipments"],
        demands=parsed["demands"],
        events=parsed["events"],
        substitutions=rules.substitutions,
        commitments=parsed["commitments"],
        plants_offline=parsed["plants_offline"],
    )
    return store, SolveInput(projection=proj, rules=rules,
                             strategy=strategy), rule_version, strategy


# ---------------------------------------------------------------------------
# Plain canonical run (no journal)
# ---------------------------------------------------------------------------
def run_canonical(payload: dict, *, strategy: Optional[str] = None,
                  rule_version: Optional[str] = None) -> AllocationPlan:
    _, inp, _, _ = build_store_and_input(payload, rule_version, strategy)
    return solve(inp)


def run(payload: dict) -> dict:
    """Top-level entry point used by the CLI and the original test surface."""
    if is_legacy(payload):
        rows = payload.get("demands", [])
        data = calculate(rows, payload)
        from .report import build_result
        return build_result("supply-allocation-reconciler", data)
    plan = run_canonical(payload)
    return envelope(plan)


def envelope(plan: AllocationPlan) -> dict:
    return {
        "project": "supply-allocation-reconciler",
        **plan.to_dict(),
    }


def failure_envelope(exc: Exception) -> dict:
    return {
        "project": "supply-allocation-reconciler",
        "status": RunStatus.FAILED.value,
        "error": {
            "type": type(exc).__name__,
            "message": str(exc),
        },
    }


def safe_run(payload: dict) -> dict:
    """Entry point that converts contract failures into ``failed`` envelopes."""
    try:
        return run(payload)
    except AllocationError as exc:
        return failure_envelope(exc)


# ---------------------------------------------------------------------------
# Journaled, replayable recomputation
# ---------------------------------------------------------------------------
def run_journaled(payload: dict, *, journal_id: str,
                  store: Optional[JournalStore] = None,
                  strategy: Optional[str] = None,
                  rule_version: Optional[str] = None,
                  fail_after: Optional[str] = None) -> AllocationPlan:
    # Resolve the concrete rule version and strategy up front so the journal
    # fingerprint is stable; the (re)build closure re-parses lazily on resume.
    _, _first, rv, st = build_store_and_input(
        payload, rule_version, strategy
    )

    def build_input() -> SolveInput:
        _, inp, _, _ = build_store_and_input(payload, rv, st)
        return inp

    return recompute(
        journal_id=journal_id,
        payload=payload,
        build_input=build_input,
        rule_version=rv,
        strategy=st,
        store=store,
        fail_after=fail_after,
    )


def replay(payload: dict, *, rule_version: str,
           strategy: str = "hierarchical") -> AllocationPlan:
    """Deterministic replay under an arbitrary historical rule version."""
    return run_canonical(
        payload, strategy=strategy, rule_version=rule_version
    )


# ---------------------------------------------------------------------------
# Incremental recomputation entry point
# ---------------------------------------------------------------------------
def run_incremental(payload: dict, *, prior: AllocationPlan,
                    new_events: Sequence[Event],
                    rule_version: Optional[str] = None
                    ) -> Tuple[AllocationPlan, set]:
    """Incrementally recompute from ``prior`` after ``new_events`` arrive.

    Both projections are built from the *unfolded* parsed entities so events
    are folded exactly once: the prior projection folds every event except
    ``new_events`` (collapsing duplicates), the full projection folds all.
    """
    canonical = upgrade_legacy(payload) if is_legacy(payload) else payload
    parsed = load_canonical(canonical)
    store = RuleStore.from_payload(canonical)
    rv = rule_version or parsed["rule_version"] or (
        store.latest().version if store.versions() else "default"
    )
    if not store.versions():
        store.publish(RuleVersion(version="default"))
    rules = store.get(rv)

    prior_events = _prior_events(canonical, new_events)
    common = dict(
        as_of=parsed["as_of"],
        batches=parsed["batches"],
        shipments=parsed["shipments"],
        demands=parsed["demands"],
        substitutions=rules.substitutions,
        commitments=parsed["commitments"],
        plants_offline=parsed["plants_offline"],
    )
    prior_proj = project(events=prior_events, **common)
    full_proj = project(events=parsed["events"], **common)

    return incremental_recompute(
        prior=prior,
        prior_projection=prior_proj,
        full_projection=full_proj,
        new_events=new_events,
        rules=rules,
    )


def _prior_events(payload: dict, new_events: Sequence[Event]) -> List[Event]:
    new_ids = {e.event_id for e in new_events}
    out: List[Event] = []
    seen: set = set()
    for row in payload.get("events", []):
        eid = str(row["event_id"])
        if eid in new_ids or eid in seen:
            # Exclude new events and collapse duplicates exactly like project().
            continue
        seen.add(eid)
        out.append(
            Event(event_id=eid, type=str(row["type"]),
                  occurred_at=int(row["occurred_at"]),
                  payload=dict(row.get("payload", {})))
        )
    return out


# ---------------------------------------------------------------------------
# Strategy comparison
# ---------------------------------------------------------------------------
def compare_strategies(payload: dict) -> Dict[str, Any]:
    """Run greedy, hierarchical and simulated incremental recompute."""
    canonical = upgrade_legacy(payload) if is_legacy(payload) else payload
    parsed = load_canonical(canonical)
    store = RuleStore.from_payload(canonical)
    rv = parsed["rule_version"] or (
        store.latest().version if store.versions() else "default"
    )
    if not store.versions():
        store.publish(RuleVersion(version="default"))
    rules = store.get(rv)
    all_events = parsed["events"]

    base_proj = project(
        as_of=parsed["as_of"],
        batches=parsed["batches"],
        shipments=parsed["shipments"],
        demands=parsed["demands"],
        events=[],
        substitutions=rules.substitutions,
        commitments=parsed["commitments"],
        plants_offline=parsed["plants_offline"],
    )
    full_proj = project(
        as_of=parsed["as_of"],
        batches=parsed["batches"],
        shipments=parsed["shipments"],
        demands=parsed["demands"],
        events=all_events,
        substitutions=rules.substitutions,
        commitments=parsed["commitments"],
        plants_offline=parsed["plants_offline"],
    )

    greedy_plan = solve(SolveInput(full_proj, rules, "greedy"))
    hier_plan = solve(SolveInput(full_proj, rules, "hierarchical"))

    prior_plan = solve(SolveInput(base_proj, rules, "hierarchical"))
    inc_plan, impacted = incremental_recompute(
        prior=prior_plan,
        prior_projection=base_proj,
        full_projection=full_proj,
        new_events=all_events,
        rules=rules,
    )

    def summary(plan: AllocationPlan) -> Dict[str, Any]:
        return {
            "metrics": plan.metrics.to_dict(),
            "conservation": plan.conservation,
            "total_allocated": plan.conservation["total_allocated"],
            "status": plan.status.value,
        }

    return {
        "rule_version": rv,
        "greedy": summary(greedy_plan),
        "hierarchical": summary(hier_plan),
        "incremental": {
            **summary(inc_plan),
            "impacted_orders": sorted(impacted),
            "impacted_count": len(impacted),
            "full_demand_count": len(full_proj.active_demands()),
        },
        "same_total_allocation": (
            greedy_plan.conservation["total_allocated"]
            == hier_plan.conservation["total_allocated"]
            == inc_plan.conservation["total_allocated"]
        ),
        "all_conservation_checked": all(
            plan.conservation["supply_balanced"]
            and plan.conservation["demand_balanced"]
            and plan.conservation["no_over_allocation"]
            for plan in (greedy_plan, hier_plan, inc_plan)
        ),
    }


# ---------------------------------------------------------------------------
# Plan (de)serialisation for journal resume
# ---------------------------------------------------------------------------
def plan_from_dict(data: dict) -> AllocationPlan:
    lines = tuple(
        AllocationLine(
            order=row["order"],
            batch_id=row["batch_id"],
            source_kind=row["source_kind"],
            plant=row["plant"],
            sku=row["sku"],
            requested_sku=row["requested_sku"],
            qty=row["qty"],
            available_at=row["available_at"],
            eta=row["eta"],
            layer=Layer(row["layer"]),
            substituted=row["substituted"],
            reservation_class=row["reservation_class"],
            rule_version=row["rule_version"],
            evidence=tuple(row["evidence"]),
            commitment_state=(
                CommitmentState(row["commitment_state"])
                if row.get("commitment_state") else None
            ),
        )
        for row in data["allocations"]
    )
    unmet = tuple(
        UnmetLine(
            order=row["order"],
            qty=row["qty"],
            reason=UnmetReason(row["reason"]),
            evidence=tuple(row["evidence"]),
        )
        for row in data["unmet"]
    )
    orders = tuple(
        OrderSummary(
            order=row["order"],
            requested=row["requested"],
            allocated=row["allocated"],
            status=AllocationStatus(row["status"]),
            layers_used=tuple(Layer(x) for x in row["layers_used"]),
            need_date=row["need_date"],
            latest_eta=row["latest_eta"],
        )
        for row in data["orders"]
    )
    affected = tuple(
        AffectedCommitment(
            order=row["order"],
            state=CommitmentState(row["state"]),
            notified=tuple(row["notified"]),
            current=tuple(row["current"]),
            reason=row["reason"],
        )
        for row in data["affected_commitments"]
    )
    m = data["metrics"]
    metrics = StrategyMetrics(
        strategy=m["strategy"],
        demand_count=m["demand_count"],
        token_count=m["token_count"],
        layers_opened=m["layers_opened"],
        solver_iterations=m["solver_iterations"],
        comparisons=m["comparisons"],
        big_o=m["big_o"],
    )
    return AllocationPlan(
        status=RunStatus(data["status"]),
        strategy=data["strategy"],
        rule_version=data["rule_version"],
        as_of=data["as_of"],
        lines=lines,
        unmet=unmet,
        orders=orders,
        affected_commitments=affected,
        metrics=metrics,
        conservation=dict(data["conservation"]),
        residual={k: int(v) for k, v in data.get("residual_supply", {}).items()},
        failed_plants=tuple(data.get("failed_plants", ())),
        duplicate_events=tuple(data.get("duplicate_events", ())),
        journal_id=data.get("journal_id"),
        resumed=bool(data.get("resumed", False)),
    )
