"""Timeline projection.

Orders, on-hand batches, in-transit shipments, plant health and reservation
policy are projected onto a single integer time axis.  The projection is a
pure fold over an ordered, idempotent event log, which makes replay from any
rule version deterministic.

A *token* is a unit of usable supply at time ``t``::

    token = (batch | open shipment) x eligibility(need_date, reservation)

The allocator never sees "total inventory": a token only exists once its
availability/ETA time has been reached and it has not expired, so inventory
arriving tomorrow cannot serve an order due today.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import UnknownReferenceError, ValidationError
from .models import Batch, Commitment, Demand, Shipment, SubstituteEdge


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Event:
    event_id: str
    type: str
    occurred_at: int
    payload: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # frozen dataclass + dict default: normalise once.
        object.__setattr__(self, "payload", dict(self.payload or {}))


KNOWN_EVENT_TYPES = frozenset(
    {
        "shipment.partial_arrival",
        "shipment.delayed",
        "shipment.cancelled",
        "batch.expired",
        "batch.adjusted",
        "demand.cancelled",
        "plant.offline",
        "plant.online",
    }
)


# ---------------------------------------------------------------------------
# Supply tokens
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Token:
    token_id: str
    kind: str                 # "batch" | "shipment"
    ref_id: str
    plant: str
    sku: str
    qty: int
    available_at: int        # on-hand release time or current ETA
    expires_at: Optional[int]
    reservation_class: str


@dataclass
class Projection:
    """Mutable fold state; converted to immutable tokens before solving."""

    as_of: int
    batches: Dict[str, Batch]
    shipments: Dict[str, Shipment]
    demands: Dict[str, Demand]
    plants_offline: set
    substitutions: Tuple[SubstituteEdge, ...]
    commitments: Dict[str, Commitment]
    applied_events: List[str]
    duplicate_events: List[str]
    # Token id -> reduced capacity.  Used by incremental recomputation so a
    # sub-problem only sees supply not frozen for untouched orders.
    token_capacity_override: Dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.token_capacity_override is None:
            self.token_capacity_override = {}

    # -- supply view ------------------------------------------------------
    def tokens(self) -> List[Token]:
        tokens: List[Token] = []
        for batch in sorted(self.batches.values(), key=lambda b: b.batch_id):
            if batch.plant in self.plants_offline:
                continue
            if batch.expires_at is not None and batch.expires_at < self.as_of:
                continue
            qty = self.token_capacity_override.get(
                batch.batch_id, batch.qty_on_hand
            )
            if qty <= 0:
                continue
            tokens.append(
                Token(
                    token_id=batch.batch_id,
                    kind="batch",
                    ref_id=batch.batch_id,
                    plant=batch.plant,
                    sku=batch.sku,
                    qty=qty,
                    available_at=max(batch.available_at, 0),
                    expires_at=batch.expires_at,
                    reservation_class=batch.reservation_class,
                )
            )
        for ship in sorted(self.shipments.values(), key=lambda s: s.shipment_id):
            if ship.cancelled or ship.open_qty <= 0:
                continue
            if ship.plant in self.plants_offline:
                continue
            qty = self.token_capacity_override.get(ship.shipment_id, ship.open_qty)
            if qty <= 0:
                continue
            tokens.append(
                Token(
                    token_id=ship.shipment_id,
                    kind="shipment",
                    ref_id=ship.shipment_id,
                    plant=ship.plant,
                    sku=ship.sku,
                    qty=qty,
                    available_at=ship.eta,
                    expires_at=None,
                    reservation_class=ship.reservation_class,
                )
            )
        return tokens

    def active_demands(self) -> List[Demand]:
        return sorted(
            (d for d in self.demands.values() if not d.cancelled),
            key=lambda d: (d.priority, d.need_date, d.order, d.seq),
        )

    def cancelled_orders(self) -> List[str]:
        return sorted(
            order for order, d in self.demands.items() if d.cancelled
        )

    def total_supply_qty(self) -> int:
        return sum(token.qty for token in self.tokens())


# ---------------------------------------------------------------------------
# Event fold
# ---------------------------------------------------------------------------
def _apply_event(proj: Projection, event: Event) -> None:
    p = event.payload
    etype = event.type
    if etype == "shipment.partial_arrival":
        ship = proj.shipments.get(p["shipment_id"])
        if ship is None:
            raise UnknownReferenceError(
                f"event {event.event_id}: unknown shipment {p['shipment_id']}"
            )
        received = ship.qty_received + int(p["qty"])
        if received > ship.qty_planned:
            raise ValidationError(
                f"event {event.event_id}: cumulative arrival {received} exceeds "
                f"planned {ship.qty_planned} for shipment {ship.shipment_id}"
            )
        proj.shipments[ship.shipment_id] = replace(ship, qty_received=received)

    elif etype == "shipment.delayed":
        ship = proj.shipments.get(p["shipment_id"])
        if ship is None:
            raise UnknownReferenceError(
                f"event {event.event_id}: unknown shipment {p['shipment_id']}"
            )
        new_eta = int(p["eta"])
        if new_eta < ship.eta:
            raise ValidationError(
                f"event {event.event_id}: delay may not move ETA earlier "
                f"({ship.eta} -> {new_eta})"
            )
        proj.shipments[ship.shipment_id] = replace(ship, eta=new_eta)

    elif etype == "shipment.cancelled":
        ship = proj.shipments.get(p["shipment_id"])
        if ship is None:
            raise UnknownReferenceError(
                f"event {event.event_id}: unknown shipment {p['shipment_id']}"
            )
        proj.shipments[ship.shipment_id] = replace(ship, cancelled=True)

    elif etype == "batch.expired":
        batch = proj.batches.get(p["batch_id"])
        if batch is None:
            raise UnknownReferenceError(
                f"event {event.event_id}: unknown batch {p['batch_id']}"
            )
        proj.batches[batch.batch_id] = replace(
            batch, expires_at=min(batch.expires_at, event.occurred_at)
            if batch.expires_at is not None else event.occurred_at
        )

    elif etype == "batch.adjusted":
        batch = proj.batches.get(p["batch_id"])
        if batch is None:
            raise UnknownReferenceError(
                f"event {event.event_id}: unknown batch {p['batch_id']}"
            )
        new_qty = int(p["qty_on_hand"])
        if new_qty < 0:
            raise ValidationError(
                f"event {event.event_id}: negative on-hand qty"
            )
        proj.batches[batch.batch_id] = replace(
            batch, qty_on_hand=new_qty, version=batch.version + 1
        )

    elif etype == "demand.cancelled":
        demand = proj.demands.get(p["order"])
        if demand is None:
            raise UnknownReferenceError(
                f"event {event.event_id}: unknown order {p['order']}"
            )
        proj.demands[demand.order] = replace(demand, cancelled=True)

    elif etype == "plant.offline":
        plant = p["plant"]
        known = {b.plant for b in proj.batches.values()} | {
            s.plant for s in proj.shipments.values()
        }
        if plant not in known:
            raise UnknownReferenceError(
                f"event {event.event_id}: unknown plant {plant}"
            )
        proj.plants_offline.add(plant)

    elif etype == "plant.online":
        proj.plants_offline.discard(p["plant"])

    else:  # pragma: no cover - guarded at parse time
        raise ValidationError(f"unknown event type {etype}")


def project(
    *,
    as_of: int,
    batches: Sequence[Batch],
    shipments: Sequence[Shipment],
    demands: Sequence[Demand],
    events: Sequence[Event],
    substitutions: Sequence[SubstituteEdge] = (),
    commitments: Optional[Dict[str, Commitment]] = None,
    plants_offline: Sequence[str] = (),
) -> Projection:
    """Fold base state plus the event log into a single projection.

    Events are ordered by ``(occurred_at, event_id)``; replaying the same
    log always yields the same projection.  Duplicate ``event_id`` values
    are applied exactly once and recorded in ``duplicate_events``.
    """
    proj = Projection(
        as_of=as_of,
        batches={b.batch_id: b for b in batches},
        shipments={s.shipment_id: s for s in shipments},
        demands={d.order: d for d in demands},
        plants_offline=set(plants_offline),
        substitutions=tuple(substitutions),
        commitments=dict(commitments or {}),
        applied_events=[],
        duplicate_events=[],
    )
    # Duplicate detection is order-independent: among events sharing an id,
    # the canonical occurrence is the first one under the same total ordering
    # used to apply events, so shuffling the input log cannot change which
    # payload wins.  Every other occurrence is recorded as a duplicate and
    # never applied.
    by_id: Dict[str, List[Event]] = {}
    for event in events:
        by_id.setdefault(event.event_id, []).append(event)
    canonical: Dict[str, Event] = {}
    duplicates: List[str] = []
    for event_id, group in by_id.items():
        ordered = sorted(group, key=lambda e: (e.occurred_at, e.event_id))
        canonical[event_id] = ordered[0]
        duplicates.extend([event_id] * (len(group) - 1))
    proj.duplicate_events.extend(sorted(duplicates))

    seen: set = set()
    for event in sorted(canonical.values(),
                        key=lambda e: (e.occurred_at, e.event_id)):
        if event.type not in KNOWN_EVENT_TYPES:
            raise ValidationError(
                f"event {event.event_id}: unknown type {event.type}"
            )
        if event.event_id in seen:  # pragma: no cover - impossible by construction
            proj.duplicate_events.append(event.event_id)
            continue
        seen.add(event.event_id)
        _apply_event(proj, event)
        proj.applied_events.append(event.event_id)
    return proj
