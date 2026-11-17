"""Fault injection: illegal inputs must fail with structured ValueErrors,
edge-case semantics must hold, and nothing may crash with an unstructured
exception or silently drop commitments.
"""

import random
import unittest

from supply_allocation_reconciler.pipeline import run

from qa import generators, oracle

SEED = 20270105
FUZZ_CASES = 400

NAN = float("nan")
INF = float("inf")

# (name, payload, message fragment the structured ValueError must contain)
ILLEGAL_CASES = [
    ("payload_not_dict_list", [], "payload"),
    ("payload_not_dict_none", None, "payload"),
    ("demands_not_list", {"demands": {}}, "demands"),
    ("inventory_not_list", {"demands": [], "inventory": "x"}, "inventory"),
    ("demand_row_not_dict", {"demands": ["oops"]}, "demands[0]"),
    ("inventory_row_not_dict", {"inventory": [42]}, "inventory[0]"),
    ("demand_missing_qty", {"demands": [{"order": "O1", "priority": 1}]}, "qty"),
    ("demand_missing_order", {"demands": [{"priority": 1, "qty": 1}]}, "order"),
    ("demand_missing_priority", {"demands": [{"order": "O1", "qty": 1}]}, "priority"),
    ("inventory_missing_sku", {"inventory": [{"qty": 1}]}, "sku"),
    ("inventory_missing_qty", {"inventory": [{"sku": "S1"}]}, "qty"),
    ("negative_demand_qty", {"demands": [{"order": "O1", "priority": 1, "qty": -1}]}, "demands[0].qty"),
    ("negative_inventory_qty", {"inventory": [{"sku": "S1", "qty": -5}]}, "inventory[0].qty"),
    ("nan_demand_qty", {"demands": [{"order": "O1", "priority": 1, "qty": NAN}]}, "finite"),
    ("inf_inventory_qty", {"inventory": [{"sku": "S1", "qty": INF}]}, "finite"),
    ("bool_demand_qty", {"demands": [{"order": "O1", "priority": 1, "qty": True}]}, "quantity"),
    ("string_demand_qty", {"demands": [{"order": "O1", "priority": 1, "qty": "10"}]}, "quantity"),
    ("nan_priority", {"demands": [{"order": "O1", "priority": NAN, "qty": 1}]}, "priority"),
    ("string_priority", {"demands": [{"order": "O1", "priority": "high", "qty": 1}]}, "priority"),
    ("bool_priority", {"demands": [{"order": "O1", "priority": False, "qty": 1}]}, "priority"),
    ("empty_order_id", {"demands": [{"order": "", "priority": 1, "qty": 1}]}, "order"),
    ("non_string_order", {"demands": [{"order": 7, "priority": 1, "qty": 1}]}, "order"),
    ("empty_sku", {"inventory": [{"sku": "", "qty": 1}]}, "sku"),
    ("non_string_sku", {"inventory": [{"sku": 3, "qty": 1}]}, "sku"),
    ("duplicate_order", {
        "demands": [
            {"order": "O1", "priority": 1, "qty": 1},
            {"order": "O1", "priority": 2, "qty": 2},
        ],
    }, "duplicate"),
]


class TestFaultInjection(unittest.TestCase):
    def test_illegal_payloads_raise_structured_value_error(self):
        for name, payload, fragment in ILLEGAL_CASES:
            with self.subTest(case=name):
                with self.assertRaises(ValueError) as ctx:
                    run(payload)
                message = str(ctx.exception)
                self.assertIn(fragment, message, "error should name the offending record")
                self.assertRegex(message, r"^(payload|demands|inventory)")

    def test_fuzzed_payloads_never_crash_and_match_oracle_verdict(self):
        rng = random.Random(SEED)
        for case_no in range(FUZZ_CASES):
            payload = generators.random_fuzzed_payload(rng)
            with self.subTest(case=case_no):
                expected_problems = oracle.problems(payload)
                if expected_problems:
                    with self.assertRaises(ValueError, msg=(
                            "oracle rejects (%s) but pipeline accepted: %r"
                            % (expected_problems, payload))):
                        run(payload)
                else:
                    result = run(payload)  # must not raise
                    oracle.check_result(payload, result)

    def test_zero_inventory_everything_unmet_with_reason(self):
        result = run({
            "demands": [
                {"order": "O1", "priority": 1, "qty": 10},
                {"order": "O2", "priority": 2, "qty": 5},
            ],
            "inventory": [],
        })
        self.assertEqual([a["allocated"] for a in result["allocations"]], [0, 0])
        self.assertEqual(
            result["unmet"],
            [
                {"order": "O1", "unmet": 10, "reason": "insufficient_inventory"},
                {"order": "O2", "unmet": 5, "reason": "insufficient_inventory"},
            ],
        )
        self.assertEqual(result["remaining_inventory"], 0)

    def test_zero_quantity_demand_is_a_cancellation(self):
        result = run({
            "demands": [
                {"order": "O1", "priority": 1, "qty": 0},
                {"order": "O2", "priority": 2, "qty": 10},
            ],
            "inventory": [{"sku": "S1", "qty": 10}],
        })
        cancelled, other = result["allocations"]
        self.assertEqual((cancelled["order"], cancelled["allocated"]), ("O1", 0))
        self.assertEqual(other["allocated"], 10)
        self.assertEqual(result["unmet"], [], "a cancelled order is owed nothing")
        self.assertEqual(result["remaining_inventory"], 0)

    def test_duplicate_inventory_records_are_partial_arrivals(self):
        result = run({
            "demands": [{"order": "O1", "priority": 1, "qty": 25}],
            "inventory": [
                {"sku": "S1", "qty": 10},
                {"sku": "S1", "qty": 10},
                {"sku": "S2", "qty": 8},
            ],
        })
        self.assertEqual(result["allocations"][0]["allocated"], 25)
        by_sku = {row["sku"]: row for row in result["inventory_by_sku"]}
        self.assertEqual(by_sku["S1"]["received"], 20, "partial arrivals must sum per sku")
        self.assertEqual(by_sku["S1"]["remaining"], 0)
        self.assertEqual(by_sku["S2"]["remaining"], 3)
        self.assertEqual(result["remaining_inventory"], 3)

    def test_zero_quantity_inventory_record_stays_traceable(self):
        result = run({
            "demands": [{"order": "O1", "priority": 1, "qty": 1}],
            "inventory": [{"sku": "S1", "qty": 0}, {"sku": "S2", "qty": 1}],
        })
        by_sku = {row["sku"]: row for row in result["inventory_by_sku"]}
        self.assertEqual(by_sku["S1"], {"sku": "S1", "received": 0, "allocated": 0, "remaining": 0})
        self.assertEqual(by_sku["S2"]["allocated"], 1)

    def test_huge_quantities_stay_exact(self):
        big = 10 ** 30
        result = run({
            "demands": [{"order": "O1", "priority": 1, "qty": big + 7}],
            "inventory": [{"sku": "S1", "qty": big}],
        })
        self.assertEqual(result["allocations"][0]["allocated"], big)
        self.assertEqual(result["unmet"][0]["unmet"], 7)
        self.assertEqual(result["remaining_inventory"], 0)

    def test_unknown_keys_are_tolerated(self):
        result = run({
            "demands": [{"order": "O1", "priority": 1, "qty": 2, "note": "vip"}],
            "inventory": [{"sku": "S1", "qty": 5, "warehouse": "W1"}],
            "meta": {"source": "erp"},
        })
        self.assertEqual(result["allocations"][0]["allocated"], 2)
        self.assertEqual(result["remaining_inventory"], 3)

    def test_missing_sections_default_to_empty(self):
        result = run({"demands": [{"order": "O1", "priority": 1, "qty": 4}]})
        self.assertEqual(result["unmet"][0]["unmet"], 4)
        result = run({"inventory": [{"sku": "S1", "qty": 4}]})
        self.assertEqual(result["allocations"], [])
        self.assertEqual(result["remaining_inventory"], 4)


if __name__ == "__main__":
    unittest.main()
