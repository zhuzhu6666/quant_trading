"""Factor health evaluation — wraps alpha/factor_health.run_evaluation()."""
from typing import Any
from pathlib import Path

from loguru import logger

from backend.core.paths import CHARTS_DIR
from backend.jobs.progress import ProgressCB


def run_factor_health(
    params: dict[str, Any], progress_cb: ProgressCB
) -> dict:
    """Run factor health evaluation. Phase 1: import alpha.factor_health.

    Raises on import or runtime failure; caller maps to JobState.error.
    """
    progress_cb("loading", 5, "importing alpha.factor_health")
    from alpha.factor_health import evaluate_factors, write_report

    threshold = float(params.get("threshold", 0.04))
    bar_count = int(params.get("bar_count", 50000))

    symbol = str(params.get("symbol", "XAUUSD+") or "XAUUSD+")
    timeframe = str(params.get("timeframe", "M15") or "M15")
    progress_cb("loading", 10, f"loading {bar_count} enriched bars from factor frame")
    from data.factor_frame import FactorFrameBuilder
    df = FactorFrameBuilder().build(symbol=symbol, timeframe=timeframe, limit=bar_count or None)
    progress_cb("loaded", 30, f"loaded {len(df)} bars")

    progress_cb("evaluating", 40, "evaluating factors (5-dim scoring)")
    result = evaluate_factors(df, threshold=threshold, progress_cb=progress_cb)
    progress_cb("evaluated", 90, f"{result['healthy']} HEALTHY / {result['watch']} WATCH / {result['decaying']} DECAYING")

    progress_cb("writing", 95, "writing report")
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    out_txt = CHARTS_DIR / "factor_health_report.txt"
    out_json = CHARTS_DIR / "factor_health_report.json"
    write_report(result, out_txt, out_json)
    progress_cb("done", 100, f"report at {out_txt}")

    return {
        "healthy": result["healthy"],
        "watch": result["watch"],
        "decaying": result["decaying"],
        "factors": result.get("factors", []),
        "report_path": str(out_txt),
    }
