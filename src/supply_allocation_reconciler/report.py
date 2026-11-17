"""Stable output envelope helpers."""
from __future__ import annotations

from typing import Any, Dict

from .models import AllocationPlan, PipelineResult


def build_result(project: str, data: dict) -> dict:
    """Legacy flat-shape envelope (kept for the original fixture contract)."""
    result = PipelineResult(project=project, data=data)
    return {"project": result.project, "status": "ok", **result.data}


def build_plan_envelope(plan: AllocationPlan) -> Dict[str, Any]:
    return {"project": "supply-allocation-reconciler", **plan.to_dict()}
