"""Canonical historical backtest task backed by the live-parity replay runner."""
from __future__ import annotations

from typing import Any

from backend.jobs.progress import ProgressCB
from backend.services.parity_replay import ParityReplayService


def _job_result_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Keep task state compact; the full auditable report remains on disk."""

    result = dict(report)
    result.pop("trades", None)
    result.pop("events", None)
    bundle = dict(result.get("learning_bundle") or {})
    bundle.pop("open_samples", None)
    bundle.pop("factor_samples", None)
    result["learning_bundle"] = bundle
    return result


def run_backtest(params: dict[str, Any], progress_cb: ProgressCB) -> dict[str, Any]:
    progress_cb("freezing", 5, "冻结历史数据、代码、配置和因子版本")
    from backend.services.evolution_work_coordinator import EvolutionWorkCoordinator

    report = EvolutionWorkCoordinator().run(
        "historical_backtest",
        lambda: ParityReplayService().run(
            {**params, "persist_artifact": True},
            progress_cb=progress_cb,
        ),
    )
    if str(dict(report or {}).get("status") or "") == "skipped_busy":
        raise RuntimeError("heavy_research_job_already_running")
    metrics = dict(report.get("metrics") or {})
    progress_cb(
        "completed",
        100,
        (
            f"完成 {int(metrics.get('bar_count') or 0)} 根K线、"
            f"{int(metrics.get('independent_trade_count') or 0)} 笔独立交易"
        ),
    )
    return _job_result_summary(report)
