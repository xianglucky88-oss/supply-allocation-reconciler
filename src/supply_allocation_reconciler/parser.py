"""Input validation and record extraction.

The parser is the validation boundary: every illegal payload is rejected here
with a structured ``ValueError`` (record path + reason) before the engine runs.
Nothing is silently dropped, coerced or invented.

Semantics encoded here:

- quantities and priorities must be finite numbers (booleans excluded);
- quantities must be >= 0 (a zero-quantity demand is a cancelled order);
- ``order`` / ``sku`` must be non-empty strings;
- duplicate demand ``order`` ids are illegal (allocations must trace back to
  exactly one order);
- duplicate inventory ``sku`` records are legal: they are partial arrivals and
  their quantities sum;
- unknown keys are ignored (tolerant reader), missing sections default to [].
"""

import math


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _quantity(value, path):
    if not _is_number(value):
        raise ValueError(f"{path}: illegal quantity {value!r} (must be a number)")
    if not math.isfinite(value):
        raise ValueError(f"{path}: illegal quantity {value!r} (must be finite)")
    if value < 0:
        raise ValueError(f"{path}: illegal quantity {value!r} (must be >= 0)")
    return value


def _priority(value, path):
    if not _is_number(value) or not math.isfinite(value):
        raise ValueError(f"{path}: illegal priority {value!r} (must be a finite number)")
    return value


def _identifier(value, path, field):
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{path}: illegal {field} {value!r} (must be a non-empty string)")
    return value


def _rows(payload, key):
    if not isinstance(payload, dict):
        raise ValueError(f"payload: must be a JSON object, got {type(payload).__name__}")
    rows = payload.get(key, [])
    if not isinstance(rows, list):
        raise ValueError(f"{key}: must be a list")
    return rows


def _fields(row, path, required):
    for field in required:
        if field not in row:
            raise ValueError(f"{path}.{field}: missing")


def load_records(payload: dict) -> list[dict]:
    """Validate and extract demand records (one allocation line per order)."""
    demands = []
    seen = set()
    for i, row in enumerate(_rows(payload, "demands")):
        path = f"demands[{i}]"
        if not isinstance(row, dict):
            raise ValueError(f"{path}: must be a demand object")
        _fields(row, path, ("order", "priority", "qty"))
        order = _identifier(row["order"], f"{path}.order", "order")
        if order in seen:
            raise ValueError(f"{path}.order: duplicate order {order!r}")
        seen.add(order)
        demands.append({
            "order": order,
            "priority": _priority(row["priority"], f"{path}.priority"),
            "qty": _quantity(row["qty"], f"{path}.qty"),
        })
    return demands


def load_inventory(payload: dict) -> list[dict]:
    """Validate and extract inventory records (duplicate skus = partial arrivals)."""
    records = []
    for i, row in enumerate(_rows(payload, "inventory")):
        path = f"inventory[{i}]"
        if not isinstance(row, dict):
            raise ValueError(f"{path}: must be an inventory record object")
        _fields(row, path, ("sku", "qty"))
        records.append({
            "sku": _identifier(row["sku"], f"{path}.sku", "sku"),
            "qty": _quantity(row["qty"], f"{path}.qty"),
        })
    return records
