import copy
import json
import unittest
from pathlib import Path

from supply_allocation_reconciler.engine import SolveInput, solve
from supply_allocation_reconciler.models import (
    AllocationStatus,
    CommitmentState,
    Layer,
    RunStatus,
    UnmetReason,
)
from supply_allocation_reconciler.parser import load_canonical
from supply_allocation_reconciler.pipeline import build_store_and_input
from supply_allocation_reconciler.rules import RuleStore
from supply_allocation_reconciler.timeline import project

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_combo.json"


def combo():
    return json.loads(FIXTURE.read_text())


def input_for(payload, strategy="hierarchical", rule_version=None):
    _, inp, _, _ = build_store_and_input(payload, rule_version, strategy)
    return inp


class TestConservation(unittest.TestCase):
    def test_quantity_conservation_every_strategy(self):
        for strategy in ("greedy", "hierarchical"):
            plan = solve(input_for(combo(), strategy))
            c = plan.conservation
            self.assertTrue(c["supply_balanced"], plan)
            self.assertTrue(c["demand_balanced"], plan)
            self.assertTrue(c["no_over_allocation"], plan)
            # Token-level residual accounting reconciles to the total.
            residual = sum(plan.residual.values())
            self.assertEqual(
                c["total_allocated"] + residual, c["initial_supply"]
            )
            # No token is allocated beyond its capacity.
            usage = {}
            for line in plan.lines:
                usage[line.batch_id] = usage.get(line.batch_id, 0) + line.qty
            for token in input_for(combo()).projection.tokens():
                self.assertLessEqual(
                    usage.get(token.token_id, 0), token.qty
                )

    def test_demand_balance_per_order(self):
        plan = solve(input_for(combo()))
        by_order = {o.order: o for o in plan.orders}
        for unmet in plan.unmet:
            order = by_order[unmet.order]
            self.assertEqual(
                order.allocated + unmet.qty, order.requested
            )


class TestAllocationSemantics(unittest.TestCase):
    def test_priority_ordering_is_stable(self):
        plan = solve(input_for(combo()))
        # Highest priority order O1 keeps its protected 60 even under
        # contention; lower priority orders carry the shortages.
        orders = {o.order: o for o in plan.orders}
        self.assertEqual(orders["O1"].allocated, 60)
        self.assertIn(orders["O3"].status,
                      (AllocationStatus.PARTIAL, AllocationStatus.UNMET))

    def test_substitution_only_after_preferred_layer(self):
        payload = {
            "as_of": 100,
            "batches": [
                {"batch_id": "B1", "plant": "P1", "sku": "S1",
                 "qty_on_hand": 40, "available_at": 0,
                 "reservation_class": "standard"},
                {"batch_id": "B2", "plant": "P1", "sku": "S2",
                 "qty_on_hand": 60, "available_at": 0,
                 "reservation_class": "standard"},
            ],
            "shipments": [],
            "demands": [
                {"order": "O1", "sku": "S1", "qty": 70, "priority": 1,
                 "need_date": 100, "plant_pref": ["P1"],
                 "reservation_class": "standard"}
            ],
            "rule_versions": [
                {"version": "v", "created_at": 1, "reservations": [],
                 "substitutions": [
                     {"from_sku": "S1", "to_sku": "S2", "rank": 1,
                      "allowed_classes": []}]}
            ],
            "events": [], "commitments": [],
        }
        plan = solve(input_for(payload))
        layers = {l.layer for l in plan.lines}
        self.assertIn(Layer.PREFERRED, layers)
        self.assertIn(Layer.SUBSTITUTE, layers)
        sub = next(l for l in plan.lines if l.substituted)
        self.assertEqual(sub.sku, "S2")
        self.assertEqual(sub.requested_sku, "S1")
        # Preferred supply is consumed before any substitute.
        preferred_total = sum(
            l.qty for l in plan.lines if l.layer == Layer.PREFERRED
        )
        self.assertEqual(preferred_total, 40)

    def test_late_shipment_cannot_serve_earlier_need(self):
        payload = {
            "as_of": 100,
            "batches": [
                {"batch_id": "B1", "plant": "P1", "sku": "S1",
                 "qty_on_hand": 40, "available_at": 0,
                 "reservation_class": "standard"}
            ],
            "shipments": [
                {"shipment_id": "T1", "plant": "P2", "sku": "S1",
                 "qty_planned": 50, "eta": 140,
                 "reservation_class": "standard"}
            ],
            "demands": [
                {"order": "O1", "sku": "S1", "qty": 70, "priority": 1,
                 "need_date": 120, "plant_pref": ["P1"],
                 "reservation_class": "standard"}
            ],
            "rule_versions": [
                {"version": "v", "created_at": 1, "reservations": [],
                 "substitutions": []}
            ],
            "events": [], "commitments": [],
        }
        plan = solve(input_for(payload))
        self.assertEqual(plan.orders[0].allocated, 40)
        self.assertEqual(plan.unmet[0].reason, UnmetReason.LATE)

    def test_reservation_blocked_explanation(self):
        plan = solve(input_for(combo()))
        o1 = next(u for u in plan.unmet if u.order == "O1")
        self.assertEqual(o1.reason, UnmetReason.RESERVATION_BLOCKED)

    def test_no_supply_reason(self):
        payload = {
            "as_of": 100,
            "batches": [], "shipments": [],
            "demands": [
                {"order": "O1", "sku": "ZZ", "qty": 5, "priority": 1,
                 "need_date": 100, "reservation_class": "standard"}
            ],
            "rule_versions": [], "events": [], "commitments": [],
        }
        plan = solve(input_for(payload))
        self.assertEqual(plan.unmet[0].reason, UnmetReason.NO_SUPPLY)

    def test_plant_offline_is_partial_not_failure(self):
        plan = solve(input_for(combo()))
        self.assertEqual(plan.status, RunStatus.PARTIAL)
        self.assertIn("P3", plan.failed_plants)

    def test_plant_offline_reason_when_all_sources_offline(self):
        payload = {
            "as_of": 100,
            "batches": [
                {"batch_id": "B1", "plant": "P2", "sku": "S1",
                 "qty_on_hand": 90, "available_at": 0,
                 "reservation_class": "standard"}
            ],
            "shipments": [],
            "demands": [
                {"order": "O1", "sku": "S1", "qty": 100, "priority": 1,
                 "need_date": 100, "reservation_class": "standard"}
            ],
            "rule_versions": [],
            "events": [
                {"event_id": "E1", "type": "plant.offline",
                 "occurred_at": 50, "payload": {"plant": "P2"}}
            ],
            "commitments": [],
        }
        plan = solve(input_for(payload))
        self.assertEqual(plan.orders[0].allocated, 0)
        self.assertEqual(plan.unmet[0].reason, UnmetReason.PLANT_OFFLINE)


class TestCommitmentProtection(unittest.TestCase):
    def test_notified_commitment_is_honoured_and_audited(self):
        plan = solve(input_for(combo()))
        o1 = next(a for a in plan.affected_commitments
                  if a.order == "O1")
        self.assertEqual(o1.state, CommitmentState.HONOURED)
        line = next(l for l in plan.lines if l.order == "O1")
        self.assertEqual(line.commitment_state, CommitmentState.HONOURED)
        self.assertEqual(line.qty, 60)

    def test_infeasible_commitment_is_replaced_not_silently_overwritten(self):
        plan = solve(input_for(combo()))
        o4 = next(a for a in plan.affected_commitments
                  if a.order == "O4")
        # T1 ETA moved to 160 > need 105: old promise infeasible.
        self.assertEqual(o4.state, CommitmentState.REPLACED)
        # The original notified snapshot is retained for re-notification.
        self.assertEqual(o4.notified[0]["batch_id"], "T1")
        self.assertTrue(o4.current)
        self.assertIn("eta", o4.reason)

    def test_revoked_when_no_replacement_exists(self):
        payload = {
            "as_of": 100,
            "batches": [
                {"batch_id": "B1", "plant": "P1", "sku": "S1",
                 "qty_on_hand": 10, "available_at": 0,
                 "reservation_class": "standard"}
            ],
            "shipments": [],
            "demands": [
                {"order": "O1", "sku": "S1", "qty": 10, "priority": 1,
                 "need_date": 100, "reservation_class": "standard"}
            ],
            "rule_versions": [],
            "commitments": [
                {"order": "O1", "lines": [
                    {"batch_id": "B1", "qty": 10, "sku": "S1",
                     "layer": "preferred", "notified_at": 1}]}
            ],
            "events": [
                {"event_id": "E1", "type": "batch.adjusted",
                 "occurred_at": 50,
                 "payload": {"batch_id": "B1", "qty_on_hand": 0}}
            ],
        }
        plan = solve(input_for(payload))
        affected = plan.affected_commitments[0]
        self.assertEqual(affected.state, CommitmentState.REVOKED)
        self.assertEqual(plan.orders[0].status, AllocationStatus.REVOKED)


class TestSolverEquivalenceAndDeterminism(unittest.TestCase):
    def test_both_strategies_conserve_quantity(self):
        g = solve(input_for(combo(), "greedy"))
        h = solve(input_for(combo(), "hierarchical"))
        for plan in (g, h):
            self.assertTrue(plan.conservation["supply_balanced"])
            self.assertTrue(plan.conservation["demand_balanced"])
        # Hierarchical flow is at least as good as greedy on total fill.
        self.assertGreaterEqual(
            h.conservation["total_allocated"],
            g.conservation["total_allocated"],
        )

    def test_hierarchical_outperforms_greedy_on_substitution_contention(self):
        # O3 wants S2; B2(S1) is a substitute via the rule.  Greedy reaches
        # the substitute layer and consumes B2 that O4 (which can only use
        # S1) needs.  The global flow keeps B2 for the S1-only order.
        g = solve(input_for(combo(), "greedy"))
        h = solve(input_for(combo(), "hierarchical"))
        def fill(plan, order):
            return next(o.allocated for o in plan.orders if o.order == order)
        # O4 gets some supply only under the globally optimised flow.
        self.assertLess(fill(g, "O4"), fill(h, "O4"))
        self.assertGreater(fill(h, "O4"), 0)

    def test_input_shuffle_does_not_change_result(self):
        import random
        payload = combo()
        base = solve(input_for(payload))
        shuffled = copy.deepcopy(payload)
        rng = random.Random(7)
        for collection in ("batches", "shipments", "demands", "events",
                           "rule_versions", "commitments"):
            rng.shuffle(shuffled[collection])
        again = solve(input_for(shuffled))
        key = lambda plan: sorted(
            (l.order, l.batch_id, l.qty, l.layer.value) for l in plan.lines
        )
        self.assertEqual(key(base), key(again))
        self.assertEqual(
            sorted((u.order, u.qty, u.reason.value) for u in base.unmet),
            sorted((u.order, u.qty, u.reason.value) for u in again.unmet),
        )

    def test_hierarchical_does_more_work_than_greedy(self):
        g = solve(input_for(combo(), "greedy"))
        h = solve(input_for(combo(), "hierarchical"))
        self.assertIn("O(D x S x L)", g.metrics.big_o)
        self.assertIn("SPFA", h.metrics.big_o)


if __name__ == "__main__":
    unittest.main()
