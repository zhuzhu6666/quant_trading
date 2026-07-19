#!/usr/bin/env python3
"""Run and durably attest the Safety v2 fault-injection matrix."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.live_safety_fault_matrix import run_fault_matrix


def main() -> int:
    result = run_fault_matrix(root=ROOT)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
