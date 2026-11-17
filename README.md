# Supply Allocation Reconciler

Versioned, explainable, recoverable supply-allocation recomputation.

The original program sorted orders by `priority` and subtracted from one
global inventory pool. It could not say *which batch* an order received, why
an order was short, how a late shipment or a cancellation changed another
order, or how to replay an old decision under an old rule set. This version
puts every entity onto one integer time axis and produces an auditable plan
with conservation guarantees.

## What it does

* **Timeline, not a single pool.** On-hand batches, in-transit shipments,
  plant health, demand and reservation rules are folded from an append-only
  event log into one projection (`timeline.py`). A supply *token* only exists
  once its availability/ETA time has been reached and before it expires, so
  stock arriving tomorrow cannot serve an order due today.
* **Per-order explanation.** Every output order carries the batches/lots it
  received, allocated quantity, layer used (`preferred` → `substitute` →
  `relaxed`), plant/time/substitution evidence and the rule version applied.
  Short orders carry a machine-readable reason: `no_supply`, `late`,
  `expired`, `plant_offline`, `reservation_blocked`, `capacity_consumed`,
  `cancelled`.
* **Versioned rules, arbitrary replay.** Reservation and substitution policy
  are immutable `RuleVersion` objects. Replaying under any historical version
  is a deterministic lookup + re-solve; a rule upgrade can change feasibility.
* **Protected customer promises.** Already-notified commitments are
  pre-allocated atomically. If a promise becomes infeasible it is never
  silently overwritten: the plan marks it `replaced` or `revoked`, keeps the
  notified snapshot for re-notification, and proposes a new plan separately.
* **Idempotent, recoverable recompute.** A write-ahead journal persists three
  phases (`projected → solved → committed`) with content hashes. A crash is
  retried with the same `journal_id` and resumes at the next phase; duplicate
  events are applied exactly once.
* **Incremental recompute.** Only the *impact set* (orders touching changed
  batches/shipments/plants, the cancelled order, and every previously short
  order) is re-solved; untouched allocation lines are frozen and their supply
  reserved. The merged result is identical to a full solve.

## Modules

| Module | Responsibility |
| --- | --- |
| `models.py` | Immutable domain entities, enumerations, output contract |
| `errors.py` | Explicit failure/partial-success exception boundary |
| `timeline.py` | Event fold, duplicate suppression, time-gated supply tokens |
| `rules.py` | Immutable, versioned reservation + substitution store |
| `parser.py` | Canonical validation + legacy single-pool input upgrade |
| `eligibility.py` | Shared eligibility predicate and unmet-reason explanation |
| `flow.py` | Min-cost max-flow (SPFA) for the hierarchical solver |
| `engine.py` | Greedy & hierarchical solvers, commitment protection, conservation |
| `recovery.py` | WAL journal, crash resume, impact set, incremental recompute |
| `pipeline.py` | Orchestration, strategy comparison, plan (de)serialisation |
| `report.py` / `cli.py` | Stable JSON envelope and deterministic CLI |

## Solver cost comparison

| Strategy | Mechanism | Time cost | Space | Optimality |
| --- | --- | --- | --- | --- |
| Greedy | Orders by priority, layers in order, first-fit tokens | `O(D × S × L)` | `O(S)` | Locally optimal; can consume a token a constrained order needed |
| Hierarchical | Min-cost max-flow per layer; costs encode priority/plant/time/substitute rank | `O(L × SPFA(D+S, D×S))` | `O(D+S)` | Globally optimal for the lexicographic objective |
| Recoverable incremental | Re-solve only the impacted set `I`, freeze the rest | `O(I × S × L)`; journal replay `O(1)` | `O(S)` | Same answer as a full solve; far less work when `I ≪ D` |

`D` = active demands, `S` = supply tokens, `L` = 3 resolution layers,
`I` = impacted orders. The CLI `--mode compare` emits measured iteration
counts alongside these bounds. Under an identical canonical ordering both
full solvers conserve quantity, but the flow solver can route supply to an
order the greedy solver starves (see
`tests/test_engine.py::test_hierarchical_outperforms_greedy_on_substitution_contention`).

## Failure / partial-success boundary

* **`failed`** — malformed input, unknown entity/rule-version reference,
  corrupted journal. A `failed` envelope contains an error and **no**
  allocation plan; nothing is committed.
* **`partial`** — a valid plan that cannot satisfy every active order, or
  that runs while one or more plants are offline. Conservation still holds
  and every shortfall is explained.
* **`ok` / `recovered`** — every active demand fully met; `recovered` means
  the answer was produced by resuming an interrupted journal.

## Run

```bash
PYTHONPATH=src python3 -m supply_allocation_reconciler.cli \
    fixtures/scenario_combo.json                 # single allocation
PYTHONPATH=src python3 -m supply_allocation_reconciler.cli \
    fixtures/scenario_combo.json --mode compare  # strategy cost comparison
PYTHONPATH=src python3 -m supply_allocation_reconciler.cli \
    fixtures/scenario_combo.json --mode journal  # idempotent journaled run
PYTHONPATH=src python3 -m supply_allocation_reconciler.cli \
    fixtures/scenario_combo.json --rule-version v1   # replay old rules
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The fixtures are local and deterministic; no network or credentials are
required. The original flat `fixtures/input.json` shape still works and is
upgraded to the canonical contract automatically.

## Combined scenario coverage

`fixtures/scenario_combo.json` exercises, in one event log: batch
substitution, partial arrivals, an ETA delay that invalidates a notified
commitment, a cancellation that releases supply, a batch expiry, an offline
plant, reservation-class blocking, a duplicate event (applied once), and
replay across two rule versions. The tests assert quantity conservation,
priority stability, input-order independence and explanation consistency for
all three strategies.
