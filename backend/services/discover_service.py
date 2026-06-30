"""L2 factor discovery service — delegates to scripts/discover_factors.run_discovery."""
from typing import Any

from loguru import logger

from backend.jobs.progress import ProgressCB


def run_discovery(params: dict[str, Any], progress_cb: ProgressCB) -> dict:
    """Run L2 factor discovery. Delegates to the two-mode scripts/discover_factors."""
    from scripts.discover_factors import run_discovery as _run
    return _run(
        n_candidates=int(params.get("n_candidates", 1000)),
        top_k=int(params.get("top_k", 50)),
        forward_periods=params.get("forward_periods", [1, 5, 20]),
        auto_register=bool(params.get("auto_register", False)),
        engine=params.get("engine", "gp"),
        gp_pop=int(params.get("gp_pop", 100)),
        gp_gen=int(params.get("gp_gen", 20)),
        n_bars=int(params.get("n_bars", 50000)),
        progress_cb=progress_cb,
    )
