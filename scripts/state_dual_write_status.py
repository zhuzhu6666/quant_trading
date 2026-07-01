"""Deprecated: state dual-write outbox is disabled after PostgreSQL migration."""

from __future__ import annotations

import json


def main() -> int:
    result = {
        "enabled": False,
        "deprecated": True,
        "message": "state dual-write outbox is disabled; runtime state writes go directly to PostgreSQL state_v1",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
