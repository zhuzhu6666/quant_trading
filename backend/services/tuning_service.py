"""Tuning service — delegates to scripts/tune_risk_params.run_tuning."""
import json
from typing import Any

from loguru import logger

from backend.jobs.progress import ProgressCB


def run_tuning(params: dict[str, Any], progress_cb: ProgressCB) -> dict:
    """Run a tuning sweep. Delegates to scripts/tune_risk_params."""
    from scripts.tune_risk_params import run_tuning as _run
    return _run(
        risk_pct_grid=params.get("risk_pct_grid", [0.5, 1.0, 1.5, 2.0]),
        cb_pct_grid=params.get("cb_pct_grid", [5, 10, 15, 20]),
        n_bars=int(params.get("n_bars", 5000)),
        top_k=int(params.get("top_k", 5)),
        progress_cb=progress_cb,
    )
