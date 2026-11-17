from .parser import load_records, load_inventory
from .engine import calculate
from .report import build_result

PROJECT = 'supply-allocation-reconciler'

def run(payload: dict) -> dict:
    demands = load_records(payload)
    inventory = load_inventory(payload)
    return build_result(PROJECT, calculate(demands, inventory))
