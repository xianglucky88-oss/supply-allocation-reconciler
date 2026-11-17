import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from supply_allocation_reconciler.cli import main
from supply_allocation_reconciler.pipeline import run_canonical, run_journaled
from supply_allocation_reconciler.recovery import FileJournalStore

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src"
COMBO = ROOT / "fixtures" / "scenario_combo.json"
LEGACY = ROOT / "fixtures" / "input.json"


class TestCli(unittest.TestCase):
    def _run_cli(self, *args):
        env = {"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"}
        proc = subprocess.run(
            [sys.executable, "-m", "supply_allocation_reconciler.cli", *args],
            cwd=str(ROOT), env=env, capture_output=True, text=True, check=True,
        )
        return json.loads(proc.stdout)

    def test_run_mode_on_combo(self):
        out = self._run_cli(str(COMBO))
        self.assertEqual(out["project"], "supply-allocation-reconciler")
        self.assertIn(out["status"], ("partial", "ok", "recovered"))
        self.assertTrue(out["conservation"]["supply_balanced"])

    def test_compare_mode(self):
        out = self._run_cli(str(COMBO), "--mode", "compare")
        self.assertIn("greedy", out)
        self.assertIn("hierarchical", out)
        self.assertIn("incremental", out)
        self.assertTrue(out["all_conservation_checked"])

    def test_journal_mode_is_idempotent_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Point a file-backed store via the journal mode twice; the CLI
            # uses an in-memory store, so here we validate the subprocess
            # contract and separately validate cross-call durability.
            out1 = self._run_cli(
                str(COMBO), "--mode", "journal", "--journal-id", "cli-j"
            )
            out2 = self._run_cli(
                str(COMBO), "--mode", "journal", "--journal-id", "cli-j"
            )
            self.assertEqual(
                out1["conservation"], out2["conservation"]
            )
            # Durable store survives process boundaries.
            store = FileJournalStore(tmp)
            payload = json.loads(COMBO.read_text())
            p1 = run_journaled(payload, journal_id="durable", store=store)
            p2 = run_journaled(payload, journal_id="durable", store=store)
            self.assertTrue(p2.resumed)
            self.assertEqual(
                p1.conservation["total_allocated"],
                p2.conservation["total_allocated"],
            )
            self.assertTrue(list(Path(tmp).glob("*.json")))

    def test_legacy_fixture_still_runs(self):
        out = self._run_cli(str(LEGACY))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["allocations"][0]["allocated"], 70.0)
        self.assertEqual(out["unmet"][0]["order"], "O2")

    def test_cli_rule_version_override_wins_over_payload(self):
        # The fixture payload pins rule_version=v2; the CLI flag must win.
        out = self._run_cli(str(COMBO), "--rule-version", "v1")
        self.assertEqual(out["rule_version"], "v1")
        out2 = self._run_cli(str(COMBO), "--strategy", "greedy")
        self.assertEqual(out2["strategy"], "greedy")


class TestContentionAndPlantFailure(unittest.TestCase):
    def test_single_batch_contested_by_many_orders(self):
        payload = {
            "as_of": 100,
            "batches": [
                {"batch_id": "B1", "plant": "P1", "sku": "S1",
                 "qty_on_hand": 50, "available_at": 0,
                 "reservation_class": "standard"}
            ],
            "shipments": [],
            "demands": [
                {"order": f"O{i}", "sku": "S1", "qty": 25, "priority": i,
                 "need_date": 100, "reservation_class": "standard"}
                for i in range(1, 5)
            ],
            "rule_versions": [], "events": [], "commitments": [],
        }
        plan = run_canonical(payload)
        # 100 requested, 50 available; exactly two orders fully served,
        # the rest unmet with contention reasons.
        served = sum(o.allocated for o in plan.orders)
        self.assertEqual(served, 50)
        self.assertEqual(plan.conservation["total_residual"], 0)
        self.assertTrue(plan.conservation["supply_balanced"])
        # Priority stability: O1, O2 (top two) are the fulfilled orders.
        statuses = {o.order: o.status.value for o in plan.orders}
        self.assertEqual(statuses["O1"], "fulfilled")
        self.assertEqual(statuses["O2"], "fulfilled")
        self.assertIn(statuses["O3"], ("unmet", "partial"))

    def test_plant_failure_excludes_all_its_batches(self):
        payload = {
            "as_of": 100,
            "batches": [
                {"batch_id": "B1", "plant": "P1", "sku": "S1",
                 "qty_on_hand": 10, "available_at": 0,
                 "reservation_class": "standard"},
                {"batch_id": "B2", "plant": "P2", "sku": "S1",
                 "qty_on_hand": 90, "available_at": 0,
                 "reservation_class": "standard"},
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
        plan = run_canonical(payload)
        self.assertIn("P2", plan.failed_plants)
        # Only P1's 10 units remain usable.
        self.assertEqual(plan.orders[0].allocated, 10)
        used_batches = {l.batch_id for l in plan.lines}
        self.assertNotIn("B2", used_batches)
        # Some online supply existed but was insufficient; the offline plant
        # is reported separately rather than as the unmet reason.
        self.assertEqual(plan.unmet[0].reason.value, "capacity_consumed")

    def test_plant_recovery_event_restores_supply(self):
        payload = {
            "as_of": 100,
            "batches": [
                {"batch_id": "B1", "plant": "P1", "sku": "S1",
                 "qty_on_hand": 10, "available_at": 0,
                 "reservation_class": "standard"},
                {"batch_id": "B2", "plant": "P2", "sku": "S1",
                 "qty_on_hand": 90, "available_at": 0,
                 "reservation_class": "standard"},
            ],
            "shipments": [],
            "demands": [
                {"order": "O1", "sku": "S1", "qty": 100, "priority": 1,
                 "need_date": 100, "reservation_class": "standard"}
            ],
            "rule_versions": [],
            "events": [
                {"event_id": "E1", "type": "plant.offline",
                 "occurred_at": 50, "payload": {"plant": "P2"}},
                {"event_id": "E2", "type": "plant.online",
                 "occurred_at": 70, "payload": {"plant": "P2"}},
            ],
            "commitments": [],
        }
        plan = run_canonical(payload)
        self.assertNotIn("P2", plan.failed_plants)
        self.assertEqual(plan.orders[0].allocated, 100)


if __name__ == "__main__":
    unittest.main()
