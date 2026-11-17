"""Property-based tests over randomized valid payloads.

Every property runs over seeded pseudo-random payloads (deterministic, no
network).  On violation the payload is shrunk with the greedy delta-debugging
shrinker and the *minimal* counterexample is reported.
"""

import json
import random
import unittest

from supply_allocation_reconciler.pipeline import run

from qa import generators, oracle

SEED = 20261117
CASES = 250


def _violates(check, payload):
    try:
        check(payload)
    except Exception:
        return True
    return False


class PropertyTestCase(unittest.TestCase):
    def assert_property(self, check, seed=SEED, cases=CASES, payload_fn=None):
        rng = random.Random(seed)
        produce = payload_fn or generators.random_payload
        for case_no in range(cases):
            payload = produce(rng)
            try:
                check(payload)
            except Exception as exc:  # noqa: BLE001 - report any violation
                minimal = generators.shrink(payload, lambda p: _violates(check, p))
                try:
                    check(minimal)
                    detail = "re-check of minimal case unexpectedly passed"
                except Exception as exc2:  # noqa: BLE001
                    detail = str(exc2)
                self.fail(
                    "property violated on case %d (seed %d).\n"
                    "minimal counterexample:\n%s\nviolation: %s"
                    % (case_no, seed, json.dumps(minimal, indent=2, sort_keys=True, default=str), detail)
                )


class TestAllocationProperties(PropertyTestCase):
    def test_sku_inventory_conservation(self):
        def check(payload):
            oracle.check_sku_conservation(payload, run(payload))
        self.assert_property(check)

    def test_order_allocated_plus_unmet_equals_requested(self):
        def check(payload):
            oracle.check_order_conservation(payload, run(payload))
        self.assert_property(check)

    def test_no_oversell_and_nonnegative_remaining(self):
        def check(payload):
            result = run(payload)
            _, inventory = oracle.normalize(payload)
            total = sum(rec["qty"] for rec in inventory)
            allocated = sum(a["allocated"] for a in result["allocations"])
            assert allocated <= total, "oversell: allocated %r > received %r" % (allocated, total)
            assert result["remaining_inventory"] >= 0, "negative remaining inventory"
            for row in result["inventory_by_sku"]:
                assert row["remaining"] >= 0, "oversold sku %r" % row["sku"]
        self.assert_property(check)

    def test_no_hidden_unmet_commitments(self):
        def check(payload):
            result = run(payload)
            unmet_orders = {u["order"] for u in result["unmet"]}
            for alloc in result["allocations"]:
                shorted = alloc["allocated"] < alloc["requested"]
                assert shorted == (alloc["order"] in unmet_orders), (
                    "unmet visibility mismatch for %r" % alloc["order"])
        self.assert_property(check)

    def test_stable_tie_break_for_equal_priorities(self):
        def check(payload):
            result = run(payload)
            processed = [a["order"] for a in result["allocations"]]
            assert processed == sorted(processed), (
                "equal priorities must be tie-broken by ascending order id: %s" % processed)
            assert result == oracle.run(payload), "tie-break result diverges from oracle"
        self.assert_property(check, payload_fn=generators.random_equal_priority_payload)

    def test_input_reorder_does_not_change_result(self):
        rng = random.Random(SEED + 1)

        def check(payload):
            baseline = run(payload)
            for _ in range(3):
                shuffled = generators.permuted(rng, payload)
                assert run(shuffled) == baseline, "reordering the input changed the result"
        self.assert_property(check)

    def test_priority_respected(self):
        def check(payload):
            oracle.check_priority_respected(payload, run(payload))
        self.assert_property(check)

    def test_determinism(self):
        def check(payload):
            assert run(payload) == run(payload), "same input produced different results"
        self.assert_property(check)

    def test_report_traceable_to_orders_and_skus(self):
        def check(payload):
            oracle.check_order_traceability(payload, run(payload))
            result = run(payload)
            demands, inventory = oracle.normalize(payload)
            orders = {d["order"] for d in demands}
            skus = {rec["sku"] for rec in inventory}
            assert {a["order"] for a in result["allocations"]} == orders
            assert {u["order"] for u in result["unmet"]} <= orders
            assert {row["sku"] for row in result["inventory_by_sku"]} == skus
        self.assert_property(check)

    def test_report_envelope_and_strict_json(self):
        def check(payload):
            oracle.check_envelope(payload, run(payload))
        self.assert_property(check)


class TestShrinker(unittest.TestCase):
    """The shrinker itself: must terminate and produce 1-minimal failures."""

    def test_always_failing_predicate_terminates_at_empty_payload(self):
        rng = random.Random(SEED + 2)
        payload = generators.random_payload(rng)
        minimal = generators.shrink(payload, lambda p: True)
        self.assertEqual(minimal.get("demands", []), [])
        self.assertEqual(minimal.get("inventory", []), [])

    def test_shrinks_to_one_minimal_failing_core(self):
        payload = {
            "demands": [
                {"order": "OA", "priority": 3, "qty": 40},
                {"order": "OB", "priority": 2, "qty": 15},
                {"order": "OC", "priority": 1, "qty": 7},
            ],
            "inventory": [{"sku": "S1", "qty": 100}],
        }
        # fails while at least two demands survive: deletions that keep two
        # demands are accepted, everything else is shrunk away
        minimal = generators.shrink(payload, lambda p: len(p.get("demands", [])) > 1)
        self.assertEqual(len(minimal["demands"]), 2)
        self.assertEqual(minimal.get("inventory", []), [])
        self.assertTrue(all(d["qty"] == 0 for d in minimal["demands"]))
        self.assertTrue(all(d["priority"] == 1 for d in minimal["demands"]))


if __name__ == "__main__":
    unittest.main()
