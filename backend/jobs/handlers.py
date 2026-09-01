"""Versioned persistent-job handler registry.
Imports stay inside handlers so the worker only loads the research stack for
the claimed job kind.  None of these handlers owns broker execution authority.
Direct canonical imports — glue services deleted (194 lines saved).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.jobs.progress import ProgressCB

JobHandler = Callable[[Mapping[str, Any], ProgressCB], Any]

# external refresh constants (migrated from deleted glue)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
REFRESH_SCRIPT = PROJECT_ROOT / "scripts" / "refresh_external_data.py"
PYTHON = sys.executable or "python"
EXTERNAL_REFRESH_SOURCES = frozenset(
    {"all", "cot", "events", "etf", "fred", "cb", "etf_daily"}
)


def run_backtest_job(params: Mapping[str, Any], progress: ProgressCB) -> Any:
    from backend.services.backtest_service import run_backtest
    return run_backtest(dict(params), progress)


def run_discover_job(params: Mapping[str, Any], progress: ProgressCB) -> Any:
    from scripts.discover_factors import run_discovery
    return run_discovery(dict(params), progress)


def run_tuning_job(params: Mapping[str, Any], progress: ProgressCB) -> Any:
    from scripts.tune_risk_params import run_tuning
    return run_tuning(dict(params), progress)


def run_ab_test_job(params: Mapping[str, Any], progress: ProgressCB) -> Any:
    from scripts.p1_e_ab_test import run_ab
    return run_ab(dict(params), progress)


def run_external_refresh_job(params: Mapping[str, Any], progress: ProgressCB) -> Any:
    """Process-independent external-data refresh — inlined from deleted glue."""
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


def run_sync_job(params: Mapping[str, Any], progress: ProgressCB) -> Any:
    from backend.services.sync_service import run_sync_once
    return run_sync_once(dict(params), progress)


def run_factor_health_job(params: Mapping[str, Any], progress: ProgressCB) -> Any:
    """Factor health evaluation — inlined from deleted glue, canonical is alpha.factor_health."""
    progress("loading", 5, "importing alpha.factor_health")
    from alpha.factor_health import evaluate_factors, write_report
    from backend.core.paths import CHARTS_DIR
    from data.factor_frame import FactorFrameBuilder

    threshold = float(params.get("threshold", 0.04))
    bar_count = int(params.get("bar_count", 50000))
    symbol = str(params.get("symbol", "XAUUSD+") or "XAUUSD+")
    timeframe = str(params.get("timeframe", "M15") or "M15")
    progress("loading", 10, f"loading {bar_count} enriched bars from factor frame")
    df = FactorFrameBuilder().build(symbol=symbol, timeframe=timeframe, limit=bar_count or None)
    progress("loaded", 30, f"loaded {len(df)} bars")
    progress("evaluating", 40, "evaluating factors (5-dim scoring)")
    result = evaluate_factors(df, threshold=threshold, progress_cb=progress)
    progress("evaluated", 90, f"{result['healthy']} HEALTHY / {result['watch']} WATCH / {result['decaying']} DECAYING / {result.get('unknown', 0)} UNKNOWN")
    progress("writing", 95, "writing report")
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    out_txt = CHARTS_DIR / "factor_health_report.txt"
    out_json = CHARTS_DIR / "factor_health_report.json"
    write_report(result, out_txt, out_json)
    progress("done", 100, f"report at {out_txt}")
    return {
        "healthy": result["healthy"],
        "watch": result["watch"],
        "decaying": result["decaying"],
        "factors": result.get("factors", []),
        "report_path": str(out_txt),
    }


def run_parameter_template_validation_job(
    params: Mapping[str, Any],
    progress: ProgressCB,
) -> Any:
    from backend.services.parameter_template_validation import (
        run_parameter_template_offline_validation,
    )
    return run_parameter_template_offline_validation(dict(params), progress)


PERSISTENT_JOB_HANDLERS: dict[str, JobHandler] = {
    "backtest": run_backtest_job,
    "discover": run_discover_job,
    "tuning": run_tuning_job,
    "ab_test": run_ab_test_job,
    "external_refresh": run_external_refresh_job,
    "sync": run_sync_job,
    "factor_health": run_factor_health_job,
    "parameter_template_validation": run_parameter_template_validation_job,
}


def persistent_job_handlers() -> dict[str, JobHandler]:
    return dict(PERSISTENT_JOB_HANDLERS)
