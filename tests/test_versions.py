import json
import unittest
from pathlib import Path

from supply_allocation_reconciler.errors import (
    UnknownRuleVersionError,
    ValidationError,
)
from supply_allocation_reconciler.models import Layer
from supply_allocation_reconciler.pipeline import replay, run_canonical
from supply_allocation_reconciler.rules import RuleStore
from supply_allocation_reconciler.models import RuleVersion, SubstituteEdge

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_combo.json"


def substitution_payload():
    return {
        "as_of": 100,
        "batches": [
            {"batch_id": "B1", "plant": "P1", "sku": "S1",
             "qty_on_hand": 40, "available_at": 0,
             "reservation_class": "standard"},
            {"batch_id": "B2", "plant": "P1", "sku": "S2",
             "qty_on_hand": 60, "available_at": 0,
             "reservation_class": "standard"},
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
            {"version": "v1", "created_at": 1, "reservations": [],
             "substitutions": []},
            {"version": "v2", "created_at": 2, "reservations": [],
             "substitutions": [
                 {"from_sku": "S1", "to_sku": "S2", "rank": 1,
                  "allowed_classes": []}]}
        ],
        "events": [], "commitments": [],
    }


class TestRuleStore(unittest.TestCase):
    def test_publish_is_immutable(self):
        store = RuleStore()
        store.publish(RuleVersion(version="v1"))
        with self.assertRaises(ValidationError):
            store.publish(RuleVersion(version="v1"))

    def test_unknown_version_raises(self):
        store = RuleStore([RuleVersion(version="v1")])
        with self.assertRaises(UnknownRuleVersionError):
            store.get("nope")
        self.assertEqual(store.latest().version, "v1")


class TestVersionReplay(unittest.TestCase):
    def setUp(self):
        self.payload = substitution_payload()

    def test_replay_under_each_version_is_deterministic(self):
        v1a = replay(self.payload, rule_version="v1")
        v1b = replay(self.payload, rule_version="v1")
        self.assertEqual(
            [(l.order, l.batch_id, l.qty, l.layer.value) for l in v1a.lines],
            [(l.order, l.batch_id, l.qty, l.layer.value) for l in v1b.lines],
        )
        self.assertEqual(v1a.rule_version, "v1")

    def test_rule_upgrade_changes_feasible_solution(self):
        v1 = replay(self.payload, rule_version="v1")
        v2 = replay(self.payload, rule_version="v2")
        # v1: only 40 of exact SKU usable in window; shipment is late.
        self.assertEqual(v1.orders[0].allocated, 40)
        self.assertTrue(
            all(l.layer != Layer.SUBSTITUTE for l in v1.lines)
        )
        # v2: substitute S2 closes the remaining 30 units.
        self.assertEqual(v2.orders[0].allocated, 70)
        self.assertTrue(any(l.substituted for l in v2.lines))
        self.assertEqual(v2.rule_version, "v2")

    def test_replay_unknown_version_is_failed_envelope(self):
        from supply_allocation_reconciler.pipeline import safe_run
        env = safe_run({**self.payload, "rule_version": "ghost"})
        self.assertEqual(env["status"], "failed")
        self.assertEqual(env["error"]["type"], "UnknownRuleVersionError")

    def test_combo_fixture_replays_both_versions(self):
        payload = json.loads(FIXTURE.read_text())
        for version in ("v1", "v2"):
            plan = replay(payload, rule_version=version)
            self.assertEqual(plan.rule_version, version)
            self.assertTrue(plan.conservation["supply_balanced"])


class TestReservationRelaxation(unittest.TestCase):
    def _payload(self, allow_relaxation):
        return {
            "as_of": 100,
            "batches": [
                {"batch_id": "B1", "plant": "P1", "sku": "S1",
                 "qty_on_hand": 20, "available_at": 0,
                 "reservation_class": "gold"},
            ],
            "shipments": [],
            "demands": [
                {"order": "O1", "sku": "S1", "qty": 20, "priority": 1,
                 "need_date": 100, "reservation_class": "standard"}
            ],
            "rule_versions": [
                {"version": "strict", "created_at": 1,
                 "reservations": [
                     {"sku": "S1", "reservation_class": "gold",
                      "allow_relaxation": False}],
                 "substitutions": []},
                {"version": "relaxed", "created_at": 2,
                 "reservations": [
                     {"sku": "S1", "reservation_class": "gold",
                      "allow_relaxation": True}],
                 "substitutions": []},
            ],
            "events": [], "commitments": [],
        }

    def test_relaxation_flag_toggles_feasibility(self):
        strict = replay(self._payload(False), rule_version="strict")
        relaxed = replay(self._payload(True), rule_version="relaxed")
        self.assertEqual(strict.orders[0].allocated, 0)
        self.assertEqual(
            strict.unmet[0].reason.value, "reservation_blocked"
        )
        self.assertEqual(relaxed.orders[0].allocated, 20)
        self.assertIn(
            Layer.RELAXED,
            [l.layer for l in relaxed.lines],
        )


if __name__ == "__main__":
    unittest.main()
