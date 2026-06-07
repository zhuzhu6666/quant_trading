"""A/B test service — delegates to scripts/p1_e_ab_test.run_ab."""
import json
from typing import Any

from loguru import logger

from backend.jobs.progress import ProgressCB


def run_ab(params: dict[str, Any], progress_cb: ProgressCB) -> dict:
    """Run an A/B test. Delegates to scripts/p1_e_ab_test.run_ab."""
    from scripts.p1_e_ab_test import run_ab as _run
    return _run(
        path_a=params.get("path_a", "baseline"),
        path_b=params.get("path_b", "reverse"),
        n_bars=int(params.get("n_bars", 5000)),
        progress_cb=progress_cb,
    )
