"""L2 factor discovery service — wraps alpha/factor_search_gp + factor_discovery."""
import time
from typing import Any

from loguru import logger

from backend.core.paths import CHARTS_DIR
from backend.jobs.progress import ProgressCB


def run_discovery(params: dict[str, Any], progress_cb: ProgressCB) -> dict:
    """Run L2 factor discovery (GP or random). Writes report to data/charts/.

    Phase 1: thin wrapper that picks engine. Phase 4 will refactor scripts/discover_factors.py
    into a service function; for now we call the alpha package directly.
    """
    engine = params.get("engine", "gp")
    n_candidates = int(params.get("n_candidates", 1000))
    top_k = int(params.get("top_k", 50))
    forward_periods = params.get("forward_periods", [1, 5, 20])
    auto_register = bool(params.get("auto_register", True))
    gp_pop = int(params.get("gp_pop", 100))
    gp_gen = int(params.get("gp_gen", 20))

    progress_cb("loading", 5, f"engine={engine} n={n_candidates} top_k={top_k}")
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = CHARTS_DIR / "discover_report.json"

    progress_cb("running", 20, "loading bars from db")
    from data.store import DataStore
    store = DataStore("data/market_data.db")
    df = store.load_bars("XAUUSD+", "M15")
    if len(df) > 50000:
        df = df.tail(50000)
    progress_cb("loaded", 30, f"loaded {len(df)} bars")

    if engine == "gp":
        progress_cb("running", 40, f"GP search pop={gp_pop} gen={gp_gen}")
        from alpha.factor_search_gp import run_gp_search
        candidates = run_gp_search(df, pop=gp_pop, gen=gp_gen, progress_cb=progress_cb)
    else:
        progress_cb("running", 40, f"random search n={n_candidates}")
        from alpha.factor_search import run_random_search
        candidates = run_random_search(df, n=n_candidates, progress_cb=progress_cb)

    progress_cb("evaluating", 70, f"evaluating {len(candidates)} candidates")
    # Top-k + report
    top = candidates[:top_k] if isinstance(candidates, list) else []

    progress_cb("writing", 95, f"writing report to {report_path}")
    import json
    report_path.write_text(
        json.dumps(
            {
                "engine": engine,
                "n_candidates": n_candidates,
                "top_k": top_k,
                "found": len(candidates),
                "top": [
                    {"name": getattr(c, "name", str(i)), "expr": getattr(c, "expr", ""), "ic": getattr(c, "ic", 0.0)}
                    for i, c in enumerate(top)
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    progress_cb("done", 100, f"done. {len(top)} top factors written")

    return {
        "engine": engine,
        "candidates_total": n_candidates,
        "found": len(candidates),
        "top_factors": [
            {"name": getattr(c, "name", str(i)), "expr": getattr(c, "expr", ""), "ic": getattr(c, "ic", 0.0)}
            for i, c in enumerate(top)
        ],
        "report_path": str(report_path),
    }
