"""Immutable domain objects.

Every entity carries an explicit identifier so events, commitments and
allocation lines can reference each other deterministically.  All quantities
are integers (units); all timestamps are epoch seconds (``int``).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class PipelineResult:
    """Legacy envelope contract retained for backward compatibility."""

    project: str
    data: Dict[str, Any]


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class Layer(str, Enum):
    """Resolution layers, tried most specific first."""

    PREFERRED = "preferred"      # exact SKU + reservation class match
    SUBSTITUTE = "substitute"    # compatible replacement SKU
    RELAXED = "relaxed"          # reservation class relaxed (if rules allow)

    @property
    def order(self) -> int:
        return {"preferred": 0, "substitute": 1, "relaxed": 2}[self.value]


class AllocationStatus(str, Enum):
    FULFILLED = "fulfilled"      # full requested quantity allocated
    PARTIAL = "partial"          # some quantity allocated
    UNMET = "unmet"              # nothing allocated, reason code attached
    CANCELLED = "cancelled"      # demand was cancelled; supply released
    PRESERVED = "preserved"      # notified commitment protected as-is
    REPLACED = "replaced"        # notified commitment moved (explicit)
    REVOKED = "revoked"          # notified commitment cannot be honoured


class CommitmentState(str, Enum):
    HONOURED = "honoured"        # exact previously notified batch/qty kept
    PRESERVED = "preserved"      # kept after re-solve (line level)
    REPLACED = "replaced"        # changed; old snapshot retained for audit
    REVOKED = "revoked"          # no longer feasible; customer must be re-notified


class RunStatus(str, Enum):
    OK = "ok"                    # every active demand fully satisfied
    PARTIAL = "partial"          # at least one demand is short or a plant failed
    FAILED = "failed"            # contract/version/journal failure; no partial plan emitted
    RECOVERED = "recovered"      # result produced by resuming an interrupted journal


class UnmetReason(str, Enum):
    NO_SUPPLY = "no_supply"                       # no token of any compatible SKU
    LATE = "late"                                 # supply exists but ETA > need date
    EXPIRED = "expired"                           # supply expires before need date
    PLANT_OFFLINE = "plant_offline"               # only sources are failed plants
    RESERVATION_BLOCKED = "reservation_blocked"   # class reservation forbids use
    CAPACITY_CONSUMED = "capacity_consumed"       # tokens existed but higher-priority orders took them
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Supply side
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Batch:
    batch_id: str
    plant: str
    sku: str
    qty_on_hand: int
    available_at: int = 0              # quarantine release; not usable before
    expires_at: Optional[int] = None   # inclusive usable boundary
    reservation_class: str = "default"
    version: int = 1                   # bumped by batch.adjusted events


@dataclass(frozen=True)
class Shipment:
    shipment_id: str
    plant: str
    sku: str
    qty_planned: int
    eta: int
    reservation_class: str = "default"
    qty_received: int = 0              # running partial-arrival accumulator
    cancelled: bool = False

    @property
    def open_qty(self) -> int:
        return max(0, self.qty_planned - self.qty_received)


# ---------------------------------------------------------------------------
# Demand side
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Demand:
    order: str
    sku: str
    qty: int
    priority: int                     # 1 = highest
    need_date: int
    plant_pref: Tuple[str, ...] = ()  # ordered plant preference
    reservation_class: str = "default"
    cancelled: bool = False
    version: int = 1
    seq: int = 0                      # input order tie breaker


@dataclass(frozen=True)
class SubstituteEdge:
    from_sku: str
    to_sku: str
    rank: int = 1                     # 1 = best substitute
    allowed_classes: Tuple[str, ...] = ()  # empty => every class allowed


@dataclass(frozen=True)
class ReservationRule:
    """Class-level reservation policy for one rule version."""

    sku: str
    reservation_class: str
    allow_relaxation: bool = False    # may other classes consume this supply


@dataclass(frozen=True)
class RuleVersion:
    """An immutable, named bundle of reservation + substitution policy."""

    version: str
    reservations: Tuple[ReservationRule, ...] = ()
    substitutions: Tuple[SubstituteEdge, ...] = ()
    created_at: int = 0

    def substitution_map(self) -> Dict[str, Tuple[SubstituteEdge, ...]]:
        out: Dict[str, List[SubstituteEdge]] = {}
        for edge in sorted(self.substitutions, key=lambda e: (e.rank, e.to_sku)):
            out.setdefault(edge.from_sku, []).append(edge)
        return {sku: tuple(edges) for sku, edges in out.items()}

    def allows_relaxation(self, sku: str, token_class: str,
                          demand_class: str) -> bool:
        if token_class == demand_class:
            return True
        for rule in self.reservations:
            if rule.sku == sku and rule.reservation_class == token_class:
                return rule.allow_relaxation
        # Unknown classes default to protected.
        return False


# ---------------------------------------------------------------------------
# Previously notified commitments (must never be silently overwritten)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CommitmentLine:
    order: str
    batch_id: str
    qty: int
    sku: str
    layer: Layer
    notified_at: int


@dataclass(frozen=True)
class Commitment:
    order: str
    lines: Tuple[CommitmentLine, ...]


# ---------------------------------------------------------------------------
# Allocation output
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AllocationLine:
    order: str
    batch_id: str
    source_kind: str             # "batch" | "shipment" | "committed"
    plant: str
    sku: str
    requested_sku: str
    qty: int
    available_at: int
    eta: int
    layer: Layer
    substituted: bool
    reservation_class: str
    rule_version: str
    evidence: Tuple[str, ...] = ()
    commitment_state: Optional[CommitmentState] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order": self.order,
            "batch_id": self.batch_id,
            "source_kind": self.source_kind,
            "plant": self.plant,
            "sku": self.sku,
            "requested_sku": self.requested_sku,
            "qty": self.qty,
            "available_at": self.available_at,
            "eta": self.eta,
            "layer": self.layer.value,
            "substituted": self.substituted,
            "reservation_class": self.reservation_class,
            "rule_version": self.rule_version,
            "evidence": list(self.evidence),
            "commitment_state": (
                self.commitment_state.value
                if self.commitment_state is not None
                else None
            ),
        }


@dataclass(frozen=True)
class UnmetLine:
    order: str
    qty: int
    reason: UnmetReason
    evidence: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order": self.order,
            "qty": self.qty,
            "reason": self.reason.value,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class OrderSummary:
    order: str
    requested: int
    allocated: int
    status: AllocationStatus
    layers_used: Tuple[Layer, ...]
    need_date: int
    latest_eta: Optional[int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order": self.order,
            "requested": self.requested,
            "allocated": self.allocated,
            "short": self.requested - self.allocated,
            "status": self.status.value,
            "layers_used": [layer.value for layer in self.layers_used],
            "need_date": self.need_date,
            "latest_eta": self.latest_eta,
        }


@dataclass(frozen=True)
class AffectedCommitment:
    order: str
    state: CommitmentState
    notified: Tuple[Dict[str, Any], ...]
    current: Tuple[Dict[str, Any], ...]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order": self.order,
            "state": self.state.value,
            "notified": [dict(item) for item in self.notified],
            "current": [dict(item) for item in self.current],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class StrategyMetrics:
    strategy: str
    demand_count: int
    token_count: int
    layers_opened: int
    solver_iterations: int
    comparisons: int
    big_o: str

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["strategy"] = self.strategy
        return d


@dataclass(frozen=True)
class AllocationPlan:
    status: RunStatus
    strategy: str
    rule_version: str
    as_of: int
    lines: Tuple[AllocationLine, ...]
    unmet: Tuple[UnmetLine, ...]
    orders: Tuple[OrderSummary, ...]
    affected_commitments: Tuple[AffectedCommitment, ...]
    metrics: StrategyMetrics
    conservation: Dict[str, Any]
    residual: Dict[str, int] = field(default_factory=dict)
    failed_plants: Tuple[str, ...] = ()
    duplicate_events: Tuple[str, ...] = ()
    journal_id: Optional[str] = None
    resumed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "strategy": self.strategy,
            "rule_version": self.rule_version,
            "as_of": self.as_of,
            "orders": [o.to_dict() for o in self.orders],
            "allocations": [line.to_dict() for line in self.lines],
            "unmet": [line.to_dict() for line in self.unmet],
            "affected_commitments": [a.to_dict() for a in self.affected_commitments],
            "conservation": dict(self.conservation),
            "residual_supply": dict(sorted(self.residual.items())),
            "failed_plants": list(self.failed_plants),
            "duplicate_events": list(self.duplicate_events),
            "metrics": self.metrics.to_dict(),
            "journal_id": self.journal_id,
            "resumed": self.resumed,
        }


def line_with(line: AllocationLine, **changes: Any) -> AllocationLine:
    """Return a copy of an immutable allocation line with changed fields."""
    return replace(line, **changes)
