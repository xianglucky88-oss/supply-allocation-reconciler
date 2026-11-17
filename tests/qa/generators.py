"""Random payload generators and a greedy counterexample shrinker.

Quantities are integers or exact binary fractions (multiples of 0.25) so all
arithmetic in the system under test and in the oracle is exact and results
compare with plain equality.
"""

import copy
import random

ORDER_POOL = ["O" + c for c in "ABCDEFGH"] + ["RUSH-1", "RUSH-2", "back/order"]
SKU_POOL = ["S1", "S2", "S3"]


def _qty(rng, hi):
    value = rng.randint(0, hi)
    if rng.random() < 0.25:
        value += rng.choice([0.25, 0.5, 0.75])
    return value


def random_payload(rng, max_demands=6, max_skus=3, max_arrivals=2):
    """A valid payload: unique orders, frequent priority ties, duplicate sku
    records (partial arrivals), occasional zero quantities / empty sections."""
    orders = rng.sample(ORDER_POOL, k=min(rng.randint(0, max_demands), len(ORDER_POOL)))
    demands = [
        {"order": order, "priority": rng.choice([1, 1, 1, 2, 2, 3]), "qty": _qty(rng, 40)}
        for order in orders
    ]
    inventory = []
    for sku in SKU_POOL[: rng.randint(0, max_skus)]:
        for _ in range(rng.randint(1, max_arrivals)):
            inventory.append({"sku": sku, "qty": _qty(rng, 60)})
    rng.shuffle(inventory)
    payload = {"demands": demands}
    if rng.random() < 0.9:
        payload["inventory"] = inventory
    elif inventory:
        payload["inventory"] = []
    return payload


def random_equal_priority_payload(rng, priority=1):
    """Valid payload where every demand shares one priority (tie-break focus)."""
    payload = random_payload(rng)
    for demand in payload["demands"]:
        demand["priority"] = priority
    return payload


def permuted(rng, payload):
    """Same records, different input order (deep-copied, lists shuffled)."""
    out = copy.deepcopy(payload)
    if isinstance(out.get("demands"), list):
        rng.shuffle(out["demands"])
    if isinstance(out.get("inventory"), list):
        rng.shuffle(out["inventory"])
    return out


# ---------------------------------------------------------------------------
# Fault injection: mutate a valid payload into a (usually) illegal one.
# ---------------------------------------------------------------------------

def _rows(payload, section):
    rows = payload.get(section)
    return rows if isinstance(rows, list) else []


def _drop_random_field(rng, payload):
    rows = _rows(payload, rng.choice(["demands", "inventory"]))
    if rows:
        row = rng.choice(rows)
        if isinstance(row, dict) and row:
            row.pop(rng.choice(list(row)), None)


def _corrupt_random_qty(rng, payload):
    rows = _rows(payload, rng.choice(["demands", "inventory"]))
    if not rows:
        return
    row = rng.choice(rows)
    if not isinstance(row, dict) or "qty" not in row:
        return
    row["qty"] = rng.choice([
        -1, -0.5, float("nan"), float("inf"), float("-inf"),
        True, "10", None, [1], {"v": 1},
    ])


def _corrupt_priority(rng, payload):
    rows = _rows(payload, "demands")
    if rows and isinstance(rows[0], dict):
        rows[0]["priority"] = rng.choice([float("nan"), float("inf"), "high", None, False])


def _corrupt_identifier(rng, payload):
    rows = _rows(payload, "demands")
    if rows and isinstance(rows[0], dict):
        rows[0]["order"] = rng.choice(["", 5, None, ["O1"]])


def _duplicate_order(rng, payload):
    rows = _rows(payload, "demands")
    if len(rows) >= 2 and all(isinstance(r, dict) for r in rows[:2]):
        rows[1]["order"] = rows[0].get("order", rows[1].get("order"))


def _non_dict_row(rng, payload):
    rows = payload.get(rng.choice(["demands", "inventory"]))
    if isinstance(rows, list) and rows:
        rows[rng.randrange(len(rows))] = rng.choice(["oops", 42, None, ["x"]])


def _section_not_list(rng, payload):
    payload[rng.choice(["demands", "inventory"])] = rng.choice([{}, "x", 7, None])


def _drop_section(rng, payload):
    payload.pop(rng.choice(["demands", "inventory"]), None)


FAULTS = (
    _drop_random_field,
    _corrupt_random_qty,
    _corrupt_priority,
    _corrupt_identifier,
    _duplicate_order,
    _non_dict_row,
    _section_not_list,
    _drop_section,  # sections are optional: this one keeps the payload valid
)


def random_fuzzed_payload(rng):
    """A valid payload with 1-2 faults injected; may stay valid (oracle decides)."""
    if rng.random() < 0.05:
        return rng.choice([None, [], "payload", 3.14])
    payload = random_payload(rng)
    for _ in range(rng.randint(1, 2)):
        rng.choice(FAULTS)(rng, payload)
    return payload


# ---------------------------------------------------------------------------
# Greedy counterexample shrinker (delta-debugging style).
# ---------------------------------------------------------------------------

def _number_candidates(value, floor):
    """Candidate simpler values between *value* and *floor*, simplest first."""
    if value == floor:
        return
    yield floor
    current = value
    for _ in range(8):
        midpoint = (current + floor) / 2
        if isinstance(value, int):
            midpoint = int(midpoint)
        if midpoint in (current, floor):
            break
        yield midpoint
        current = midpoint


def shrink(payload, fails):
    """Return a minimal payload that still satisfies ``fails``.

    ``fails(payload) -> bool`` must be True when the defect still reproduces.
    Candidates that raise inside ``fails`` count as not failing.  The shrinker
    only deletes records or moves numbers toward their floor, so valid inputs
    stay valid.
    """
    current = copy.deepcopy(payload)

    def still_fails(candidate):
        try:
            return bool(fails(candidate))
        except Exception:
            return False

    if not still_fails(current):
        return current

    changed = True
    while changed:
        changed = False
        # 1) delete records
        for key in ("demands", "inventory"):
            rows = current.get(key)
            if not isinstance(rows, list):
                continue
            i = 0
            while i < len(rows):
                candidate = copy.deepcopy(current)
                candidate[key].pop(i)
                if still_fails(candidate):
                    current = candidate
                    rows = current[key]
                    changed = True
                else:
                    i += 1
        # 2) move quantities toward 0 and priorities toward 1
        for key, field, floor in (
            ("demands", "qty", 0),
            ("inventory", "qty", 0),
            ("demands", "priority", 1),
        ):
            rows = current.get(key)
            if not isinstance(rows, list):
                continue
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or not isinstance(row.get(field), (int, float)):
                    continue
                if isinstance(row[field], bool):
                    continue
                for simpler in _number_candidates(row[field], floor):
                    if simpler == row[field]:
                        continue
                    candidate = copy.deepcopy(current)
                    candidate[key][index][field] = simpler
                    if still_fails(candidate):
                        current = candidate
                        changed = True
                        break
    return current
