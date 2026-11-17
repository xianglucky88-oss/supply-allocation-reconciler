"""Allocation engine: domain calculation only.

Two full-solve strategies share one eligibility predicate and one plan
assembler, so their explanations and conservation invariants are identical:

* ``greedy``       -- priority-sorted, one order at a time, layers in order;
* ``hierarchical`` -- min-cost max-flow per resolution layer, globally optimal
  for the lexicographic (priority, plant, time, substitute) objective.

The engine is a pure function of (projection, rule version); journals and
replay orchestration live in :mod:`recovery`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .eligibility import Candidate, candidates_for_layer, explain_unmet
from .errors import ValidationError
from .flow import MinCostFlow
from .models import (
    AffectedCommitment,
    AllocationLine,
    AllocationPlan,
    AllocationStatus,
    CommitmentState,
    Demand,
    Layer,
    OrderSummary,
    RuleVersion,
    RunStatus,
    StrategyMetrics,
    UnmetLine,
)
from .timeline import Projection, Token

LAYERS = (Layer.PREFERRED, Layer.SUBSTITUTE, Layer.RELAXED)

# Cost constants for the hierarchical solver; each dominates the sum of all
# lower-ranked components for the graph sizes this domain produces.
C_PRIORITY = 10_000_000
C_PLANT = 100_000
C_TIME = 1_000
C_EDGE = 10


@dataclass
class SolveInput:
    projection: Projection
    rules: RuleVersion
    strategy: str = "hierarchical"


# ---------------------------------------------------------------------------
# Line construction
# ---------------------------------------------------------------------------
def token_id_of(batch_id: str) -> str:
    """Normalise an allocation/commitment source id to a token id."""
    if batch_id.startswith(("batch:", "shipment:")):
        return batch_id.split(":", 1)[1]
    return batch_id


def _line(demand: Demand, cand: Candidate, qty: int, rule_version: str,
          evidence: Sequence[str],
          commitment_state: Optional[CommitmentState] = None) -> AllocationLine:
    token = cand.token
    return AllocationLine(
        order=demand.order,
        batch_id=token.ref_id,
        source_kind=token.kind,
        plant=token.plant,
        sku=token.sku,
        requested_sku=demand.sku,
        qty=qty,
        available_at=token.available_at,
        eta=token.available_at,
        layer=cand.layer,
        substituted=cand.substituted,
        reservation_class=token.reservation_class,
        rule_version=rule_version,
        evidence=tuple(evidence),
        commitment_state=commitment_state,
    )


def _candidate_evidence(demand: Demand, cand: Candidate,
                        rules: RuleVersion) -> List[str]:
    token = cand.token
    out = [
        f"rule={rules.version} layer={cand.layer.value}",
        f"plant={token.plant} rank={cand.plant_rank}",
        f"available_at={token.available_at} need_date={demand.need_date}",
    ]
    if cand.substituted:
        out.append(
            f"substitute {demand.sku}->{token.sku} edge_rank={cand.edge_rank}"
        )
    if token.reservation_class == demand.reservation_class:
        out.append(f"reservation class={token.reservation_class} exact match")
    elif cand.layer == Layer.RELAXED:
        out.append(
            f"reservation relaxed: class {token.reservation_class} serves "
            f"{demand.reservation_class} (rule {rules.version})"
        )
    else:
        out.append(
            f"reservation class={token.reservation_class} accepted via "
            f"substitution allow-list"
        )
    if token.kind == "shipment":
        out.append(f"in-transit shipment eta={token.available_at}")
    if token.expires_at is not None:
        out.append(f"expires_at={token.expires_at}")
    return out


# ---------------------------------------------------------------------------
# Notified commitment protection (never silently overwritten)
# ---------------------------------------------------------------------------
def _preallocate_commitments(
    proj: Projection,
    rules: RuleVersion,
    remaining: Dict[str, int],
    tokens_by_id: Dict[str, Token],
) -> Tuple[List[AllocationLine], Dict[str, str], Dict[str, List[AllocationLine]]]:
    """Reserve every still-feasible notified commitment atomically per order.

    Returns (protected lines, order -> audit state, order -> protected lines).
    Infeasible commitments are *not* consumed: the order is returned to the
    normal solver and the customer-facing audit marks it revoked/replaced.
    """
    lines: List[AllocationLine] = []
    audit: Dict[str, str] = {}
    protected_by_order: Dict[str, List[AllocationLine]] = {}

    for order in sorted(proj.commitments):
        commitment = proj.commitments[order]
        demand = proj.demands.get(order)
        if demand is None or demand.cancelled:
            audit[order] = CommitmentState.REVOKED.value
            continue

        needed: List[Tuple[Any, int]] = []
        feasible = True
        reasons: List[str] = []
        total_qty = 0
        for cline in commitment.lines:
            token = tokens_by_id.get(token_id_of(cline.batch_id))
            if token is None:
                feasible = False
                reasons.append(f"committed source {cline.batch_id} no longer exists")
                break
            if token.available_at > demand.need_date:
                feasible = False
                reasons.append(
                    f"source {token.ref_id} eta {token.available_at} > "
                    f"need_date {demand.need_date}"
                )
                break
            if token.expires_at is not None and token.expires_at < demand.need_date:
                feasible = False
                reasons.append(f"source {token.ref_id} expired")
                break
            if token.plant in proj.plants_offline:
                feasible = False
                reasons.append(f"plant {token.plant} offline")
                break
            if remaining.get(token.token_id, 0) < cline.qty:
                feasible = False
                reasons.append(
                    f"source {token.ref_id} remaining "
                    f"{remaining.get(token.token_id, 0)} < committed {cline.qty}"
                )
                break
            total_qty += cline.qty
            needed.append((token, cline.qty))

        if total_qty > demand.qty:
            feasible = False
            reasons.append(
                f"committed qty {total_qty} exceeds current demand {demand.qty}"
            )
        if not feasible:
            audit[order] = CommitmentState.REVOKED.value
            audit[order + ":reason"] = "; ".join(reasons) or "commitment infeasible"
            continue

        for token, qty in needed:
            remaining[token.token_id] -= qty
            cand = Candidate(
                token=token,
                layer=Layer.PREFERRED,
                plant_rank=0,
                time_rank=0,
                substituted=(token.sku != demand.sku),
                edge_rank=None,
            )
            evidence = [
                f"rule={rules.version} protected notified commitment; "
                f"notified_at<=as_of, protected qty={qty}",
                "frozen customer promise: never silently overwritten",
            ]
            line = _line(
                demand, cand, qty, rules.version, evidence,
                commitment_state=CommitmentState.HONOURED,
            )
            lines.append(line)
            protected_by_order.setdefault(order, []).append(line)
        audit[order] = CommitmentState.HONOURED.value
    return lines, audit, protected_by_order


# ---------------------------------------------------------------------------
# Greedy solver
# ---------------------------------------------------------------------------
def _solve_greedy(inp: SolveInput, remaining: Dict[str, int],
                  skip_orders: set) -> Tuple[List[AllocationLine], Dict[str, bool],
                                             StrategyMetrics, set]:
    proj, rules = inp.projection, inp.rules
    out: List[AllocationLine] = []
    eligible_seen: Dict[str, bool] = {}
    layers_opened: set = set()
    comparisons = 0

    for demand in proj.active_demands():
        if demand.order in skip_orders:
            continue
        short = demand.qty
        for layer in LAYERS:
            if short <= 0:
                break
            candidates = [
                c for c in candidates_for_layer(
                    demand, _live_tokens(proj, remaining), rules, layer
                )
                if remaining.get(c.token.token_id, 0) > 0
            ]
            if not candidates:
                continue
            layers_opened.add(layer.value)
            for cand in candidates:
                comparisons += 1
                avail = remaining.get(cand.token.token_id, 0)
                if avail <= 0:
                    continue
                eligible_seen[demand.order] = True
                take = min(avail, short)
                remaining[cand.token.token_id] -= take
                short -= take
                out.append(
                    _line(
                        demand, cand, take, rules.version,
                        _candidate_evidence(demand, cand, rules),
                    )
                )
                if short <= 0:
                    break
        if short > 0:
            eligible_seen.setdefault(demand.order, False)
    metrics = StrategyMetrics(
        strategy="greedy",
        demand_count=len([d for d in proj.active_demands()
                          if d.order not in skip_orders]),
        token_count=len(proj.tokens()),
        layers_opened=len(layers_opened),
        solver_iterations=comparisons,
        comparisons=comparisons,
        big_o="O(D x S x L) time, O(S) space",
    )
    return out, eligible_seen, metrics, layers_opened


def _live_tokens(proj: Projection, remaining: Dict[str, int]) -> List[Token]:
    return [t for t in proj.tokens() if remaining.get(t.token_id, 0) > 0]


# ---------------------------------------------------------------------------
# Hierarchical min-cost flow solver
# ---------------------------------------------------------------------------
def _solve_hierarchical(inp: SolveInput, remaining: Dict[str, int],
                        skip_orders: set
                        ) -> Tuple[List[AllocationLine], Dict[str, bool],
                                   StrategyMetrics, set]:
    proj, rules = inp.projection, inp.rules
    demands = [d for d in proj.active_demands() if d.order not in skip_orders]
    out: List[AllocationLine] = []
    eligible_seen: Dict[str, bool] = {d.order: False for d in demands}
    layers_opened: set = set()
    total_relax = 0
    total_augment = 0
    demand_short = {d.order: d.qty for d in demands}

    for layer in LAYERS:
        active = [d for d in demands if demand_short[d.order] > 0]
        if not active:
            break
        candidate_map: Dict[str, List[Candidate]] = {}
        token_ids: List[str] = []
        token_index: Dict[str, int] = {}
        for demand in active:
            cands = [
                c for c in candidates_for_layer(
                    demand, _live_tokens(proj, remaining), rules, layer
                )
                if remaining.get(c.token.token_id, 0) > 0
            ]
            if cands:
                candidate_map[demand.order] = cands
                eligible_seen[demand.order] = True
                for cand in cands:
                    if cand.token.token_id not in token_index:
                        token_index[cand.token.token_id] = len(token_ids)
                        token_ids.append(cand.token.token_id)
        if not candidate_map:
            continue
        layers_opened.add(layer.value)

        n_demand = len(active)
        n_token = len(token_ids)
        source = 0
        demand_nodes = {d.order: i + 1 for i, d in enumerate(active)}
        token_node0 = 1 + n_demand
        sink = token_node0 + n_token
        mcf = MinCostFlow(sink + 1)

        for demand in active:
            node = demand_nodes[demand.order]
            mcf.add_edge(source, node, demand_short[demand.order],
                         demand.priority * C_PRIORITY + demand.seq * C_EDGE)
        arc_ref: Dict[Tuple[int, int], Candidate] = {}
        for demand in active:
            for cand in candidate_map.get(demand.order, []):
                tnode = token_node0 + token_index[cand.token.token_id]
                dnode = demand_nodes[demand.order]
                cost = (
                    cand.plant_rank * C_PLANT
                    + cand.time_rank * C_TIME
                    + (cand.edge_rank or 0) * C_EDGE
                    + token_index[cand.token.token_id]
                )
                mcf.add_edge(dnode, tnode, remaining[cand.token.token_id], cost)
                arc_ref[(dnode, tnode)] = cand
        for tid in token_ids:
            mcf.add_edge(token_node0 + token_index[tid], sink,
                         remaining[tid], 0)

        limit = sum(demand_short.values())
        _, _ = mcf.flow(source, sink, limit)
        total_relax += mcf.relaxations
        total_augment += mcf.augmentations

        for demand in active:
            dnode = demand_nodes[demand.order]
            for tnode, flow in mcf.edge_flows(dnode):
                if flow <= 0:
                    continue
                cand = arc_ref[(dnode, tnode)]
                remaining[cand.token.token_id] -= flow
                demand_short[demand.order] -= flow
                out.append(
                    _line(
                        demand, cand, flow, rules.version,
                        _candidate_evidence(demand, cand, rules),
                    )
                )

    metrics = StrategyMetrics(
        strategy="hierarchical",
        demand_count=len(demands),
        token_count=len(proj.tokens()),
        layers_opened=len(layers_opened),
        solver_iterations=total_relax,
        comparisons=total_augment,
        big_o="O(L x SPFA(D+S, DxS)) per recompute; O(D+S) space",
    )
    return out, eligible_seen, metrics, layers_opened


# ---------------------------------------------------------------------------
# Plan assembly
# ---------------------------------------------------------------------------
def _assemble(
    inp: SolveInput,
    lines: List[AllocationLine],
    remaining: Dict[str, int],
    eligible_seen: Dict[str, bool],
    metrics: StrategyMetrics,
    audit: Dict[str, str],
    protected_by_order: Dict[str, List[AllocationLine]],
    *,
    journal_id: Optional[str] = None,
    resumed: bool = False,
) -> AllocationPlan:
    proj, rules = inp.projection, inp.rules
    lines.sort(key=lambda l: (
        proj.demands[l.order].priority if l.order in proj.demands else 99,
        l.order, l.layer.order, l.batch_id,
    ))

    # ---- unmet ----------------------------------------------------------
    unmet: List[UnmetLine] = []
    allocated_by_order: Dict[str, int] = {}
    for line in lines:
        allocated_by_order[line.order] = (
            allocated_by_order.get(line.order, 0) + line.qty
        )
    for demand in proj.active_demands():
        got = allocated_by_order.get(demand.order, 0)
        if got < demand.qty:
            reason, evidence = explain_unmet(
                demand, proj, rules,
                eligible_existed=eligible_seen.get(demand.order, False),
            )
            unmet.append(
                UnmetLine(
                    order=demand.order, qty=demand.qty - got,
                    reason=reason, evidence=tuple(evidence),
                )
            )

    # ---- order summaries ------------------------------------------------
    orders: List[OrderSummary] = []
    all_demands = sorted(
        proj.demands.values(), key=lambda d: (d.priority, d.need_date, d.order)
    )
    for demand in all_demands:
        if demand.cancelled:
            orders.append(
                OrderSummary(
                    order=demand.order, requested=demand.qty, allocated=0,
                    status=AllocationStatus.CANCELLED, layers_used=(),
                    need_date=demand.need_date, latest_eta=None,
                )
            )
            continue
        got = allocated_by_order.get(demand.order, 0)
        order_lines = [l for l in lines if l.order == demand.order]
        etas = [l.eta for l in order_lines]
        if got == demand.qty:
            status = AllocationStatus.FULFILLED
        elif got > 0:
            status = AllocationStatus.PARTIAL
        else:
            status = AllocationStatus.UNMET
        if demand.order in audit:
            protected = protected_by_order.get(demand.order, [])
            if audit[demand.order] == CommitmentState.HONOURED.value:
                if len(protected) == len(order_lines) and status == AllocationStatus.FULFILLED:
                    status = AllocationStatus.PRESERVED
            elif got > 0:
                status = AllocationStatus.REPLACED
            else:
                status = AllocationStatus.REVOKED
        orders.append(
            OrderSummary(
                order=demand.order, requested=demand.qty, allocated=got,
                status=status,
                layers_used=tuple(sorted({l.layer for l in order_lines},
                                         key=lambda x: x.order)),
                need_date=demand.need_date,
                latest_eta=max(etas) if etas else None,
            )
        )

    # ---- commitment audit ----------------------------------------------
    affected: List[AffectedCommitment] = []
    for order in sorted(proj.commitments):
        commitment = proj.commitments[order]
        notified = [
            {
                "batch_id": cl.batch_id, "qty": cl.qty, "sku": cl.sku,
                "layer": cl.layer.value, "notified_at": cl.notified_at,
            }
            for cl in commitment.lines
        ]
        current = [l.to_dict() for l in lines if l.order == order]
        state_value = audit.get(order)
        if state_value == CommitmentState.HONOURED.value:
            state = CommitmentState.HONOURED
            reason = "notified commitment preserved atomically"
        else:
            demand = proj.demands.get(order)
            if demand is not None and demand.cancelled:
                state = CommitmentState.REVOKED
                reason = "order cancelled; committed supply released"
            elif current:
                state = CommitmentState.REPLACED
                reason = audit.get(order + ":reason",
                                   "original commitment infeasible; new proposal requires re-notification")
            else:
                state = CommitmentState.REVOKED
                reason = audit.get(order + ":reason",
                                   "commitment no longer feasible and no replacement supply exists")
        affected.append(
            AffectedCommitment(
                order=order, state=state,
                notified=tuple(notified), current=tuple(current), reason=reason,
            )
        )

    # ---- conservation ---------------------------------------------------
    initial_supply = sum(t.qty for t in proj.tokens())
    total_allocated = sum(l.qty for l in lines)
    total_residual = sum(remaining.values())
    total_demand = sum(d.qty for d in proj.active_demands())
    total_unmet = sum(u.qty for u in unmet)
    cancelled_qty = sum(
        d.qty for d in proj.demands.values() if d.cancelled
    )
    conservation = {
        "initial_supply": initial_supply,
        "total_allocated": total_allocated,
        "total_residual": total_residual,
        "active_demand": total_demand,
        "total_unmet": total_unmet,
        "cancelled_demand_released": cancelled_qty,
        "supply_balanced": total_allocated + total_residual == initial_supply,
        "demand_balanced": total_allocated + total_unmet == total_demand,
        "no_over_allocation": total_allocated <= initial_supply,
    }

    if unmet or proj.plants_offline:
        status = RunStatus.PARTIAL
    elif resumed:
        status = RunStatus.RECOVERED
    else:
        status = RunStatus.OK

    return AllocationPlan(
        status=status,
        strategy=metrics.strategy,
        rule_version=rules.version,
        as_of=proj.as_of,
        lines=tuple(lines),
        unmet=tuple(unmet),
        orders=tuple(orders),
        affected_commitments=tuple(affected),
        metrics=metrics,
        conservation=conservation,
        residual={tid: qty for tid, qty in sorted(remaining.items()) if qty > 0},
        failed_plants=tuple(sorted(proj.plants_offline)),
        duplicate_events=tuple(proj.duplicate_events),
        journal_id=journal_id,
        resumed=resumed,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def solve(inp: SolveInput, *, journal_id: Optional[str] = None,
          resumed: bool = False) -> AllocationPlan:
    if inp.strategy not in ("greedy", "hierarchical"):
        raise ValidationError(
            f"unknown strategy {inp.strategy!r}; "
            "use 'greedy' or 'hierarchical'"
        )
    tokens = inp.projection.tokens()
    remaining = {t.token_id: t.qty for t in tokens}
    tokens_by_id = {t.token_id: t for t in tokens}

    protected, audit, protected_by_order = _preallocate_commitments(
        inp.projection, inp.rules, remaining, tokens_by_id
    )
    # Protected orders still compete for their *remaining* open quantity.
    skip_all = {
        order for order, pls in protected_by_order.items()
        if sum(l.qty for l in pls) >= inp.projection.demands[order].qty
    }

    if inp.strategy == "greedy":
        solved, eligible, metrics, _ = _solve_greedy(
            inp, remaining, skip_all
        )
    else:
        solved, eligible, metrics, _ = _solve_hierarchical(
            inp, remaining, skip_all
        )

    # Protected orders whose commitment only covered part of the demand.
    for order in protected_by_order:
        eligible.setdefault(order, True)

    return _assemble(
        inp, protected + solved, remaining, eligible, metrics, audit,
        protected_by_order, journal_id=journal_id, resumed=resumed,
    )


# ---------------------------------------------------------------------------
# Legacy shim preserving the original module contract / fixture semantics
# ---------------------------------------------------------------------------
def calculate(rows, payload):  # pragma: no cover - exercised via test_core
    """Original flat-shape entry point, retained for backward compatibility."""
    from .parser import load_canonical, upgrade_legacy
    from .rules import RuleStore
    from .timeline import project

    canonical = upgrade_legacy(payload)
    parsed = load_canonical(canonical)
    parsed_rules = RuleStore()
    from .models import RuleVersion
    parsed_rules.publish(RuleVersion(version="legacy-default"))
    inp = SolveInput(
        projection=project(
            as_of=parsed["as_of"],
            batches=parsed["batches"],
            shipments=parsed["shipments"],
            demands=parsed["demands"],
            events=parsed["events"],
        ),
        rules=parsed_rules.get("legacy-default"),
        strategy="greedy",
    )
    plan = solve(inp)
    allocations = []
    unmet = []
    for order in plan.orders:
        if order.status == AllocationStatus.CANCELLED:
            continue
        allocations.append(
            {
                "order": order.order,
                "allocated": float(order.allocated),
                "requested": float(order.requested),
            }
        )
        if order.allocated < order.requested:
            unmet.append(
                {
                    "order": order.order,
                    "unmet": float(order.requested - order.allocated),
                }
            )
    return {
        "allocations": allocations,
        "unmet": unmet,
        "remaining_inventory": float(plan.conservation["total_residual"]),
    }
