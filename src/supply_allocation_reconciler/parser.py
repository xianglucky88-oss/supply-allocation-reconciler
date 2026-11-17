"""Input validation and record extraction.

Two shapes are accepted:

1. the canonical time-aware contract (batches / shipments / demands /
   events / rule_versions / commitments);
2. the original flat fixture shape (``demands`` + single-pool ``inventory``),
   which is upgraded deterministically so old inputs keep working.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from .errors import ValidationError
from .models import (
    Batch,
    Commitment,
    CommitmentLine,
    Demand,
    Layer,
    Shipment,
)
from .timeline import Event


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def _require(obj: dict, key: str, ctx: str) -> Any:
    if key not in obj:
        raise ValidationError(f"{ctx}: missing required field {key!r}")
    return obj[key]


def _int(obj: dict, key: str, ctx: str, *, minimum: int = None) -> int:
    value = _require(obj, key, ctx)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{ctx}: field {key!r} must be an integer")
    if minimum is not None and value < minimum:
        raise ValidationError(
            f"{ctx}: field {key!r} must be >= {minimum}"
        )
    return value


def _str(obj: dict, key: str, ctx: str) -> str:
    value = _require(obj, key, ctx)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{ctx}: field {key!r} must be a non-empty string")
    return value


def _str_tuple(obj: dict, key: str, ctx: str) -> Tuple[str, ...]:
    value = obj.get(key, ())
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{ctx}: field {key!r} must be a list")
    out: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValidationError(f"{ctx}: {key!r} entries must be strings")
        out.append(item)
    return tuple(out)


# ---------------------------------------------------------------------------
# Canonical parsing
# ---------------------------------------------------------------------------
def _parse_batches(raw: Sequence[dict]) -> List[Batch]:
    out: List[Batch] = []
    seen: set = set()
    for i, row in enumerate(raw):
        ctx = f"batches[{i}]"
        batch_id = _str(row, "batch_id", ctx)
        if batch_id in seen:
            raise ValidationError(f"{ctx}: duplicate batch_id {batch_id!r}")
        seen.add(batch_id)
        out.append(
            Batch(
                batch_id=batch_id,
                plant=_str(row, "plant", ctx),
                sku=_str(row, "sku", ctx),
                qty_on_hand=_int(row, "qty_on_hand", ctx, minimum=0),
                available_at=int(row.get("available_at", 0)),
                expires_at=(
                    int(row["expires_at"])
                    if row.get("expires_at") is not None
                    else None
                ),
                reservation_class=str(row.get("reservation_class", "default")),
            )
        )
    return out


def _parse_shipments(raw: Sequence[dict]) -> List[Shipment]:
    out: List[Shipment] = []
    seen: set = set()
    for i, row in enumerate(raw):
        ctx = f"shipments[{i}]"
        ship_id = _str(row, "shipment_id", ctx)
        if ship_id in seen:
            raise ValidationError(f"{ctx}: duplicate shipment_id {ship_id!r}")
        seen.add(ship_id)
        planned = _int(row, "qty_planned", ctx, minimum=0)
        received = int(row.get("qty_received", 0))
        if received > planned:
            raise ValidationError(
                f"{ctx}: qty_received {received} > qty_planned {planned}"
            )
        out.append(
            Shipment(
                shipment_id=ship_id,
                plant=_str(row, "plant", ctx),
                sku=_str(row, "sku", ctx),
                qty_planned=planned,
                eta=_int(row, "eta", ctx),
                reservation_class=str(row.get("reservation_class", "default")),
                qty_received=received,
                cancelled=bool(row.get("cancelled", False)),
            )
        )
    return out


def _parse_demands(raw: Sequence[dict]) -> List[Demand]:
    out: List[Demand] = []
    seen: set = set()
    for i, row in enumerate(raw):
        ctx = f"demands[{i}]"
        order = _str(row, "order", ctx)
        if order in seen:
            raise ValidationError(f"{ctx}: duplicate order {order!r}")
        seen.add(order)
        out.append(
            Demand(
                order=order,
                sku=_str(row, "sku", ctx),
                qty=_int(row, "qty", ctx, minimum=1),
                priority=_int(row, "priority", ctx, minimum=1),
                need_date=_int(row, "need_date", ctx),
                plant_pref=_str_tuple(row, "plant_pref", ctx),
                reservation_class=str(row.get("reservation_class", "default")),
                cancelled=bool(row.get("cancelled", False)),
                seq=i,
            )
        )
    return out


def _parse_events(raw: Sequence[dict]) -> List[Event]:
    out: List[Event] = []
    for i, row in enumerate(raw):
        ctx = f"events[{i}]"
        out.append(
            Event(
                event_id=_str(row, "event_id", ctx),
                type=_str(row, "type", ctx),
                occurred_at=_int(row, "occurred_at", ctx),
                payload=dict(_require(row, "payload", ctx)),
            )
        )
    return out


def _parse_commitments(raw: Sequence[dict]) -> Dict[str, Commitment]:
    out: Dict[str, Commitment] = {}
    for i, row in enumerate(raw):
        ctx = f"commitments[{i}]"
        order = _str(row, "order", ctx)
        if order in out:
            raise ValidationError(f"{ctx}: duplicate commitment for {order!r}")
        lines = []
        for j, line in enumerate(_require(row, "lines", ctx)):
            lctx = f"{ctx}.lines[{j}]"
            lines.append(
                CommitmentLine(
                    order=order,
                    batch_id=_str(line, "batch_id", lctx),
                    qty=_int(line, "qty", lctx, minimum=1),
                    sku=_str(line, "sku", lctx),
                    layer=Layer(_str(line, "layer", lctx)),
                    notified_at=_int(line, "notified_at", lctx),
                )
            )
        out[order] = Commitment(order=order, lines=tuple(lines))
    return out


def load_canonical(payload: dict) -> Dict[str, Any]:
    """Validate a canonical payload and return parsed domain objects."""
    if not isinstance(payload, dict):
        raise ValidationError("payload must be an object")
    demands = _parse_demands(payload.get("demands", []))
    return {
        "as_of": _int(payload, "as_of", "root"),
        "batches": _parse_batches(payload.get("batches", [])),
        "shipments": _parse_shipments(payload.get("shipments", [])),
        "demands": demands,
        "events": _parse_events(payload.get("events", [])),
        "commitments": _parse_commitments(payload.get("commitments", [])),
        "plants_offline": list(payload.get("plants_offline", [])),
        "rule_version": payload.get("rule_version"),
        "strategy": str(payload.get("strategy", "hierarchical")),
    }


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------
def is_legacy(payload: dict) -> bool:
    return (
        isinstance(payload, dict)
        and "inventory" in payload
        and "batches" not in payload
        and "as_of" not in payload
    )


def upgrade_legacy(payload: dict) -> dict:
    """Map the old single-pool shape onto the canonical contract.

    Each inventory row becomes an immediately available, never-expiring
    batch; demands get a default need date of ``0`` so the old behaviour
    (all stock usable at all times) is reproduced exactly.
    """
    batches: List[dict] = []
    skus: List[str] = []
    for i, row in enumerate(payload.get("inventory", [])):
        if not isinstance(row, dict):
            raise ValidationError("inventory entries must be objects")
        sku = _str(row, "sku", f"inventory[{i}]")
        skus.append(sku)
        batches.append(
            {
                "batch_id": f"B{i + 1}",
                "plant": str(row.get("plant", "P1")),
                "sku": sku,
                "qty_on_hand": _int(row, "qty", f"inventory[{i}]", minimum=0),
                "available_at": 0,
            }
        )
    default_sku = skus[0] if skus else "DEFAULT"
    demands: List[dict] = []
    for i, row in enumerate(payload.get("demands", [])):
        if not isinstance(row, dict):
            raise ValidationError("demands entries must be objects")
        demands.append(
            {
                "order": _str(row, "order", f"demands[{i}]"),
                "sku": str(row.get("sku", default_sku)),
                "qty": _int(row, "qty", f"demands[{i}]", minimum=1),
                "priority": _int(row, "priority", f"demands[{i}]", minimum=1),
                "need_date": int(row.get("need_date", 0)),
                "reservation_class": str(
                    row.get("reservation_class", "default")
                ),
            }
        )
    return {
        "as_of": 0,
        "batches": batches,
        "shipments": [],
        "demands": demands,
        "events": [],
        "rule_versions": [],
        "commitments": [],
        "plants_offline": [],
        "strategy": payload.get("strategy", "greedy"),
    }


def load_records(payload: dict) -> List[dict]:
    """Backward-compatible shim used by the original test/CLI surface."""
    rows = payload.get("demands", [])
    if not isinstance(rows, list):
        raise ValidationError("demands must be a list")
    return [dict(row) for row in rows if isinstance(row, dict)]
