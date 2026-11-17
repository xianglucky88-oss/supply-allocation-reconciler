"""Eligibility and explanation helpers shared by every solver.

The same predicate decides what a token may serve in both the greedy and the
hierarchical solver, which keeps their explanations interchangeable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import Demand, Layer, RuleVersion, UnmetReason
from .timeline import Projection, Token


@dataclass(frozen=True)
class Candidate:
    token: Token
    layer: Layer
    plant_rank: int      # index in demand.plant_pref, len(pref) when unlisted
    time_rank: int       # rank of available_at among same-layer candidates
    substituted: bool
    edge_rank: Optional[int]


def _sub_skus(demand: Demand, rules: RuleVersion) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for edge in rules.substitution_map().get(demand.sku, ()):
        if edge.allowed_classes and demand.reservation_class not in edge.allowed_classes:
            continue
        out[edge.to_sku] = edge.rank
    return out


def _class_ok(demand: Demand, token: Token, rules: RuleVersion,
              layer: Layer, substituted: bool) -> bool:
    if token.reservation_class == demand.reservation_class:
        return True
    if layer == Layer.RELAXED:
        return rules.allows_relaxation(
            token.sku, token.reservation_class, demand.reservation_class
        )
    if substituted:
        # Substitution edges explicitly name the classes they may serve;
        # an empty allow-list means "every class".
        for edge in rules.substitution_map().get(demand.sku, ()):
            if edge.to_sku == token.sku:
                return not edge.allowed_classes or (
                    demand.reservation_class in edge.allowed_classes
                )
    return False


def _time_ok(demand: Demand, token: Token) -> bool:
    if token.available_at > demand.need_date:
        return False
    if token.expires_at is not None and token.expires_at < demand.need_date:
        return False
    return True


def candidates_for_layer(
    demand: Demand,
    tokens: Sequence[Token],
    rules: RuleVersion,
    layer: Layer,
) -> List[Candidate]:
    """Return the usable tokens for one resolution layer, deterministically."""
    sub_skus = _sub_skus(demand, rules)
    picked: List[Tuple[Token, bool, Optional[int]]] = []
    for token in tokens:
        substituted = False
        edge_rank: Optional[int] = None
        if layer == Layer.PREFERRED or layer == Layer.RELAXED:
            if token.sku != demand.sku:
                continue
        elif layer == Layer.SUBSTITUTE:
            if token.sku not in sub_skus:
                continue
            substituted = True
            edge_rank = sub_skus[token.sku]
        if not _class_ok(demand, token, rules, layer, substituted):
            continue
        if not _time_ok(demand, token):
            continue
        picked.append((token, substituted, edge_rank))

    availability = sorted({t.available_at for t, _, _ in picked})
    time_ranks = {ts: i for i, ts in enumerate(availability)}
    pref = list(demand.plant_pref)

    def plant_rank(token: Token) -> int:
        return pref.index(token.plant) if token.plant in pref else len(pref)

    def sort_key(item: Tuple[Token, bool, Optional[int]]) -> tuple:
        token, _, edge = item
        kind_rank = 0 if token.kind == "batch" else 1
        return (
            plant_rank(token),
            kind_rank,
            time_ranks[token.available_at],
            (edge if edge is not None else 0),
            token.token_id,
        )

    picked.sort(key=sort_key)
    return [
        Candidate(
            token=token,
            layer=layer,
            plant_rank=plant_rank(token),
            time_rank=time_ranks[token.available_at],
            substituted=substituted,
            edge_rank=edge_rank,
        )
        for token, substituted, edge_rank in picked
    ]


def _all_supply_skus(proj: Projection) -> List[str]:
    skus = {b.sku for b in proj.batches.values()}
    skus.update(s.sku for s in proj.shipments.values())
    return sorted(skus)


def explain_unmet(
    demand: Demand,
    proj: Projection,
    rules: RuleVersion,
    *,
    eligible_existed: bool,
) -> Tuple[UnmetReason, List[str]]:
    """Classify why a demand could not be (fully) served.

    ``eligible_existed`` says whether at least one *eligible* token was seen
    during solving; when it is false we walk the raw supply to attribute the
    blockage to timing, plant health or reservation policy.
    """
    sub_skus = set(_sub_skus(demand, rules))
    compatible = [
        s for s in _all_supply_records(proj)
        if s["sku"] == demand.sku or s["sku"] in sub_skus
    ]
    if not compatible:
        return UnmetReason.NO_SUPPLY, [
            f"no supply for sku={demand.sku} or its substitutes "
            f"(available skus: {_all_supply_skus(proj) or 'none'})"
        ]

    offline = [s for s in compatible if s["plant"] in proj.plants_offline]
    online = [s for s in compatible if s["plant"] not in proj.plants_offline]
    if offline and not online:
        return UnmetReason.PLANT_OFFLINE, [
            f"all compatible supply sits on offline plant(s): "
            f"{sorted({s['plant'] for s in offline})}"
        ]

    expired = [
        s for s in online
        if s["expires_at"] is not None and s["expires_at"] < demand.need_date
    ]
    late = [s for s in online if s["available_at"] > demand.need_date]
    # Records that miss the need window for either timing reason.
    blocking = [s for s in online if not _time_ok(demand, _record_token(s))]
    # Records of the exact requested SKU that are timing-infeasible.  When
    # every *remaining* same-SKU source misses the need window and no other
    # eligible source exists, the residual gap is timing, not contention.
    exact = [s for s in online if s["sku"] == demand.sku]
    exact_blocking = [s for s in exact if not _time_ok(demand, _record_token(s))]
    # Same-SKU, in-window supply that reservation policy forbids is the most
    # actionable explanation and therefore outranks timing of other sources.
    class_blocked_exact = [
        s for s in exact
        if _time_ok(demand, _record_token(s))
        and s["reservation_class"] != demand.reservation_class
        and not rules.allows_relaxation(
            s["sku"], s["reservation_class"], demand.reservation_class
        )
    ]
    if class_blocked_exact:
        return UnmetReason.RESERVATION_BLOCKED, [
            f"reservation class {demand.reservation_class!r} cannot consume "
            f"in-window sku={demand.sku} class "
            f"{sorted({s['reservation_class'] for s in class_blocked_exact})} "
            f"(rule {rules.version})"
        ]
    if exact and len(exact_blocking) == len(exact):
        # No same-SKU source can serve the need window at all.
        return _timing_reason(demand, expired, late, exact_blocking)
    if exact_blocking:
        # Some same-SKU supply was usable but insufficient; everything left
        # of that SKU is timing-infeasible, so the residual gap is timing.
        return _timing_reason(demand, expired, late, exact_blocking)
    if blocking and len(blocking) == len(online):
        return _timing_reason(demand, expired, late, blocking)

    # Timing is fine: reservation policy or contention explains the gap.
    class_blocked = [
        s for s in online
        if s["reservation_class"] != demand.reservation_class
        and not rules.allows_relaxation(
            s["sku"], s["reservation_class"], demand.reservation_class
        )
        and s["sku"] not in sub_skus
    ]
    if class_blocked and not eligible_existed:
        return UnmetReason.RESERVATION_BLOCKED, [
            f"reservation class {demand.reservation_class!r} cannot consume "
            f"class {sorted({s['reservation_class'] for s in class_blocked})}"
        ]

    return UnmetReason.CAPACITY_CONSUMED, [
        "eligible supply existed but was fully consumed by higher-priority "
        "or earlier-due orders during this run"
    ]


def _timing_reason(demand: Demand, expired: List[dict], late: List[dict],
                   blocking: List[dict]) -> Tuple[UnmetReason, List[str]]:
    if expired and not late:
        return UnmetReason.EXPIRED, [
            f"batch(es) expire before need_date={demand.need_date}: "
            f"{[s['id'] for s in expired]}"
        ]
    if late and not expired:
        return UnmetReason.LATE, [
            f"supply arrives after need_date={demand.need_date}: "
            f"{[(s['id'], s['available_at']) for s in late]}"
        ]
    return UnmetReason.LATE, [
        f"compatible supply misses need_date={demand.need_date}: "
        f"expired={[s['id'] for s in expired]}, "
        f"late={[(s['id'], s['available_at']) for s in late]}"
    ]


def _record_token(record: Dict[str, Any]) -> Token:
    return Token(
        token_id=record["id"],
        kind=record["kind"],
        ref_id=record["id"],
        plant=record["plant"],
        sku=record["sku"],
        qty=0,
        available_at=record["available_at"],
        expires_at=record["expires_at"],
        reservation_class=record["reservation_class"],
    )


def _all_supply_records(proj: Projection) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for batch in proj.batches.values():
        out.append(
            {
                "id": batch.batch_id,
                "kind": "batch",
                "plant": batch.plant,
                "sku": batch.sku,
                "available_at": batch.available_at,
                "expires_at": batch.expires_at,
                "reservation_class": batch.reservation_class,
            }
        )
    for ship in proj.shipments.values():
        if ship.cancelled:
            continue
        out.append(
            {
                "id": ship.shipment_id,
                "kind": "shipment",
                "plant": ship.plant,
                "sku": ship.sku,
                "available_at": ship.eta,
                "expires_at": None,
                "reservation_class": ship.reservation_class,
            }
        )
    return out
