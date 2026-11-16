import json
import unittest
from pathlib import Path
from supply_allocation_reconciler.pipeline import run
from supply_allocation_reconciler.parser import load_records
from supply_allocation_reconciler.engine import calculate

def fixture():
    return json.loads((Path(__file__).parents[1] / "fixtures" / "input.json").read_text())

class TestCore(unittest.TestCase):
    def test_pipeline_and_modules(self):
        payload = fixture()
        rows = load_records(payload)
        self.assertTrue(rows)
        data = calculate(rows, payload)
        self.assertIsInstance(data, dict)
        self.assertTrue(data)
        result = run(payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["project"], 'supply-allocation-reconciler')

        assert result["allocations"][0]["allocated"] == 70 and result["unmet"][0]["order"] == "O2"
