"""Independent reference oracle for the allocation semantics.

This module re-implements the specification from scratch so the QA layer never
relies on the code under test.  It deliberately uses a different computation
strategy than ``engine.calculate``: grants are decided against a scalar pool
first and only afterwards attributed to SKU buckets, while the engine walks
SKU buckets per demand.  Both must agree exactly.

It also hosts the structural invariant checker used by the property,
differential and mutation tests.  The invariants are checked from the input
and output alone (not by re-running the allocation), so they catch
discrepancies that a pure output-to-output diff could rationalize away.
"""

import json
import math

PROJECT = "supply-allocation-reconciler"
REASON_INSUFFICIENT = "insufficient_inventory"

RESULT_KEYS = {"project", "status", "allocations", "unmet", "remaining_inventory", "inventory_by_sku"}
ALLOCATION_KEYS = {"order", "requested", "allocated"}
UNMET_KEYS = {"order", "unmet", "reason"}
SKU_KEYS = {"sku", "received", "allocated", "remaining"}


# ---------------------------------------------------------------------------
# Validation spec (mirrors the parser contract, returns problems instead of
# raising so fault-injection tests can diff acceptance behaviour).
# ---------------------------------------------------------------------------

def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_quantity(value):
    return _is_number(value) and math.isfinite(value) and value >= 0


def _is_priority(value):
    return _is_number(value) and math.isfinite(value)


def _is_identifier(value):
    return isinstance(value, str) and value != ""


def problems(payload):
    """Return the list of spec violations for *payload* ([] means valid)."""
    found = []
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]

    demands = payload.get("demands", [])
    if not isinstance(demands, list):
        found.append("demands must be a list")
    else:
        seen = set()
        for i, row in enumerate(demands):
            path = "demands[%d]" % i
            if not isinstance(row, dict):
                found.append("%s must be a demand object" % path)
                continue
            for field in ("order", "priority", "qty"):
                if field not in row:
                    found.append("%s.%s missing" % (path, field))
            if "order" in row:
                if not _is_identifier(row["order"]):
                    found.append("%s.order must be a non-empty string" % path)
                elif row["order"] in seen:
                    found.append("%s.order duplicate %r" % (path, row["order"]))
                else:
                    seen.add(row["order"])
            if "priority" in row and not _is_priority(row["priority"]):
                found.append("%s.priority must be a finite number" % path)
            if "qty" in row and not _is_quantity(row["qty"]):
                found.append("%s.qty must be a finite number >= 0" % path)

    inventory = payload.get("inventory", [])
    if not isinstance(inventory, list):
        found.append("inventory must be a list")
    else:
        for i, row in enumerate(inventory):
            path = "inventory[%d]" % i
            if not isinstance(row, dict):
                found.append("%s must be an inventory record object" % path)
                continue
            for field in ("sku", "qty"):
                if field not in row:
                    found.append("%s.%s missing" % (path, field))
            if "sku" in row and not _is_identifier(row["sku"]):
                found.append("%s.sku must be a non-empty string" % path)
            if "qty" in row and not _is_quantity(row["qty"]):
                found.append("%s.qty must be a finite number >= 0" % path)
    return found


def normalize(payload):
    """Return (demands, inventory) for a valid payload."""
    demands = [
        {"order": row["order"], "priority": row["priority"], "qty": row["qty"]}
        for row in payload.get("demands", [])
    ]
    inventory = [
        {"sku": row["sku"], "qty": row["qty"]}
        for row in payload.get("inventory", [])
    ]
    return demands, inventory


# ---------------------------------------------------------------------------
# Reference allocation (independent of the engine's code path).
# ---------------------------------------------------------------------------

def allocate(demands, inventory):
    received = {}
    for rec in inventory:
        received[rec["sku"]] = received.get(rec["sku"], 0) + rec["qty"]

    pool = sum(received.values())
    allocations = []
    unmet = []
    for demand in sorted(demands, key=lambda d: (d["priority"], d["order"])):
        want = demand["qty"]
        granted = want if want < pool else pool
        pool -= granted
        allocations.append({"order": demand["order"], "requested": want, "allocated": granted})
        if granted < want:
            unmet.append({"order": demand["order"], "unmet": want - granted, "reason": REASON_INSUFFICIENT})

    # Attribute the consumed pool to SKUs in ascending sku order (traceability).
    consumed = sum(received.values()) - pool
    remaining = {}
    for sku in sorted(received):
        take = received[sku] if received[sku] < consumed else consumed
        remaining[sku] = received[sku] - take
        consumed -= take
    inventory_by_sku = [
        {
            "sku": sku,
            "received": received[sku],
            "allocated": received[sku] - remaining[sku],
            "remaining": remaining[sku],
        }
        for sku in sorted(received)
    ]
    return {
        "allocations": allocations,
        "unmet": unmet,
        "remaining_inventory": pool,
        "inventory_by_sku": inventory_by_sku,
    }


def run(payload):
    """Reference pipeline: envelope included, same contract as report.build_result."""
    demands, inventory = normalize(payload)
    return {"project": PROJECT, "status": "ok", **allocate(demands, inventory)}


# ---------------------------------------------------------------------------
# Structural invariants.  Each check raises AssertionError with a focused
# message; check_result runs them all.
# ---------------------------------------------------------------------------

def _sorted_demands(payload):
    demands, _ = normalize(payload)
    return sorted(demands, key=lambda d: (d["priority"], d["order"]))


def check_envelope(payload, result):
    assert isinstance(result, dict), "result must be a dict"
    assert set(result) == RESULT_KEYS, "envelope keys drifted: %s" % sorted(result)
    assert result["project"] == PROJECT, "wrong project: %r" % result["project"]
    assert result["status"] == "ok", "wrong status: %r" % result["status"]
    # the report must be strict JSON (no NaN/Infinity may leak into it)
    json.loads(json.dumps(result, allow_nan=False))


def check_order_traceability(payload, result):
    demands = _sorted_demands(payload)
    expected_orders = [d["order"] for d in demands]
    actual_orders = [a["order"] for a in result["allocations"]]
    assert len(set(actual_orders)) == len(actual_orders), "duplicate order in allocations"
    assert actual_orders == expected_orders, (
        "allocations must cover every order exactly once, in (priority, order) "
        "processing order: %s != %s" % (actual_orders, expected_orders))
    for entry in result["allocations"]:
        assert set(entry) == ALLOCATION_KEYS, "allocation keys drifted: %s" % sorted(entry)


def check_order_conservation(payload, result):
    """allocated + unmet == requested for every order; nothing invented, nothing hidden."""
    demands = _sorted_demands(payload)
    unmet_by_order = {}
    for entry in result["unmet"]:
        assert set(entry) == UNMET_KEYS, "unmet keys drifted: %s" % sorted(entry)
        assert entry["order"] not in unmet_by_order, "duplicate unmet for %r" % entry["order"]
        unmet_by_order[entry["order"]] = entry
    for demand, alloc in zip(demands, result["allocations"]):
        requested = demand["qty"]
        assert alloc["requested"] == requested, "requested rewritten for %r" % demand["order"]
        allocated = alloc["allocated"]
        assert 0 <= allocated <= requested, "allocated out of bounds for %r" % demand["order"]
        shortfall = requested - allocated
        if shortfall > 0:
            entry = unmet_by_order.get(demand["order"])
            assert entry is not None, "hidden unmet commitment for %r" % demand["order"]
            assert entry["unmet"] == shortfall, "unmet amount wrong for %r" % demand["order"]
            assert entry["reason"] == REASON_INSUFFICIENT, (
                "infeasible order %r lacks structured reason" % demand["order"])
        else:
            assert demand["order"] not in unmet_by_order, (
                "phantom unmet entry for fully satisfied %r" % demand["order"])
    assert len(unmet_by_order) == sum(1 for d, a in zip(demands, result["allocations"])
                                      if a["allocated"] < d["qty"]), "unmet list size mismatch"


def check_sku_conservation(payload, result):
    """Per-SKU and global inventory conservation; no oversell."""
    _, inventory = normalize(payload)
    received = {}
    for rec in inventory:
        received[rec["sku"]] = received.get(rec["sku"], 0) + rec["qty"]
    by_sku = result["inventory_by_sku"]
    assert [row["sku"] for row in by_sku] == sorted(received), (
        "inventory_by_sku must cover every sku exactly once, sorted")
    for row in by_sku:
        assert set(row) == SKU_KEYS, "sku row keys drifted: %s" % sorted(row)
        assert row["received"] == received[row["sku"]], "received wrong for sku %r" % row["sku"]
        assert row["allocated"] >= 0, "negative allocation for sku %r" % row["sku"]
        assert row["remaining"] >= 0, "oversold sku %r" % row["sku"]
        assert row["received"] == row["allocated"] + row["remaining"], (
            "sku %r does not conserve inventory" % row["sku"])
    total_received = sum(received.values())
    total_allocated = sum(a["allocated"] for a in result["allocations"])
    assert result["remaining_inventory"] >= 0, "negative remaining inventory (oversell)"
    assert total_allocated <= total_received, "allocated more than received (oversell)"
    assert total_allocated + result["remaining_inventory"] == total_received, (
        "global inventory not conserved: allocated %r + remaining %r != received %r"
        % (total_allocated, result["remaining_inventory"], total_received))
    assert result["remaining_inventory"] == sum(row["remaining"] for row in by_sku), (
        "remaining_inventory does not match the per-sku breakdown")


def check_priority_respected(payload, result):
    """Once an order is shorted, no later (lower-priority) order may receive stock."""
    shorted_seen = False
    for alloc in result["allocations"]:
        if shorted_seen:
            assert alloc["allocated"] == 0, (
                "order %r received stock after an earlier order was shorted" % alloc["order"])
        if alloc["allocated"] < alloc["requested"]:
            shorted_seen = True


def check_result(payload, result):
    check_envelope(payload, result)
    check_order_traceability(payload, result)
    check_order_conservation(payload, result)
    check_sku_conservation(payload, result)
    check_priority_respected(payload, result)
