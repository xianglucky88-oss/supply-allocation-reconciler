# Supply Allocation Reconciler

Allocate constrained inventory to demand while explaining unmet commitments.

## Modules

- `src/supply_allocation_reconciler/models.py`: immutable result contract.
- `src/supply_allocation_reconciler/parser.py`: input validation and record extraction.
- `src/supply_allocation_reconciler/engine.py`: domain calculation only.
- `src/supply_allocation_reconciler/report.py`: stable output envelope.
- `src/supply_allocation_reconciler/pipeline.py`: orchestration boundary.
- `src/supply_allocation_reconciler/cli.py`: deterministic JSON CLI.

## Allocation semantics

- Demands are processed in ascending `(priority, order)` order; equal
  priorities are tie-broken by order id, so the result does not depend on the
  input ordering of records.
- Each demand is granted as much of the remaining pool as it requests
  (partial fulfillment allowed). A zero-quantity demand is a cancelled order:
  allocated nothing, owed nothing.
- Every shortfall is reported once per order with a structured `reason`
  (`insufficient_inventory`); `allocated + unmet == requested` per order.
- Inventory is conserved per SKU: duplicate `sku` records are partial
  arrivals and sum; `received == allocated + remaining` per SKU and in total;
  allocated never exceeds received.
- Illegal input (negative/NaN/infinite/non-numeric quantities, duplicate
  order ids, non-dict rows, ...) is rejected by the parser with a structured
  `ValueError` naming the offending record; nothing is silently dropped.

## Run

```bash
PYTHONPATH=src python -m supply_allocation_reconciler.cli
PYTHONPATH=src python -m unittest discover -s tests -v
```

The fixture is local and deterministic; no network or credentials are required.

## QA layer (`tests/qa/`)

Independent quality-assurance layer proving the semantics above:

- `oracle.py`: reference oracle re-implementing the semantics independently
  (scalar-pool grant + SKU attribution) plus structural invariant checkers
  (SKU conservation, order allocated+unmet sums, tie-break stability,
  traceability of the report to orders and SKUs, strict-JSON envelope).
- `generators.py`: seeded random payload generators, fault injectors, and a
  greedy delta-debugging shrinker that reduces any property violation to a
  minimal counterexample.
- `test_properties.py`: property-based tests (conservation, no oversell, no
  hidden unmet, stable tie-break, input-reorder invariance, priority respect,
  determinism, report traceability/envelope) with automatic shrinking.
- `test_differential.py`: pipeline vs. oracle over randomized and reordered
  inputs; golden snapshots in `tests/qa/snapshots/`; CLI determinism.
  Regenerate snapshots deliberately with `SAR_REGEN_SNAPSHOTS=1`.
- `test_fault_injection.py`: structured rejection of illegal payloads
  (negative/NaN/boolean/string quantities, duplicates, non-dict rows, ...),
  randomized fault fuzzing cross-checked against the oracle's verdict, and
  edge-case semantics (zero inventory, cancellations, partial arrivals).
- `test_mutation.py`: mutation testing — 16 in-memory mutants of the engine,
  parser, report and pipeline (sorting, grouping, quantity and
  report-wrapping defects) must all be killed by the QA suite; survivors
  indicate test holes and fail the build.
