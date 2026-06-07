"""Risk-parameter tuning service — wraps scripts/tune_risk_params via subprocess."""
import subprocess
import sys
from pathlib import Path
from typing import Any

from backend.core.paths import CHARTS_DIR, PROJECT_ROOT
from backend.jobs.progress import ProgressCB


def run_tuning(params: dict[str, Any], progress_cb: ProgressCB) -> dict:
    """Run a tuning sweep. Wraps scripts/tune_risk_params via subprocess.

    Phase 1: subprocess. Phase 4 will refactor scripts/tune_risk_params.py
    into an importable service function and replace this.
    """
    risk_grid = params.get("risk_pct_grid", [0.5, 1.0, 1.5, 2.0])
    cb_grid = params.get("cb_pct_grid", [5, 10, 15, 20])
    n_bars = int(params.get("n_bars", 5000))

    progress_cb("loading", 5, f"risk={risk_grid} cb={cb_grid} n_bars={n_bars}")
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CHARTS_DIR / "tune_report.txt"

    cmd = [
        sys.executable, "scripts/tune_risk_params.py",
        "--risk-grid", ",".join(str(x) for x in risk_grid),
        "--cb-grid", ",".join(str(x) for x in cb_grid),
        "--n-bars", str(n_bars),
    ]
    progress_cb("running", 20, " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))

    progress_cb("running", 80, f"tuning exited rc={proc.returncode}")
    if proc.returncode != 0:
        raise RuntimeError(f"tuning failed: {proc.stderr[-500:]}")

    # Parse latest tune report
    report_text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
    progress_cb("done", 100, f"report: {out_path}")

    return {
        "returncode": proc.returncode,
        "report_path": str(out_path),
        "report_excerpt": report_text[-2000:],
    }
