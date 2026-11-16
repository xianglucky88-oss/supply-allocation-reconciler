# Supply Allocation Reconciler

Allocate constrained inventory to demand while explaining unmet commitments.

## Modules

- `src/supply_allocation_reconciler/models.py`: immutable result contract.
- `src/supply_allocation_reconciler/parser.py`: input validation and record extraction.
- `src/supply_allocation_reconciler/engine.py`: domain calculation only.
- `src/supply_allocation_reconciler/report.py`: stable output envelope.
- `src/supply_allocation_reconciler/pipeline.py`: orchestration boundary.
- `src/supply_allocation_reconciler/cli.py`: deterministic JSON CLI.

## Run

```bash
PYTHONPATH=src python -m supply_allocation_reconciler.cli
PYTHONPATH=src python -m unittest discover -s tests -v
```

The fixture is local and deterministic; no network or credentials are required.
