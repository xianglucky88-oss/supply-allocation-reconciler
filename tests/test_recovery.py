import json
import tempfile
import unittest
from pathlib import Path

from supply_allocation_reconciler.errors import (
    InterruptedRecompute,
    JournalCorrupted,
)
from supply_allocation_reconciler.pipeline import (
    compare_strategies,
    run_canonical,
    run_incremental,
    run_journaled,
)
from supply_allocation_reconciler.recovery import (
    FileJournalStore,
    JournalStore,
    PHASE_PROJECTED,
    PHASE_SOLVED,
    canonical_fingerprint,
    plan_fingerprint,
)
from supply_allocation_reconciler.timeline import Event

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_combo.json"


def combo():
    return json.loads(FIXTURE.read_text())


class TestJournalRecovery(unittest.TestCase):
    def setUp(self):
        self.payload = combo()
        self.tmp = tempfile.mkdtemp()
        self.store = FileJournalStore(self.tmp)

    def test_crash_after_projection_resumes(self):
        with self.assertRaises(InterruptedRecompute):
            run_journaled(
                self.payload, journal_id="J1", store=self.store,
                fail_after=PHASE_PROJECTED,
            )
        plan = run_journaled(
            self.payload, journal_id="J1", store=self.store
        )
        self.assertTrue(plan.resumed)
        self.assertTrue(plan.conservation["supply_balanced"])

    def test_crash_after_solve_keeps_durable_plan(self):
        with self.assertRaises(InterruptedRecompute):
            run_journaled(
                self.payload, journal_id="J2", store=self.store,
                fail_after=PHASE_SOLVED,
            )
        plan = run_journaled(
            self.payload, journal_id="J2", store=self.store
        )
        self.assertTrue(plan.resumed)
        self.assertEqual(plan.status.value, "partial")

    def test_idempotent_replay_returns_identical_plan(self):
        first = run_journaled(
            self.payload, journal_id="J3", store=self.store
        )
        second = run_journaled(
            self.payload, journal_id="J3", store=self.store
        )
        # The second call is served from the committed journal entry; only
        # the resume marker differs.
        self.assertFalse(first.resumed)
        self.assertTrue(second.resumed)
        d1, d2 = first.to_dict(), second.to_dict()
        self.assertEqual(d1.pop("resumed"), False)
        self.assertEqual(d2.pop("resumed"), True)
        self.assertEqual(d1, d2)
        self.assertEqual(plan_fingerprint(first), plan_fingerprint(second))

    def test_different_input_same_journal_does_not_replay_stale_plan(self):
        run_journaled(self.payload, journal_id="J4", store=self.store)
        changed = json.loads(json.dumps(self.payload))
        changed["demands"][0]["qty"] += 5
        # New fingerprint -> independent journal entry, fresh solve.
        fp1 = canonical_fingerprint(self.payload, "v2", "hierarchical")
        fp2 = canonical_fingerprint(changed, "v2", "hierarchical")
        self.assertNotEqual(fp1, fp2)
        plan = run_journaled(changed, journal_id="J4", store=self.store)
        self.assertFalse(plan.resumed)

    def test_in_memory_store_supports_same_semantics(self):
        store = JournalStore()
        with self.assertRaises(InterruptedRecompute):
            run_journaled(
                self.payload, journal_id="JM", store=store,
                fail_after=PHASE_PROJECTED,
            )
        plan = run_journaled(self.payload, journal_id="JM", store=store)
        self.assertTrue(plan.resumed)


class TestIncrementalRecompute(unittest.TestCase):
    def test_cancellation_releases_supply_to_short_order(self):
        payload = {
            "as_of": 100,
            "batches": [
                {"batch_id": "B1", "plant": "P1", "sku": "S1",
                 "qty_on_hand": 50, "available_at": 0,
                 "reservation_class": "standard"}
            ],
            "shipments": [],
            "demands": [
                {"order": "O1", "sku": "S1", "qty": 30, "priority": 1,
                 "need_date": 100, "reservation_class": "standard"},
                {"order": "O2", "sku": "S1", "qty": 30, "priority": 2,
                 "need_date": 100, "reservation_class": "standard"},
            ],
            "rule_versions": [], "events": [], "commitments": [],
        }
        prior = run_canonical(payload)
        self.assertEqual(
            next(o for o in prior.orders if o.order == "O2").allocated, 20
        )
        new_payload = json.loads(json.dumps(payload))
        new_payload["events"] = [
            {"event_id": "C1", "type": "demand.cancelled",
             "occurred_at": 90, "payload": {"order": "O1"}}
        ]
        plan, impacted = run_incremental(
            new_payload, prior=prior,
            new_events=[Event("C1", "demand.cancelled", 90,
                              {"order": "O1"})],
        )
        self.assertIn("O1", impacted)
        self.assertIn("O2", impacted)  # previously short -> re-opened
        o2 = next(o for o in plan.orders if o.order == "O2")
        self.assertEqual(o2.allocated, 30)
        self.assertTrue(plan.conservation["supply_balanced"])
        # Equivalent to a full solve.
        full = run_canonical(new_payload)
        self.assertEqual(
            sorted((l.order, l.batch_id, l.qty) for l in plan.lines),
            sorted((l.order, l.batch_id, l.qty) for l in full.lines),
        )

    def test_unrelated_orders_keep_their_frozen_lines(self):
        payload = combo()
        no_cancel = json.loads(json.dumps(payload))
        no_cancel["events"] = [
            e for e in no_cancel["events"] if e["event_id"] != "E5"
        ]
        prior = run_canonical(no_cancel)
        plan, impacted = run_incremental(
            payload, prior=prior,
            new_events=[Event("E5", "demand.cancelled", 85,
                              {"order": "O5"})],
        )
        # The cancelled order is always in the impact set; previously short
        # orders are also re-opened so they can claim any released supply.
        self.assertIn("O5", impacted)
        # Every line belonging to an untouched order is frozen verbatim.
        prior_lines = {
            (l.order, l.batch_id, l.qty)
            for l in prior.lines if l.order not in impacted
        }
        new_lines = {
            (l.order, l.batch_id, l.qty) for l in plan.lines
        }
        self.assertTrue(prior_lines.issubset(new_lines))
        full = run_canonical(payload)
        self.assertEqual(
            plan.conservation["total_allocated"],
            full.conservation["total_allocated"],
        )
        self.assertTrue(plan.conservation["supply_balanced"])

    def test_delay_event_reopens_affected_owner(self):
        payload = {
            "as_of": 100,
            "batches": [
                {"batch_id": "B1", "plant": "P1", "sku": "S1",
                 "qty_on_hand": 100, "available_at": 0,
                 "reservation_class": "standard"}
            ],
            "shipments": [
                {"shipment_id": "T1", "plant": "P1", "sku": "S1",
                 "qty_planned": 50, "eta": 90,
                 "reservation_class": "standard"}
            ],
            "demands": [
                {"order": "O1", "sku": "S1", "qty": 120, "priority": 1,
                 "need_date": 95, "reservation_class": "standard"}
            ],
            "rule_versions": [],
            "events": [], "commitments": [],
        }
        prior = run_canonical(payload)
        new_payload = json.loads(json.dumps(payload))
        new_payload["events"] = [
            {"event_id": "D1", "type": "shipment.delayed",
             "occurred_at": 80, "payload": {"shipment_id": "T1", "eta": 200}}
        ]
        plan, impacted = run_incremental(
            new_payload, prior=prior,
            new_events=[Event("D1", "shipment.delayed", 80,
                              {"shipment_id": "T1", "eta": 200})],
        )
        self.assertIn("O1", impacted)
        # Shipment now arrives after need date -> only the 100 on hand count.
        self.assertEqual(plan.orders[0].allocated, 100)
        self.assertEqual(plan.unmet[0].reason.value, "late")


class TestStrategyComparison(unittest.TestCase):
    def test_compare_reports_all_three_strategies(self):
        report = compare_strategies(combo())
        for key in ("greedy", "hierarchical", "incremental"):
            self.assertIn(key, report)
            self.assertTrue(
                report[key]["conservation"]["supply_balanced"]
            )
        self.assertTrue(report["all_conservation_checked"])
        self.assertTrue(report["same_total_allocation"])
        # Incremental run reports the impact set.
        self.assertGreaterEqual(report["incremental"]["impacted_count"], 1)
        self.assertIn("big_o", report["greedy"]["metrics"])


if __name__ == "__main__":
    unittest.main()
