"""Deterministic JSON command-line interface."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .errors import AllocationError
from .pipeline import (
    compare_strategies,
    envelope,
    failure_envelope,
    run_canonical,
    run_journaled,
    safe_run,
)

DEFAULT_FIXTURE = (
    Path(__file__).parents[2] / "fixtures" / "scenario_combo.json"
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Versioned, explainable supply allocation recomputation with "
            "idempotent recovery."
        )
    )
    parser.add_argument(
        "input", nargs="?",
        default=str(DEFAULT_FIXTURE if DEFAULT_FIXTURE.exists()
                    else Path(__file__).parents[2] / "fixtures" / "input.json"),
    )
    parser.add_argument(
        "--mode", choices=("run", "compare", "journal"), default="run",
        help="run: single allocation; compare: strategy cost comparison; "
             "journal: idempotent journaled recompute.",
    )
    parser.add_argument("--journal-id", default="cli-journal")
    parser.add_argument(
        "--strategy", default=None,
        help="greedy | hierarchical (overrides payload strategy)",
    )
    parser.add_argument(
        "--rule-version", default=None,
        help="replay under any published rule version",
    )
    args = parser.parse_args()

    payload = json.loads(Path(args.input).read_text())

    if args.mode == "compare":
        output = compare_strategies(payload)
    elif args.mode == "journal":
        try:
            plan = run_journaled(
                payload, journal_id=args.journal_id,
                strategy=args.strategy, rule_version=args.rule_version,
            )
            output = envelope(plan)
        except AllocationError as exc:
            output = failure_envelope(exc)
    else:
        try:
            if args.strategy is not None or args.rule_version is not None:
                output = envelope(
                    run_canonical(
                        payload,
                        strategy=args.strategy,
                        rule_version=args.rule_version,
                    )
                )
            else:
                output = safe_run(payload)
        except Exception as exc:  # pragma: no cover - defensive CLI boundary
            output = failure_envelope(exc)

    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
