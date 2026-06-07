"""A/B test service — wraps scripts/p1_e_ab_test via subprocess."""
import subprocess
import sys
from pathlib import Path
from typing import Any

from backend.core.paths import CHARTS_DIR, PROJECT_ROOT
from backend.jobs.progress import ProgressCB


def run_ab(params: dict[str, Any], progress_cb: ProgressCB) -> dict:
    """Run an A/B test between two strategy paths. Wraps scripts/p1_e_ab_test.py.

    Phase 1: subprocess. Phase 4 will refactor the script into an importable
    service function.
    """
    path_a = params.get("path_a", "baseline")
    path_b = params.get("path_b", "reverse")
    n_bars = int(params.get("n_bars", 5000))

    progress_cb("loading", 5, f"path_a={path_a} path_b={path_b} n_bars={n_bars}")
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CHARTS_DIR / "p1_e_ab_report.txt"

    cmd = [
        sys.executable, "scripts/p1_e_ab_test.py",
        "--path-a", path_a,
        "--path-b", path_b,
        "--n-bars", str(n_bars),
    ]
    progress_cb("running", 20, " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))

    progress_cb("running", 80, f"ab exited rc={proc.returncode}")
    if proc.returncode != 0:
        raise RuntimeError(f"ab test failed: {proc.stderr[-500:]}")

    report_text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
    progress_cb("done", 100, f"report: {out_path}")

    return {
        "returncode": proc.returncode,
        "report_path": str(out_path),
        "report_excerpt": report_text[-2000:],
    }
