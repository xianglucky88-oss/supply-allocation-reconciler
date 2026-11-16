import argparse
import json
from pathlib import Path
from .pipeline import run

def main() -> None:
    parser = argparse.ArgumentParser(description='Allocate constrained inventory to demand while explaining unmet commitments.')
    parser.add_argument("input", nargs="?", default=str(Path(__file__).parents[2] / "fixtures" / "input.json"))
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text())
    print(json.dumps(run(payload), ensure_ascii=False, indent=2, sort_keys=True))

if __name__ == "__main__":
    main()
