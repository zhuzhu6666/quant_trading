"""Shadow factor service — read/promote/demote via alpha/persistent_registry.jsonl."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.paths import CHARTS_DIR


# Shadow factors are persisted in a jsonl log; each line is a JSON record.
SHADOW_LOG = CHARTS_DIR / "shadow_factors.jsonl"


def _read_log() -> list[dict]:
    if not SHADOW_LOG.exists():
        return []
    out = []
    for line in SHADOW_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def list_shadows() -> list[dict]:
    """Return current shadow factors, deduplicated by name (latest entry wins)."""
    entries = _read_log()
    by_name: dict[str, dict] = {}
    for e in entries:
        name = e.get("name")
        if not name:
            continue
        by_name[name] = e
    return list(by_name.values())


def promote(name: str) -> dict:
    """Mark a shadow factor as promoted to active."""
    return _mutate(name, "active", "promote")


def demote(name: str) -> dict:
    """Mark a shadow factor as demoted (kept in log but flagged shadow)."""
    return _mutate(name, "shadow", "demote")


def _mutate(name: str, new_status: str, action: str) -> dict:
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "name": name,
        "status": new_status,
        "action": action,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with SHADOW_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"name": name, "new_status": new_status, "ok": True}
