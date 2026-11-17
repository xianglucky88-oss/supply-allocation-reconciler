import json
import unittest
from pathlib import Path

from supply_allocation_reconciler.errors import (
    UnknownReferenceError,
    ValidationError,
)
from supply_allocation_reconciler.parser import (
    is_legacy,
    load_canonical,
    upgrade_legacy,
)
from supply_allocation_reconciler.timeline import Event, project

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_combo.json"


def combo():
    return json.loads(FIXTURE.read_text())


def fold(parsed, *, events=None):
    return project(
        as_of=parsed["as_of"],
        batches=parsed["batches"],
        shipments=parsed["shipments"],
        demands=parsed["demands"],
        events=parsed["events"] if events is None else events,
        commitments=parsed["commitments"],
        plants_offline=parsed["plants_offline"],
    )


class TestParser(unittest.TestCase):
    def test_canonical_parse(self):
        parsed = load_canonical(combo())
        self.assertEqual(parsed["as_of"], 100)
        self.assertEqual(len(parsed["batches"]), 5)
        self.assertEqual(len(parsed["demands"]), 5)

    def test_legacy_detection_and_upgrade(self):
        legacy = {"demands": [{"order": "O1", "priority": 1, "qty": 70}],
                  "inventory": [{"sku": "S1", "qty": 100}]}
        self.assertTrue(is_legacy(legacy))
        upgraded = upgrade_legacy(legacy)
        self.assertEqual(upgraded["batches"][0]["sku"], "S1")
        # Legacy demand without sku inherits the inventory sku.
        self.assertEqual(upgraded["demands"][0]["sku"], "S1")
        parsed = load_canonical(upgraded)
        self.assertEqual(parsed["demands"][0].sku, "S1")

    def test_validation_failures_become_validation_error(self):
        with self.assertRaises(ValidationError):
            load_canonical({"as_of": 1, "demands": [{"order": "O1"}]})
        with self.assertRaises(ValidationError):
            load_canonical({"as_of": 1, "demands": [
                {"order": "O1", "sku": "S", "qty": 0, "priority": 1,
                 "need_date": 1}]})

    def test_duplicate_identifiers_rejected(self):
        bad = {"as_of": 1, "batches": [
            {"batch_id": "B", "plant": "P", "sku": "S", "qty_on_hand": 1},
            {"batch_id": "B", "plant": "P", "sku": "S", "qty_on_hand": 1},
        ]}
        with self.assertRaises(ValidationError):
            load_canonical(bad)


class TestTimeline(unittest.TestCase):
    def setUp(self):
        self.parsed = load_canonical(combo())

    def test_duplicate_event_applied_once_order_independent(self):
        events = list(self.parsed["events"])
        proj = fold(self.parsed)
        # E1 appears twice in the fixture; only the canonical one applies.
        self.assertIn("E1", proj.duplicate_events)
        self.assertEqual(proj.shipments["T1"].qty_received, 40)
        # Shuffling the log must not change the projection.
        shuffled = events[::-1]
        proj2 = fold(self.parsed, events=shuffled)
        self.assertEqual(proj2.shipments["T1"].qty_received, 40)
        self.assertEqual(sorted(proj2.duplicate_events),
                         sorted(proj.duplicate_events))
        self.assertEqual(
            {b: v.qty_on_hand for b, v in proj2.batches.items()},
            {b: v.qty_on_hand for b, v in proj.batches.items()},
        )

    def test_partial_arrival_accumulates_and_delays_eta(self):
        proj = fold(self.parsed)
        t1 = proj.shipments["T1"]
        self.assertEqual(t1.qty_received, 40)      # 20 base + E1 20
        self.assertEqual(t1.open_qty, 10)
        self.assertEqual(t1.eta, 160)               # E2 delay
        t2 = proj.shipments["T2"]
        self.assertEqual(t2.qty_received, 15)       # E3

    def test_over_receipt_rejected(self):
        bad = [Event("E", "shipment.partial_arrival", 1,
                     {"shipment_id": "T1", "qty": 999})]
        with self.assertRaises(ValidationError):
            fold(self.parsed, events=bad)

    def test_eta_cannot_move_earlier(self):
        bad = [Event("E", "shipment.delayed", 1,
                     {"shipment_id": "T1", "eta": 5})]
        with self.assertRaises(ValidationError):
            fold(self.parsed, events=bad)

    def test_unknown_event_reference(self):
        bad = [Event("E", "batch.expired", 1, {"batch_id": "GHOST"})]
        with self.assertRaises(UnknownReferenceError):
            fold(self.parsed, events=bad)

    def test_cancel_and_plant_offline_and_expiry(self):
        proj = fold(self.parsed)
        self.assertTrue(proj.demands["O5"].cancelled)
        self.assertIn("P3", proj.plants_offline)
        # B5 expired at 95 and as_of=100 -> no token emitted.
        token_ids = {t.token_id for t in proj.tokens()}
        self.assertNotIn("B5", token_ids)
        # P3 offline -> B4 token suppressed even though on hand.
        self.assertNotIn("B4", token_ids)

    def test_tokens_are_time_gated_not_a_single_pool(self):
        # T1 eta 160 > as_of 100 still appears as a token with future
        # available_at; eligibility (not projection) decides usability.
        proj = fold(self.parsed)
        t1 = next(t for t in proj.tokens() if t.token_id == "T1")
        self.assertEqual(t1.available_at, 160)
        self.assertEqual(t1.qty, 10)

    def test_batch_adjustment_bumps_version(self):
        proj = fold(self.parsed)
        self.assertEqual(proj.batches["B3"].qty_on_hand, 25)
        self.assertGreaterEqual(proj.batches["B3"].version, 2)


if __name__ == "__main__":
    unittest.main()
