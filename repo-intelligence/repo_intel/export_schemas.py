"""Export JSON Schemas for every contract: `python -m repo_intel.export_schemas`."""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import EXPORTED

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "contracts" / "schemas"


def render() -> dict[str, str]:
    return {f"{m.__name__}.v1.json": json.dumps(m.model_json_schema(), indent=2, sort_keys=True) + "\n" for m in EXPORTED}


def main() -> None:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in render().items():
        (SCHEMA_DIR / name).write_text(text)
    print(f"wrote {len(EXPORTED)} schemas to {SCHEMA_DIR}")


if __name__ == "__main__":
    main()
