"""Process-independent external-data refresh job use case."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from backend.jobs.progress import ProgressCB


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REFRESH_SCRIPT = PROJECT_ROOT / "scripts" / "refresh_external_data.py"
PYTHON = sys.executable or "python"
EXTERNAL_REFRESH_SOURCES = frozenset(
    {"all", "cot", "events", "etf", "fred", "cb", "etf_daily"}
)


def run_external_data_refresh(
    params: Mapping[str, Any],
    progress: ProgressCB,
) -> dict[str, Any]:
    """Run one bounded refresh from either the PG worker or legacy adapter."""

    source = str(params.get("source") or "all").strip().lower()
    if source not in EXTERNAL_REFRESH_SOURCES:
        raise ValueError(f"invalid_external_refresh_source:{source}")
    if not REFRESH_SCRIPT.is_file():
        raise RuntimeError(f"external_refresh_script_missing:{REFRESH_SCRIPT}")

    args = [PYTHON, str(REFRESH_SCRIPT), "--once"]
    if source != "all":
        args.extend(["--source", source])
    if bool(params.get("force", False)):
        args.append("--force")

    progress("launch", 5.0, f"refresh source={source}")
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=300,
        encoding="utf-8",
        errors="replace",
    )
    output = (result.stdout if result.returncode == 0 else result.stderr or result.stdout)
    lines = str(output or "").strip().splitlines()[-20:]
    if result.returncode != 0:
        raise RuntimeError(
            f"external_refresh_failed:source={source}:returncode={result.returncode}:"
            + "\n".join(lines)
        )
    progress("complete", 100.0, f"refresh source={source} complete")
    return {
        "status": "completed",
        "source": source,
        "force": bool(params.get("force", False)),
        "output": lines,
        "returncode": int(result.returncode),
    }


__all__ = [
    "EXTERNAL_REFRESH_SOURCES",
    "PYTHON",
    "REFRESH_SCRIPT",
    "run_external_data_refresh",
]
