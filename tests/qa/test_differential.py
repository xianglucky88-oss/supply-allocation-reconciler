"""Differential tests (pipeline vs. independent oracle) and golden snapshots.

Snapshots live in ``tests/qa/snapshots/*.json`` as ``{"name", "input",
"output"}`` documents.  Regenerate them deliberately with:

    SAR_REGEN_SNAPSHOTS=1 PYTHONPATH=src python -m unittest discover -s tests
"""

import json
import os
import random
import subprocess
import sys
import unittest
from pathlib import Path

from supply_allocation_reconciler.pipeline import run

from qa import generators, oracle

SEED = 20261201
CASES = 500
SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
REGEN = os.environ.get("SAR_REGEN_SNAPSHOTS") == "1"
REPO_ROOT = Path(__file__).parents[2]


class TestDifferential(unittest.TestCase):
    def test_pipeline_matches_oracle(self):
        rng = random.Random(SEED)
        for case_no in range(CASES):
            payload = generators.random_payload(rng)
            with self.subTest(case=case_no):
                self.assertEqual(run(payload), oracle.run(payload))

    def test_reordered_input_still_matches_oracle(self):
        rng = random.Random(SEED + 1)
        for case_no in range(100):
            payload = generators.random_payload(rng)
            shuffled = generators.permuted(rng, payload)
            with self.subTest(case=case_no):
                self.assertEqual(run(shuffled), oracle.run(payload))

    def test_snapshots(self):
        snapshots = sorted(SNAPSHOT_DIR.glob("*.json"))
        self.assertTrue(snapshots, "no snapshots found")
        for path in snapshots:
            doc = json.loads(path.read_text())
            with self.subTest(snapshot=path.name):
                self.assertEqual(oracle.problems(doc["input"]), [],
                                 "snapshot input must be a valid payload")
                actual = run(doc["input"])
                if REGEN:
                    doc["output"] = actual
                    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
                self.assertEqual(doc["output"], actual,
                                 "snapshot %s drifted" % path.name)

    def test_cli_is_deterministic_and_matches_pipeline(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        fixture = REPO_ROOT / "fixtures" / "input.json"
        outputs = []
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, "-m", "supply_allocation_reconciler.cli", str(fixture)],
                capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), check=True,
            )
            outputs.append(proc.stdout)
        self.assertEqual(outputs[0], outputs[1], "CLI output is not deterministic")
        expected = run(json.loads(fixture.read_text()))
        self.assertEqual(json.loads(outputs[0]), expected)


if __name__ == "__main__":
    unittest.main()
