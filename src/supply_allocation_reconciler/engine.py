"""Domain calculation only: greedy priority allocation over pooled inventory.

Semantics (the contract the QA oracle independently re-implements):

- demands are processed in ascending ``(priority, order)`` order, so the
  tie-break between equal priorities is deterministic and the result does not
  depend on the input ordering of records;
- each demand is granted as much of the remaining pool as it requests
  (partial fulfillment is allowed); a zero-quantity demand is a cancelled
  order: it is allocated nothing and is owed nothing;
- any shortfall is reported exactly once per order with a structured reason;
- inventory is accounted per SKU (duplicate records are partial arrivals and
  sum); consumption is attributed to SKUs in ascending ``sku`` order so the
  report can trace every allocated and remaining unit back to its SKU.

Input is assumed validated by the parser (pipeline boundary).
"""

UNMET_REASON_INSUFFICIENT_INVENTORY = "insufficient_inventory"


def calculate(demands, inventory):
    received = {}
    for rec in inventory:
        sku = rec["sku"]
        received[sku] = received.get(sku, 0) + rec["qty"]

    remaining = dict(received)
    allocations = []
    unmet = []
    for demand in sorted(demands, key=lambda d: (d["priority"], d["order"])):
        want = demand["qty"]
        need = want
        for sku in sorted(remaining):
            if need == 0:
                break
            take = remaining[sku] if remaining[sku] < need else need
            remaining[sku] -= take
            need -= take
        granted = want - need
        allocations.append({
            "order": demand["order"],
            "requested": want,
            "allocated": granted,
        })
        if need > 0:
            unmet.append({
                "order": demand["order"],
                "unmet": need,
                "reason": UNMET_REASON_INSUFFICIENT_INVENTORY,
            })

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
        "remaining_inventory": sum(remaining.values()),
        "inventory_by_sku": inventory_by_sku,
    }
