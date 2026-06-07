"""Calibrator service — read/write data/charts/calibrator_bucket.json."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.paths import CHARTS_DIR


CALIBRATOR_PATH = CHARTS_DIR / "calibrator_bucket.json"


def get_status() -> dict:
    """Return current calibrator state on disk."""
    if not CALIBRATOR_PATH.exists():
        return {"path": str(CALIBRATOR_PATH), "exists": False, "buckets": None, "last_modified": None}
    try:
        data = json.loads(CALIBRATOR_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return {"path": str(CALIBRATOR_PATH), "exists": True, "buckets": None, "error": str(e)}
    return {
        "path": str(CALIBRATOR_PATH),
        "exists": True,
        "buckets": data.get("buckets"),
        "platt": data.get("platt"),
        "last_modified": datetime.fromtimestamp(CALIBRATOR_PATH.stat().st_mtime, tz=timezone.utc).isoformat(),
    }


def save(buckets: list[dict]) -> dict:
    """Save the calibrator buckets to disk. Merges into existing JSON."""
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if CALIBRATOR_PATH.exists():
        try:
            existing = json.loads(CALIBRATOR_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing["buckets"] = buckets
    existing["saved_at"] = datetime.now(timezone.utc).isoformat()
    CALIBRATOR_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"path": str(CALIBRATOR_PATH), "saved_n": len(buckets)}


def load() -> dict:
    """Reload calibrator into the running ProbabilityCalibrator instance.

    Phase 1: stub that just re-reads the file. Phase 4 will hook into the actual
    ProbabilityCalibrator singleton.
    """
    if not CALIBRATOR_PATH.exists():
        raise FileNotFoundError(f"calibrator file not found: {CALIBRATOR_PATH}")
    data = json.loads(CALIBRATOR_PATH.read_text(encoding="utf-8"))
    return {"buckets": data.get("buckets"), "platt": data.get("platt")}
