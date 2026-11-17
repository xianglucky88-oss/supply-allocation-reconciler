"""Mutation testing: prove the QA suite catches sorting, grouping, quantity
and report-wrapping defects.

Each mutant rewrites one anchored line of a source module *in memory* (the
files on disk are never touched), the mutated module is wired into a pipeline
with the real remaining modules, and the kill suite (oracle invariants over
canonical + randomized payloads, reorder invariance, and the fault-injection
table) must reject it.  Any surviving mutant fails this test and points at a
hole in the QA layer.
"""

import json
import random
import types
import unittest
from pathlib import Path

from supply_allocation_reconciler import engine, parser, report

from qa import generators, oracle
from qa.test_fault_injection import ILLEGAL_CASES

SRC_DIR = Path(__file__).parents[2] / "src" / "supply_allocation_reconciler"
SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
SEED = 20261117
RANDOM_CASES = 120

# name, category, source file, anchor (exactly one occurrence), replacement
MUTANTS = [
    ("E1-tie-break-dropped", "sorting", "engine.py",
     "key=lambda d: (d[\"priority\"], d[\"order\"])",
     "key=lambda d: d[\"priority\"]"),
    ("E2-over-allocates-sku", "quantity", "engine.py",
     "take = remaining[sku] if remaining[sku] < need else need",
     "take = remaining[sku]"),
    ("E3-phantom-unmet", "quantity", "engine.py",
     "if need > 0:",
     "if need >= 0:"),
    ("E4-reversed-priority", "sorting", "engine.py",
     "sorted(demands, key=lambda d: (d[\"priority\"], d[\"order\"]))",
     "sorted(demands, key=lambda d: (d[\"priority\"], d[\"order\"]), reverse=True)"),
    ("E5-inventory-created", "quantity", "engine.py",
     "remaining[sku] -= take",
     "remaining[sku] += take"),
    ("E6-unmet-hidden", "quantity", "engine.py",
     "granted = want - need",
     "granted = want"),
    ("E7-reason-dropped", "report", "engine.py",
     "UNMET_REASON_INSUFFICIENT_INVENTORY = \"insufficient_inventory\"",
     "UNMET_REASON_INSUFFICIENT_INVENTORY = \"unknown\""),
    ("E8-sku-remaining-wrong", "quantity", "engine.py",
     "\"remaining\": remaining[sku],",
     "\"remaining\": received[sku],"),
    ("E9-arrivals-overwritten", "grouping", "engine.py",
     "received[sku] = received.get(sku, 0) + rec[\"qty\"]",
     "received[sku] = rec[\"qty\"]"),
    ("P1-zero-qty-rejected", "quantity", "parser.py",
     "if value < 0:",
     "if value <= 0:"),
    ("P2-duplicate-orders-allowed", "grouping", "parser.py",
     "if order in seen:",
     "if False:"),
    ("P3-nonfinite-accepted", "quantity", "parser.py",
     "if not math.isfinite(value):",
     "if False:"),
    ("P4-rows-silently-dropped", "report", "parser.py",
     "if not isinstance(row, dict):\n            raise ValueError(f\"{path}: must be a demand object\")",
     "if not isinstance(row, dict):\n            continue"),
    ("R1-status-renamed", "report", "report.py",
     "\"status\": \"ok\"",
     "\"status\": \"success\""),
    ("R2-status-dropped", "report", "report.py",
     "return {\"project\": result.project, \"status\": \"ok\", **result.data}",
     "return {\"project\": result.project, **result.data}"),
    ("PL1-arguments-swapped", "orchestration", "pipeline.py",
     "calculate(demands, inventory)",
     "calculate(inventory, demands)"),
]


def load_source(filename):
    return (SRC_DIR / filename).read_text()


def apply_mutant(filename, anchor, replacement):
    source = load_source(filename)
    assert source.count(anchor) == 1, "anchor %r must occur exactly once in %s" % (anchor, filename)
    return source.replace(anchor, replacement)


def exec_module(filename, source):
    module = types.ModuleType("qa_mutant_" + filename.replace(".", "_"))
    module.__dict__["__package__"] = "supply_allocation_reconciler"
    exec(compile(source, filename, "exec"), module.__dict__)
    return module


def make_pipeline(parser_mod, engine_mod, report_mod):
    def run_pipeline(payload):
        demands = parser_mod.load_records(payload)
        inventory = parser_mod.load_inventory(payload)
        return report_mod.build_result(
            "supply-allocation-reconciler", engine_mod.calculate(demands, inventory))
    return run_pipeline


def canonical_payloads():
    return [json.loads(path.read_text())["input"] for path in sorted(SNAPSHOT_DIR.glob("*.json"))]


def kill_suite(run_pipeline):
    """Return the list of kill-suite failures for a candidate pipeline."""
    failures = []

    # 1) valid payloads: every structural invariant + exact oracle agreement
    rng = random.Random(SEED)
    payloads = canonical_payloads() + [generators.random_payload(rng) for _ in range(RANDOM_CASES)]
    for payload in payloads:
        try:
            result = run_pipeline(payload)
            oracle.check_result(payload, result)
            assert result == oracle.run(payload), "result diverges from oracle"
        except Exception as exc:  # noqa: BLE001 - any failure kills
            failures.append("valid payload rejected/miscomputed: %s" % exc)
            break

    # 2) reorder invariance
    perm_rng = random.Random(SEED + 1)
    reorder_pool = canonical_payloads()[:3] + [generators.random_payload(perm_rng) for _ in range(20)]
    for payload in reorder_pool:
        try:
            baseline = run_pipeline(payload)
            for _ in range(3):
                assert run_pipeline(generators.permuted(perm_rng, payload)) == baseline, (
                    "reordering the input changed the result")
        except Exception as exc:  # noqa: BLE001
            failures.append("reorder invariance broken: %s" % exc)
            break

    # 3) fault injection: illegal payloads must raise ValueError, never be accepted
    for name, payload, _fragment in ILLEGAL_CASES:
        try:
            run_pipeline(payload)
        except ValueError:
            continue
        except Exception as exc:  # noqa: BLE001
            failures.append("fault %s raised unstructured %r" % (name, exc))
            break
        failures.append("fault %s was silently accepted" % name)
        break

    # 4) determinism
    for payload in canonical_payloads()[:5]:
        try:
            assert run_pipeline(payload) == run_pipeline(payload), "non-deterministic result"
        except Exception as exc:  # noqa: BLE001
            failures.append("determinism broken: %s" % exc)
            break

    return failures


class TestMutation(unittest.TestCase):
    def test_baseline_kill_suite_is_clean(self):
        failures = kill_suite(make_pipeline(parser, engine, report))
        self.assertEqual(failures, [], "kill suite must pass on the unmutated code")

    def test_all_mutants_are_killed(self):
        survivors = []
        for name, category, filename, anchor, replacement in MUTANTS:
            with self.subTest(mutant=name, category=category):
                mutated_source = apply_mutant(filename, anchor, replacement)
                mutated = exec_module(filename, mutated_source)
                if filename == "engine.py":
                    run_pipeline = make_pipeline(parser, mutated, report)
                elif filename == "parser.py":
                    run_pipeline = make_pipeline(mutated, engine, report)
                elif filename == "report.py":
                    run_pipeline = make_pipeline(parser, engine, mutated)
                elif filename == "pipeline.py":
                    run_pipeline = mutated.run
                else:  # pragma: no cover - defensive
                    raise AssertionError("no wiring for %s" % filename)
                failures = kill_suite(run_pipeline)
                if not failures:
                    survivors.append(name)
        self.assertEqual(
            survivors, [],
            "mutants survived the QA suite (test holes): %s" % ", ".join(survivors))


if __name__ == "__main__":
    unittest.main()
