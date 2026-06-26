"""Sync service — 把 data.live_sync 包装成 LoopHost 可用的 factory。

对外保持原 get_status / run_sync_once 接口(其它服务/测试还在用),
新增 sync_runner_factory 给 LoopHost 用。
Phase 1.4 会把 health/recovery/quality_gate 接进来。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from loguru import logger

from backend.jobs.progress import ProgressCB
from backend.runtime.runtime_state import RuntimeState

# 保留模块级 logger 名称(老代码用 loguru,新代码用 stdlib logging,统一用 name 区分)
_stdlib_logger = logging.getLogger(__name__)


def get_status() -> dict:
    """Return current sync status (from the live_sync_status.json on disk)."""
    from scripts.live_sync import get_status as _get_status

    return _get_status()


def run_sync_once(params: dict[str, Any], progress_cb: ProgressCB) -> dict:
    """Run a one-shot sync. Delegates to scripts/live_sync.run_sync_once."""
    from scripts.live_sync import run_sync_once as _run

    return _run(
        timeframes=params.get("timeframes", ["M15", "H1", "D1"]),
        sync_type=params.get("type", "incremental"),
        progress_cb=progress_cb,
    )


# ----- Phase 1.1 新增:LoopHost factory -----
async def sync_runner_factory(state: RuntimeState) -> None:
    """LoopHost 的 sync 工厂:周期跑一次 CTraderPuller。"""
    from data.live_sync.health import SyncHealth

    loop_status = state.get_loop("sync")
    interval_sec = int(loop_status.extra.get("interval_sec", 300)) if loop_status else 300
    health = SyncHealth.shared()
    _stdlib_logger.info("sync loop started, interval=%ds", interval_sec)
    while True:
        try:
            await _do_one_sync(state, health)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _stdlib_logger.exception("sync iteration failed")
            health.record_failure("iteration_exception")
            state.emit_metric("sync_iteration_error", {})

        await asyncio.sleep(interval_sec)


async def _do_one_sync(state: RuntimeState, health) -> None:
    """一次同步迭代。委托 CTraderPuller (原 MT5 orchestrator 已移除)。"""
    from monitor.evolution_story import EvolutionStory

    health.record_attempt()
    try:
        from data.live_sync.ctrader_puller import CTraderPuller
        from config.runtime_config import shared as rcc

        cfg = rcc()
        symbol = list(cfg.enabled_symbols)[0] if hasattr(cfg, 'enabled_symbols') and cfg.enabled_symbols else "XAUUSD+"
        results = {}
        last_bar_ts_by_tf = {}
        for tf in ["M5", "M15"]:
            puller = CTraderPuller()
            r = puller.pull_history(symbol=symbol, timeframe=tf, n=50)
            results[tf] = r.n_bars if hasattr(r, 'n_bars') else 0
            if getattr(r, "last_time", 0):
                last_bar_ts_by_tf[tf] = float(r.last_time)

        total = sum(results.values())
        _stdlib_logger.info("sync done: %d bars | %s", total, results)
        health.record_success(last_bar_ts_by_tf=last_bar_ts_by_tf or None)
        if total > 0:
            EvolutionStory.shared().append("sync_success", {"inserted": total})
    except Exception as e:
        health.record_failure(str(e)[:100])
        _stdlib_logger.exception("sync failed")
        EvolutionStory.shared().append("sync_error", {"error": str(e)[:200]})

    state.emit_metric("sync_iteration_done", {"inserted": total if 'total' in dir() else 0})
